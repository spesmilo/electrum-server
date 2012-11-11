from json import dumps, loads
import leveldb, urllib
import deserialize
import ast, time, threading, hashlib
from Queue import Queue


def rev_hex(s):
    return s.decode('hex')[::-1].encode('hex')


def int_to_hex(i, length=1):
    s = hex(i)[2:].rstrip('L')
    s = "0"*(2*length - len(s)) + s
    return rev_hex(s)


from processor import Processor, print_log


class Blockchain2Processor(Processor):

    def __init__(self, config):
        Processor.__init__(self)

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

        # catch_up first
        try:
            hist = self.deserialize(self.db.Get('0'))
            hh, self.height = hist[0] 
            self.block_hashes = [hh]
            print_log( "hist", hist )
        except:
            traceback.print_exc(file=sys.stdout)
            self.height = 0
            self.block_hashes = [ '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f' ]

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
        for txid, height in h:
            s += txid + int_to_hex(height, 4)
        return s.decode('hex')

    def deserialize(self, s):
        h = []
        while s:
            txid = s[0:32].encode('hex')
            height = s[32:36].encode('hex')
            height = int( rev_hex( height ), 16 )
            h.append( ( txid, height ) )
            s = s[36:]
        return h


    def block2header(self, b):
        return {"block_height":b.get('height'), "version":b.get('version'), "prev_block_hash":b.get('previousblockhash'), 
                "merkle_root":b.get('merkleroot'), "timestamp":b.get('time'), "bits":b.get('bits'), "nonce":b.get('nonce')}

    def get_header(self, height):
        block_hash = self.bitcoind('getblockhash', [height])
        b = self.bitcoind('getblock', [block_hash])
        return self.block2header(b)
    

    def get_chunk(self):
        # store them on disk; store the current chunk in memory
        pass


    def get_transaction(self, txid, block_height=-1):
        raw_tx = self.bitcoind('getrawtransaction', [txid, 0, block_height])
        vds = deserialize.BCDataStream()
        vds.write(raw_tx.decode('hex'))
        return deserialize.parse_Transaction(vds)


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

        hist = map(lambda x: {'tx_hash':x[0], 'height':x[1]}, hist)
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

        


    def import_block(self, block, block_hash, block_height):
        #print "importing block", block_hash, block_height

        txlist = block.get('tx')
        batch_list = {}

        for txid in txlist:
            tx = self.get_transaction(txid, block_height)
            for x in tx.get('inputs') + tx.get('outputs'):
                addr = x.get('address')
                serialized_hist = batch_list.get(addr)
                if serialized_hist is None:
                    try:
                        serialized_hist = self.db.Get(addr)
                    except: 
                        serialized_hist = ''

                s = (txid + int_to_hex(block_height, 4)).decode('hex')

                found = False
                for i in range(len(serialized_hist)/36):
                    item = serialized_hist[-36*(1+i):]
                    item = item[0:36]

                    h = int( rev_hex( item[32:36].encode('hex') ), 16 )
                    if h > block_height:
                        txhash = item[0:32].encode('hex')
                        print_log('warning: non-chronological order at', addr, (txhash, h), (txid, block_height))
                        hist = self.deserialize(serialized_hist)
                        print_log(hist)
                        hist.sort( key=lambda tup: tup[1])
                        while hist:
                            last = hist[-1]
                            if last[1] > block_height:
                                hist = hist[0:-1]
                            else:
                                break
                        found = (txhash, h) in hist
                        print_log('new sorted hist', hist, found)
                        serialized_hist = self.serialize(hist)
                        break
                    elif h < block_height:
                        break
                    elif item == s:
                        found = True
                        break

                if not found:
                    serialized_hist += s

                batch_list[addr] = serialized_hist

        # batch write
        batch = leveldb.WriteBatch()
        for addr, hist in batch_list.items():
            batch.Put(addr, serialized_hist)
        batch.Put('0', self.serialize( [(block_hash, block_height)] ) )
        self.db.Write(batch, sync = True)

        # invalidate cache
        for addr in batch_list.keys(): self.update_history_cache(addr)

        return len(txlist)



    def revert_block(self, block, block_hash, block_height):

        txlist = block.get('tx')
        batch_list = {}

        for txid in txlist:
            tx = self.get_transaction(txid, block_height)
            for x in tx.get('inputs') + tx.get('outputs'):

                addr = x.get('address')

                hist = batch_list.get(addr)
                if hist is None:
                    try:
                        hist = self.deserialize(self.db.Get(addr))
                    except: 
                        hist = []

                if (txid, block_height) in hist:
                    hist.remove( (txid, block_height) )
                else:
                    print "error: txid not found during block revert", txid, block_height

                batch_list[addr] = hist

        # batch write
        batch = leveldb.WriteBatch()
        for addr, hist in batch_list.items():
            batch.Put(addr, self.serialize(hist))
        batch.Put('0', self.serialize( [(block_hash, block_height)] ) )
        self.db.Write(batch, sync = True)

        # invalidate cache
        for addr in batch_list.keys(): self.update_history_cache(addr)

        return len(txlist)



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

        if method == 'blockchain2.numblocks.subscribe':
            result = self.height

        elif method == 'blockchain2.headers.subscribe':
            result = self.header

        elif method == 'blockchain2.address.subscribe':
            try:
                address = params[0]
                result = self.get_status(address, cache_only)
                self.watch_address(address)
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log( "error:", error )

        elif method == 'blockchain2.address.subscribe2':
            try:
                address = params[0]
                result = self.get_status2(address, cache_only)
                self.watch_address(address)
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log( "error:", error )

        elif method == 'blockchain2.address.get_history':
            try:
                address = params[0]
                result = self.get_history( address, cache_only )
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log( "error:", error )

        elif method == 'blockchain2.block.get_header':
            if cache_only: 
                result = -1
            else:
                try:
                    height = params[0]
                    result = self.get_header( height ) 
                except BaseException, e:
                    error = str(e) + ': %d'% height
                    print_log( "error:", error )
                    
        elif method == 'blockchain2.block.get_chunk':
            if cache_only:
                result = -1
            else:
                try:
                    index = params[0]
                    result = self.get_chunk( index ) 
                except BaseException, e:
                    error = str(e) + ': %d'% index
                    print_log( "error:", error)

        elif method == 'blockchain2.transaction.broadcast':
            txo = self.bitcoind('sendrawtransaction', params[0])
            print_log( "sent tx:", txo )
            result = txo 

        elif method == 'blockchain2.transaction.get_merkle':
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
                    
        elif method == 'blockchain2.transaction.get':
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


    def catch_up(self):

        t1 = time.time()

        while not self.shared.stopped():

            # are we done yet?
            info = self.bitcoind('getinfo')
            bitcoind_height = info.get('blocks')
            bitcoind_block_hash = self.bitcoind('getblockhash', [bitcoind_height])
            if self.last_hash() == bitcoind_block_hash: break

            # not done..
            block_hash = self.bitcoind('getblockhash', [self.height+1])
            block = self.bitcoind('getblock', [block_hash])

            if block.get('previousblockhash') == self.last_hash():

                self.import_block(block, block_hash, self.height+1)

                if (self.height+1)%100 == 0: 
                    t2 = time.time()
                    print_log( "bc2: block %d (%.3fs)"%( self.height+1, t2 - t1 ) )
                    t1 = t2

                self.height = self.height + 1
                self.block_hashes.append(block_hash)
                self.block_hashes = self.block_hashes[-10:]
                    
            else:
                # revert current block
                print_log( "bc2: reorg", self.height, block.get('previousblockhash'), self.last_hash() )
                block_hash = self.last_hash()
                block = self.bitcoind('getblock', [block_hash])
                self.height = self.height -1
                self.block_hashes.remove(block_hash)
                self.revert_block(block, self.last_hash(), self.height)
        

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
            print_log( "bc2 terminating")
            return

        with self.dblock:
            t1 = time.time()
            self.catch_up()
            t2 = time.time()
            print_log( "blockchain: %d (%.3fs)"%( self.height+1, t2 - t1 ) )
        self.memorypool_update()

        if self.sent_height != self.height:
            self.sent_height = self.height
            self.push_response({ 'id': None, 'method':'blockchain2.numblocks.subscribe', 'params':[self.height] })

        if self.sent_header != self.header:
            self.sent_header = self.header
            self.push_response({ 'id': None, 'method':'blockchain2.headers.subscribe', 'params':[self.header] })

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
            print_log( "bc2 terminating" )




