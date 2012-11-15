from json import dumps, loads
import leveldb, urllib
import deserialize
import ast, time, threading, hashlib
from Queue import Queue
import traceback, sys, os



Hash = lambda x: hashlib.sha256(hashlib.sha256(x).digest()).digest()
hash_encode = lambda x: x[::-1].encode('hex')
hash_decode = lambda x: x.decode('hex')[::-1]



def rev_hex(s):
    return s.decode('hex')[::-1].encode('hex')


def int_to_hex(i, length=1):
    s = hex(i)[2:].rstrip('L')
    s = "0"*(2*length - len(s)) + s
    return rev_hex(s)

def header_to_string(res):
    pbh = res.get('prev_block_hash')
    if pbh is None: pbh = '0'*64
    s = int_to_hex(res.get('version'),4) \
        + rev_hex(pbh) \
        + rev_hex(res.get('merkle_root')) \
        + int_to_hex(int(res.get('timestamp')),4) \
        + int_to_hex(int(res.get('bits')),4) \
        + int_to_hex(int(res.get('nonce')),4)
    return s

def header_from_string( s):
    hex_to_int = lambda s: eval('0x' + s[::-1].encode('hex'))
    h = {}
    h['version'] = hex_to_int(s[0:4])
    h['prev_block_hash'] = hash_encode(s[4:36])
    h['merkle_root'] = hash_encode(s[36:68])
    h['timestamp'] = hex_to_int(s[68:72])
    h['bits'] = hex_to_int(s[72:76])
    h['nonce'] = hex_to_int(s[76:80])
    return h




from processor import Processor, print_log

