from json import dumps, loads
import leveldb, urllib
import deserialize
import ast, time, threading, hashlib
from Queue import Queue
import traceback, sys



Hash = lambda x: hashlib.sha256(hashlib.sha256(x).digest()).digest()
hash_encode = lambda x: x[::-1].encode('hex')
hash_decode = lambda x: x.decode('hex')[::-1]



def rev_hex(s):
    return s.decode('hex')[::-1].encode('hex')


def int_to_hex(i, length=1):
    s = hex(i)[2:].rstrip('L')
    s = "0"*(2*length - len(s)) + s
    return rev_hex(s)


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

        self.mempool_hist = {}
        self.known_mempool_hashes = []
        self.address_queue = Queue()

        self.dblock = threading.Lock()
        try:
            self.db = leveldb.LevelDB(config.get('leveldb', 'path'))
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

        # catch_up first
        threading.Timer(0, lambda: self.catch_up(sync=False)).start()
        while not shared.stopped() and not self.up_to_date:
            try:
                time.sleep(1)
            except:
                print "keyboard interrupt: stopping threads"
                shared.stop()
                sys.exit(0)



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
    

    def get_chunk(self):
        # store them on disk; store the current chunk in memory
        pass


    def get_transaction(self, txid, block_height=-1, is_coinbase = False):
        t0 = time.time()
        raw_tx = self.bitcoind('getrawtransaction', [txid, 0, block_height])
        t1 = time.time()
        vds = deserialize.BCDataStream()
        vds.write(raw_tx.decode('hex'))
        out = deserialize.parse_Transaction(vds, is_coinbase)
        t2 = time.time()
        return out, t1 - t0, t2 - t1


    def get_history(self, addr, cache_only=False):
        with self.cache_lock: hist = self.history_cache.get( addr )
        if hist is not None: return hist
        if cache_only: return -1

        with self.dblock:
            try:
                hist = self.deserialize(self.db.Get(addr))
            except: 
                hist = []

        # should not be necessary
        hist.sort( key=lambda tup: tup[1])
        # check uniqueness too...

        # add memory pool
        for txid in self.mempool_hist.get(addr,[]):
            hist.append((txid, 0))

        hist = map(lambda x: {'tx_hash':x[0], 'height':x[2]}, hist)
        with self.cache_lock: self.history_cache[addr] = hist
        return hist


    def get_status(self, addr, cache_only=False):
        tx_points = self.get_history(addr, cache_only)
        if cache_only and tx_points == -1: return -1

        if not tx_points: return None
        status = ''
        for tx in tx_points:
            status += tx.get('tx_hash') + ':%d:' % tx.get('height')
        return hashlib.sha256( status ).digest().encode('hex')


    def get_merkle(self, target_hash, height):

        block_hash = self.bitcoind('getblockhash', [height])
        b = self.bitcoind('getblock', [block_hash])
        merkle = b.get('tx')

        s = []
        while len(merkle) != 1:
            if len(merkle)%2: merkle.append( merkle[-1] )
            n = []
            while merkle:
                new_hash = Hash( merkle[0] + merkle[1] )
                if merkle[0] == target_hash:
                    s.append( merkle[1])
                    target_hash = new_hash
                elif merkle[1] == target_hash:
                    s.append( merkle[0])
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

        # invalidate cache
        for addr in self.batch_list.keys(): self.update_history_cache(addr)



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
            txo = self.bitcoind('sendrawtransaction', params[0])
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

                if (self.height+1)%100 == 0 and not sync: 
                    t2 = time.time()
                    print_log( "catch_up: block %d (%.3fs)"%( self.height+1, t2 - t1 ) )
                    t1 = t2

                self.height = self.height + 1
                self.block_hashes.append(block_hash)
                self.block_hashes = self.block_hashes[-10:]
                    
            else:
                # revert current block
                print_log( "bc2: reorg", self.height, block.get('previousblockhash'), self.last_hash() )
                block_hash = self.last_hash()
                block = self.bitcoind('getblock', [block_hash, 1])
                self.height = self.height -1
                self.block_hashes.remove(block_hash)
                self.import_block(block, self.last_hash(), self.height, revert=True)
        

        self.header = self.block2header(self.bitcoind('getblock', [self.last_hash()]))

        

            
    def memorypool_update(self):

        mempool_hashes = self.bitcoind('getrawmempool')

        for tx_hash in mempool_hashes:
            if tx_hash in self.known_mempool_hashes: continue
            self.known_mempool_hashes.append(tx_hash)

            tx = self.get_transaction(tx_hash)
            if not tx: continue

            for x in tx.get('inputs') + tx.get('outputs'):
                addr = x.get('address')
                hist = self.mempool_hist.get(addr, [])
                if tx_hash not in hist: 
                    hist.append( tx_hash )
                    self.mempool_hist[addr] = hist
                    self.update_history_cache(addr)

        self.known_mempool_hashes = mempool_hashes


    def update_history_cache(self, address):
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
            print_log( "blockchain: %d (%.3fs)"%( self.height+1, t2 - t1 ) )
        self.memorypool_update()

        if self.sent_height != self.height:
            self.sent_height = self.height
            self.push_response({ 'id': None, 'method':'blockchain.numblocks.subscribe', 'params':[self.height] })

        if self.sent_header != self.header:
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