class BlockchainProcessor(Processor):

    def __init__(self, config, shared):
        Processor.__init__(self)

        self.shared = shared
        self.up_to_date = False
        self.watched_addresses = []
        self.history_cache = {}
        self.chunk_cache = {}
        self.cache_lock = threading.Lock()
        self.headers_data = ''

        self.mempool_addresses = {}
        self.mempool_hist = {}
        self.mempool_hashes = []
        self.mempool_lock = threading.Lock()

        self.address_queue = Queue()
        self.dbpath = config.get('leveldb', 'path')

        self.dblock = threading.Lock()
        try:
            self.db = leveldb.LevelDB(self.dbpath)
        except:
            traceback.print_exc(file=sys.stdout)
            self.shared.stop()

        self.bitcoind_url = 'http://%s:%s@%s:%s/' % (
            config.get('bitcoind','user'),
            config.get('bitcoind','password'),
            config.get('bitcoind','host'),
            config.get('bitcoind','port'))

        self.height = 0
        self.sent_height = 0
        self.sent_header = None


        try:
            hist = self.deserialize(self.db.Get('0'))
            hh, self.height, _ = hist[0] 
            self.block_hashes = [hh]
            print_log( "hist", hist )
        except:
            #traceback.print_exc(file=sys.stdout)
            print_log('initializing database')
            self.height = 0
            self.block_hashes = [ '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f' ]

        # catch_up headers
        self.init_headers(self.height)

        threading.Timer(0, lambda: self.catch_up(sync=False)).start()
        while not shared.stopped() and not self.up_to_date:
            try:
                time.sleep(1)
            except:
                print "keyboard interrupt: stopping threads"
                shared.stop()
                sys.exit(0)

        print "blockchain is up to date."

        threading.Timer(10, self.main_iteration).start()



    def bitcoind(self, method, params=[]):
        postdata = dumps({"method": method, 'params': params, 'id':'jsonrpc'})
        respdata = urllib.urlopen(self.bitcoind_url, postdata).read()
        r = loads(respdata)
        if r['error'] != None:
            raise BaseException(r['error'])
        return r.get('result')
    

    def serialize(self, h):
        s = ''
        for txid, txpos, height in h:
            s += txid + int_to_hex(txpos, 4) + int_to_hex(height, 4)
        return s.decode('hex')


    def deserialize(self, s):
        h = []
        while s:
            txid = s[0:32].encode('hex')
            txpos = int( rev_hex( s[32:36].encode('hex') ), 16 )
            height = int( rev_hex( s[36:40].encode('hex') ), 16 )
            h.append( ( txid, txpos, height ) )
            s = s[40:]
        return h


    def block2header(self, b):
        return {"block_height":b.get('height'), "version":b.get('version'), "prev_block_hash":b.get('previousblockhash'), 
                "merkle_root":b.get('merkleroot'), "timestamp":b.get('time'), "bits":int(b.get('bits'),16), "nonce":b.get('nonce')}


    def get_header(self, height):
        block_hash = self.bitcoind('getblockhash', [height])
        b = self.bitcoind('getblock', [block_hash])
        return self.block2header(b)
    

    def init_headers(self, db_height):
        self.chunk_cache = {}
        self.headers_filename = os.path.join( self.dbpath, 'blockchain_headers')

        height = 0
        if os.path.exists(self.headers_filename):
            height = os.path.getsize(self.headers_filename)/80

        if height:
            prev_header = self.read_header(height -1)
            prev_hash = self.hash_header(prev_header)
        else:
            open(self.headers_filename,'wb').close()
            prev_hash = None

        if height != db_height:
            print_log( "catching up missing headers:", height, db_height)

        s = ''
        try:
            for i in range(height, db_height):
                header = self.get_header(i)
                assert prev_hash == header.get('prev_block_hash')
                self.write_header(header, sync=False)
                prev_hash = self.hash_header(header)
                if i%1000==0: print_log("headers file:",i)
        except KeyboardInterrupt:
            self.flush_headers()
            sys.exit()

        self.flush_headers()


    def hash_header(self, header):
        return rev_hex(Hash(header_to_string(header).decode('hex')).encode('hex'))


    def read_header(self, block_height):
        if os.path.exists(self.headers_filename):
            f = open(self.headers_filename,'rb')
            f.seek(block_height*80)
            h = f.read(80)
            f.close()
            if len(h) == 80:
                h = header_from_string(h)
                return h


    def read_chunk(self, index):
        f = open(self.headers_filename,'rb')
        f.seek(index*2016*80)
        chunk = f.read(2016*80)
        f.close()
        return chunk.encode('hex')


    def write_header(self, header, sync=True):
        if not self.headers_data:
            self.headers_offset = header.get('block_height')
        self.headers_data += header_to_string(header).decode('hex')
        if sync or len(self.headers_data) > 40*100:
            self.flush_headers()

    def pop_header(self):
        # we need to do this only if we have not flushed
        if self.headers_data:
            self.headers_data = self.headers_data[:-40]

    def flush_headers(self):
        if not self.headers_data: return
        f = open(self.headers_filename,'rb+')
        f.seek(self.headers_offset*80)
        f.write(self.headers_data)
        f.close()
        self.headers_data = ''


    def get_chunk(self, i):
        # store them on disk; store the current chunk in memory
        chunk = self.chunk_cache.get(i)
        if not chunk:
            chunk = self.read_chunk(i)
            self.chunk_cache[i] = chunk
        return chunk


    def get_transaction(self, txid, block_height=-1, is_coinbase = False):
        raw_tx = self.bitcoind('getrawtransaction', [txid, 0, block_height])
        vds = deserialize.BCDataStream()
        vds.write(raw_tx.decode('hex'))
        out = deserialize.parse_Transaction(vds, is_coinbase)
        return out


    def get_history(self, addr, cache_only=False):
        with self.cache_lock: hist = self.history_cache.get( addr )
        if hist is not None: return hist
        if cache_only: return -1

        with self.dblock:
            try:
                hist = self.deserialize(self.db.Get(addr))
                is_known = True
            except: 
                hist = []
                is_known = False

        # should not be necessary
        hist.sort( key=lambda tup: tup[1])
        # check uniqueness too...

        # add memory pool
        with self.mempool_lock:
            for txid in self.mempool_hist.get(addr,[]):
                hist.append((txid, 0, 0))

        hist = map(lambda x: {'tx_hash':x[0], 'height':x[2]}, hist)
        # add something to distinguish between unused and empty addresses
        if hist == [] and is_known: hist = ['*']

        with self.cache_lock: self.history_cache[addr] = hist
        return hist


    def get_status(self, addr, cache_only=False):
        tx_points = self.get_history(addr, cache_only)
        if cache_only and tx_points == -1: return -1

        if not tx_points: return None
        if tx_points == ['*']: return '*'
        status = ''
        for tx in tx_points:
            status += tx.get('tx_hash') + ':%d:' % tx.get('height')
        return hashlib.sha256( status ).digest().encode('hex')


    def get_merkle(self, tx_hash, height):

        block_hash = self.bitcoind('getblockhash', [height])
        b = self.bitcoind('getblock', [block_hash])
        tx_list = b.get('tx')
        tx_pos = tx_list.index(tx_hash)
        
        merkle = map(hash_decode, tx_list)
        target_hash = hash_decode(tx_hash)
        s = []
        while len(merkle) != 1:
            if len(merkle)%2: merkle.append( merkle[-1] )
            n = []
            while merkle:
                new_hash = Hash( merkle[0] + merkle[1] )
                if merkle[0] == target_hash:
                    s.append( hash_encode( merkle[1]))
                    target_hash = new_hash
                elif merkle[1] == target_hash:
                    s.append( hash_encode( merkle[0]))
                    target_hash = new_hash
                n.append( new_hash )
                merkle = merkle[2:]
            merkle = n

        return {"block_height":height, "merkle":s, "pos":tx_pos}

        
    def add_to_batch(self, addr, tx_hash, tx_pos, tx_height):

        # we do it chronologically, so nothing wrong can happen...
        s = (tx_hash + int_to_hex(tx_pos, 4) + int_to_hex(tx_height, 4)).decode('hex')
        self.batch_list[addr] += s

        # backlink
        txo = (tx_hash + int_to_hex(tx_pos, 4)).decode('hex')
        self.batch_txio[txo] = addr


    def remove_from_batch(self, tx_hash, tx_pos):
                    
        txi = (tx_hash + int_to_hex(tx_pos, 4)).decode('hex')
        try:
            addr = self.batch_txio[txi]
        except:
            #raise BaseException(tx_hash, tx_pos)
            print "WARNING: cannot find address for", (tx_hash, tx_pos)
            return

        serialized_hist = self.batch_list[addr]

        l = len(serialized_hist)/40
        for i in range(l):
            if serialized_hist[40*i:40*i+36] == txi:
                serialized_hist = serialized_hist[0:40*i] + serialized_hist[40*(i+1):]
                break
        else:
            raise BaseException("prevout not found", addr, hist, tx_hash, tx_pos)
        self.batch_list[addr] = serialized_hist


    def deserialize_block(self, block):
        txlist = block.get('tx')
        tx_hashes = []  # ordered txids
        txdict = {}     # deserialized tx
        is_coinbase = True
        for raw_tx in txlist:
            tx_hash = hash_encode(Hash(raw_tx.decode('hex')))
            tx_hashes.append(tx_hash)
            vds = deserialize.BCDataStream()
            vds.write(raw_tx.decode('hex'))
            tx = deserialize.parse_Transaction(vds, is_coinbase)
            txdict[tx_hash] = tx
            is_coinbase = False
        return tx_hashes, txdict


    def import_block(self, block, block_hash, block_height, sync, revert=False):

        self.batch_list = {}  # address -> history
        self.batch_txio = {}  # transaction i/o -> address

        inputs_to_read = []
        addr_to_read = []

        # deserialize transactions
        t0 = time.time()
        tx_hashes, txdict = self.deserialize_block(block)

        # read addresses of tx inputs
        t00 = time.time()
        for tx in txdict.values():
            for x in tx.get('inputs'):
                txi = (x.get('prevout_hash') + int_to_hex(x.get('prevout_n'), 4)).decode('hex')
                inputs_to_read.append(txi)

        inputs_to_read.sort()
        for txi in inputs_to_read:
            try:
                addr = self.db.Get(txi)    
            except:
                # the input could come from the same block
                continue
            self.batch_txio[txi] = addr
            addr_to_read.append(addr)

        # read histories of addresses
        for txid, tx in txdict.items():
            for x in tx.get('outputs'):
                addr_to_read.append(x.get('address'))

        addr_to_read.sort()
        for addr in addr_to_read:
            try:
                self.batch_list[addr] = self.db.Get(addr)
            except: 
                self.batch_list[addr] = ''
              
        # process
        t1 = time.time()

        for txid in tx_hashes: # must be ordered
            tx = txdict[txid]
            if not revert:
                for x in tx.get('inputs'):
                    self.remove_from_batch( x.get('prevout_hash'), x.get('prevout_n'))
                for x in tx.get('outputs'):
                    self.add_to_batch( x.get('address'), txid, x.get('index'), block_height)
            else:
                for x in tx.get('outputs'):
                    self.remove_from_batch( x.get('prevout_hash'), x.get('prevout_n'))
                for x in tx.get('inputs'):
                    self.add_to_batch( x.get('address'), txid, x.get('index'), block_height)

        # write
        max_len = 0
        max_addr = ''
        t2 = time.time()

        batch = leveldb.WriteBatch()
        for addr, serialized_hist in self.batch_list.items():
            batch.Put(addr, serialized_hist)
            l = len(serialized_hist)
            if l > max_len:
                max_len = l
                max_addr = addr

        for txio, addr in self.batch_txio.items():
            batch.Put(txio, addr)
        # delete spent inputs
        for txi in inputs_to_read:
            batch.Delete(txi)
        batch.Put('0', self.serialize( [(block_hash, block_height, 0)] ) )

        # actual write
        self.db.Write(batch, sync = sync)

        t3 = time.time()
        if t3 - t0 > 10: 
            print_log("block", block_height, 
                      "parse:%0.2f "%(t00 - t0), 
                      "read:%0.2f "%(t1 - t00), 
                      "proc:%.2f "%(t2-t1), 
                      "write:%.2f "%(t3-t2), 
                      "max:", max_len, max_addr)

        for addr in self.batch_list.keys(): self.invalidate_cache(addr)



    def add_request(self, request):
        # see if we can get if from cache. if not, add to queue
        if self.process( request, cache_only = True) == -1:
            self.queue.put(request)



    def process(self, request, cache_only = False):
        #print "abe process", request

        message_id = request['id']
        method = request['method']
        params = request.get('params',[])
        result = None
        error = None

        if method == 'blockchain.numblocks.subscribe':
            result = self.height

        elif method == 'blockchain.headers.subscribe':
            result = self.header

        elif method == 'blockchain.address.subscribe':
            try:
                address = params[0]
                result = self.get_status(address, cache_only)
                self.watch_address(address)
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log( "error:", error )

        elif method == 'blockchain.address.subscribe2':
            try:
                address = params[0]
                result = self.get_status(address, cache_only)
                self.watch_address(address)
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log( "error:", error )

        elif method == 'blockchain.address.get_history2':
            try:
                address = params[0]
                result = self.get_history( address, cache_only )
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log( "error:", error )

        elif method == 'blockchain.block.get_header':
            if cache_only: 
                result = -1
            else:
                try:
                    height = params[0]
                    result = self.get_header( height ) 
                except BaseException, e:
                    error = str(e) + ': %d'% height
                    print_log( "error:", error )
                    
        elif method == 'blockchain.block.get_chunk':
            if cache_only:
                result = -1
            else:
                try:
                    index = params[0]
                    result = self.get_chunk( index ) 
                except BaseException, e:
                    error = str(e) + ': %d'% index
                    print_log( "error:", error)

        elif method == 'blockchain.transaction.broadcast':
            txo = self.bitcoind('sendrawtransaction', params)
            print_log( "sent tx:", txo )
            result = txo 

        elif method == 'blockchain.transaction.get_merkle':
            if cache_only:
                result = -1
            else:
                try:
                    tx_hash = params[0]
                    tx_height = params[1]
                    result = self.get_merkle(tx_hash, tx_height) 
                except BaseException, e:
                    error = str(e) + ': ' + tx_hash
                    print_log( "error:", error )
                    
        elif method == 'blockchain.transaction.get':
            try:
                tx_hash = params[0]
                height = params[1]
                result = self.bitcoind('getrawtransaction', [tx_hash, 0, height] ) 
            except BaseException, e:
                error = str(e) + ': ' + tx_hash
                print_log( "error:", error )

        else:
            error = "unknown method:%s"%method

        if cache_only and result == -1: return -1

        if error:
            response = { 'id':message_id, 'error':error }
            self.push_response(response)
        elif result != '':
            response = { 'id':message_id, 'result':result }
            self.push_response(response)


    def watch_address(self, addr):
        if addr not in self.watched_addresses:
            self.watched_addresses.append(addr)



    def last_hash(self):
        return self.block_hashes[-1]


    def catch_up(self, sync = True):
        t1 = time.time()

        while not self.shared.stopped():

            # are we done yet?
            info = self.bitcoind('getinfo')
            bitcoind_height = info.get('blocks')
            bitcoind_block_hash = self.bitcoind('getblockhash', [bitcoind_height])
            if self.last_hash() == bitcoind_block_hash: 
                self.up_to_date = True
                break

            # not done..
            self.up_to_date = False
            block_hash = self.bitcoind('getblockhash', [self.height+1])
            block = self.bitcoind('getblock', [block_hash, 1])

            if block.get('previousblockhash') == self.last_hash():

                self.import_block(block, block_hash, self.height+1, sync)
                self.height = self.height + 1
                self.write_header(self.block2header(block), sync)

                self.block_hashes.append(block_hash)
                self.block_hashes = self.block_hashes[-10:]

                if (self.height+1)%100 == 0 and not sync: 
                    t2 = time.time()
                    print_log( "catch_up: block %d (%.3fs)"%( self.height, t2 - t1 ) )
                    t1 = t2

                    
            else:
                # revert current block
                print_log( "bc2: reorg", self.height, block.get('previousblockhash'), self.last_hash() )
                block_hash = self.last_hash()
                block = self.bitcoind('getblock', [block_hash, 1])
                self.height = self.height -1
                self.pop_header()

                self.block_hashes.remove(block_hash)
                self.import_block(block, self.last_hash(), self.height, revert=True)
        

        self.header = self.block2header(self.bitcoind('getblock', [self.last_hash()]))

        

            
    def memorypool_update(self):

        mempool_hashes = self.bitcoind('getrawmempool')

        for tx_hash in mempool_hashes:
            if tx_hash in self.mempool_hashes: continue

            tx = self.get_transaction(tx_hash)
            if not tx: continue

            for x in tx.get('inputs'):
                txi = (x.get('prevout_hash') + int_to_hex(x.get('prevout_n'), 4)).decode('hex')
                try:
                    addr = self.db.Get(txi)    
                except:
                    continue
                l = self.mempool_addresses.get(tx_hash, [])
                if addr not in l: 
                    l.append( addr )
                    self.mempool_addresses[tx_hash] = l

            for x in tx.get('outputs'):
                addr = x.get('address')
                l = self.mempool_addresses.get(tx_hash, [])
                if addr not in l: 
                    l.append( addr )
                    self.mempool_addresses[tx_hash] = l

            self.mempool_hashes.append(tx_hash)

        # remove older entries from mempool_hashes
        self.mempool_hashes = mempool_hashes

        # remove deprecated entries from mempool_addresses
        for tx_hash, addresses in self.mempool_addresses.items():
            if tx_hash not in self.mempool_hashes:
                self.mempool_addresses.pop(tx_hash)

        # rebuild histories
        with self.mempool_lock:
            self.mempool_hist = {}
            for tx_hash, addresses in self.mempool_addresses.items():
                for addr in addresses:
                    h = self.mempool_hist.get(addr, [])
                    if tx_hash not in h: 
                        h.append( tx_hash )
                        self.mempool_hist[addr] = h
                        self.invalidate_cache(addr)




    def invalidate_cache(self, address):
        with self.cache_lock:
            if self.history_cache.has_key(address):
                print_log( "cache: invalidating", address )
                self.history_cache.pop(address)



    def main_iteration(self):

        if self.shared.stopped(): 
            print_log( "blockchain processor terminating" )
            return

        with self.dblock:
            t1 = time.time()
            self.catch_up()
            t2 = time.time()

        self.memorypool_update()
        t3 = time.time()
        # print "mempool:", len(self.mempool_addresses), len(self.mempool_hist), "%.3fs"%(t3 - t2)


        if self.sent_height != self.height:
            self.sent_height = self.height
            self.push_response({ 'id': None, 'method':'blockchain.numblocks.subscribe', 'params':[self.height] })

        if self.sent_header != self.header:
            print_log( "blockchain: %d (%.3fs)"%( self.height, t2 - t1 ) )
            self.sent_header = self.header
            self.push_response({ 'id': None, 'method':'blockchain.headers.subscribe', 'params':[self.header] })

        while True:
            try:
                addr = self.address_queue.get(False)
            except:
                break
            if addr in self.watched_addresses:
                status = self.get_status( addr )
                self.push_response({ 'id': None, 'method':'blockchain.address.subscribe', 'params':[addr, status] })


        if not self.shared.stopped(): 
            threading.Timer(10, self.main_iteration).start()
        else:
            print_log( "blockchain processor terminating" )




