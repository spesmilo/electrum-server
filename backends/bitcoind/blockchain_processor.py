import ast
import hashlib
from json import dumps, loads
import os
from Queue import Queue
import random
import sys
import time
import threading
import traceback
import urllib

from backends.bitcoind import deserialize
from processor import Processor, print_log
from utils import *

from storage import Storage


class BlockchainProcessor(Processor):

    def __init__(self, config, shared):
        Processor.__init__(self)

        self.mtimes = {} # monitoring
        self.shared = shared
        self.config = config
        self.up_to_date = False

        self.watch_lock = threading.Lock()
        self.watch_blocks = []
        self.watch_headers = []
        self.watched_addresses = {}

        self.history_cache = {}
        self.chunk_cache = {}
        self.cache_lock = threading.Lock()
        self.headers_data = ''
        self.headers_path = config.get('leveldb', 'path_fulltree')

        self.mempool_values = {}
        self.mempool_addresses = {}
        self.mempool_hist = {}
        self.mempool_hashes = set([])
        self.mempool_lock = threading.Lock()

        self.address_queue = Queue()

        try:
            self.test_reorgs = config.getboolean('leveldb', 'test_reorgs')   # simulate random blockchain reorgs
        except:
            self.test_reorgs = False
        self.storage = Storage(config, shared, self.test_reorgs)

        self.dblock = threading.Lock()

        self.bitcoind_url = 'http://%s:%s@%s:%s/' % (
            config.get('bitcoind', 'user'),
            config.get('bitcoind', 'password'),
            config.get('bitcoind', 'host'),
            config.get('bitcoind', 'port'))

        while True:
            try:
                self.bitcoind('getinfo')
                break
            except:
                print_log('cannot contact bitcoind...')
                time.sleep(5)
                continue

        self.sent_height = 0
        self.sent_header = None

        # catch_up headers
        self.init_headers(self.storage.height)

        threading.Timer(0, lambda: self.catch_up(sync=False)).start()
        while not shared.stopped() and not self.up_to_date:
            try:
                time.sleep(1)
            except:
                print "keyboard interrupt: stopping threads"
                shared.stop()
                sys.exit(0)

        print_log("Blockchain is up to date.")
        self.memorypool_update()
        print_log("Memory pool initialized.")

        self.timer = threading.Timer(10, self.main_iteration)
        self.timer.start()



    def mtime(self, name):
        now = time.time()
        if name != '':
            delta = now - self.now
            t = self.mtimes.get(name, 0)
            self.mtimes[name] = t + delta
        self.now = now

    def print_mtime(self):
        s = ''
        for k, v in self.mtimes.items():
            s += k+':'+"%.2f"%v+' '
        print_log(s)


    def bitcoind(self, method, params=[]):
        postdata = dumps({"method": method, 'params': params, 'id': 'jsonrpc'})
        try:
            respdata = urllib.urlopen(self.bitcoind_url, postdata).read()
        except:
            print_log("error calling bitcoind")
            traceback.print_exc(file=sys.stdout)
            self.shared.stop()

        r = loads(respdata)
        if r['error'] is not None:
            raise BaseException(r['error'])
        return r.get('result')


    def block2header(self, b):
        return {
            "block_height": b.get('height'),
            "version": b.get('version'),
            "prev_block_hash": b.get('previousblockhash'),
            "merkle_root": b.get('merkleroot'),
            "timestamp": b.get('time'),
            "bits": int(b.get('bits'), 16),
            "nonce": b.get('nonce'),
        }

    def get_header(self, height):
        block_hash = self.bitcoind('getblockhash', [height])
        b = self.bitcoind('getblock', [block_hash])
        return self.block2header(b)

    def init_headers(self, db_height):
        self.chunk_cache = {}
        self.headers_filename = os.path.join(self.headers_path, 'blockchain_headers')

        if os.path.exists(self.headers_filename):
            height = os.path.getsize(self.headers_filename)/80 - 1   # the current height
            if height > 0:
                prev_hash = self.hash_header(self.read_header(height))
            else:
                prev_hash = None
        else:
            open(self.headers_filename, 'wb').close()
            prev_hash = None
            height = -1

        if height < db_height:
            print_log("catching up missing headers:", height, db_height)

        try:
            while height < db_height:
                height += 1
                header = self.get_header(height)
                if height > 1:
                    if prev_hash != header.get('prev_block_hash'):
                        # The prev_hash block is orphaned, go back
                        print_log("reorganizing, a block in file is orphaned:", prev_hash)
                        # Go to the parent of the orphaned block
                        height -= 2
                        prev_hash = self.hash_header(self.read_header(height))
                        continue

                self.write_header(header, sync=False)
                prev_hash = self.hash_header(header)
                if (height % 1000) == 0:
                    print_log("headers file:", height)
        except KeyboardInterrupt:
            self.flush_headers()
            sys.exit()

        self.flush_headers()

    def hash_header(self, header):
        return rev_hex(Hash(header_to_string(header).decode('hex')).encode('hex'))

    def read_header(self, block_height):
        if os.path.exists(self.headers_filename):
            with open(self.headers_filename, 'rb') as f:
                f.seek(block_height * 80)
                h = f.read(80)
            if len(h) == 80:
                h = header_from_string(h)
                return h

    def read_chunk(self, index):
        with open(self.headers_filename, 'rb') as f:
            f.seek(index*2016*80)
            chunk = f.read(2016*80)
        return chunk.encode('hex')

    def write_header(self, header, sync=True):
        if not self.headers_data:
            self.headers_offset = header.get('block_height')

        self.headers_data += header_to_string(header).decode('hex')
        if sync or len(self.headers_data) > 40*100:
            self.flush_headers()

        with self.cache_lock:
            chunk_index = header.get('block_height')/2016
            if self.chunk_cache.get(chunk_index):
                self.chunk_cache.pop(chunk_index)

    def pop_header(self):
        # we need to do this only if we have not flushed
        if self.headers_data:
            self.headers_data = self.headers_data[:-40]

    def flush_headers(self):
        if not self.headers_data:
            return
        with open(self.headers_filename, 'rb+') as f:
            f.seek(self.headers_offset*80)
            f.write(self.headers_data)
        self.headers_data = ''

    def get_chunk(self, i):
        # store them on disk; store the current chunk in memory
        with self.cache_lock:
            chunk = self.chunk_cache.get(i)
            if not chunk:
                chunk = self.read_chunk(i)
                self.chunk_cache[i] = chunk

        return chunk

    def get_mempool_transaction(self, txid):
        try:
            raw_tx = self.bitcoind('getrawtransaction', [txid, 0])
        except:
            return None

        vds = deserialize.BCDataStream()
        vds.write(raw_tx.decode('hex'))
        try:
            return deserialize.parse_Transaction(vds, is_coinbase=False)
        except:
            print_log("ERROR: cannot parse", txid)
            return None


    def get_history(self, addr, cache_only=False):
        with self.cache_lock:
            hist = self.history_cache.get(addr)
        if hist is not None:
            return hist
        if cache_only:
            return -1

        with self.dblock:
            try:
                hist = self.storage.get_history(addr)
                is_known = True
            except:
                print_log("error get_history")
                traceback.print_exc(file=sys.stdout)
                raise
            if hist:
                is_known = True
            else:
                hist = []
                is_known = False

        # add memory pool
        with self.mempool_lock:
            for txid, delta in self.mempool_hist.get(addr, []):
                hist.append({'tx_hash':txid, 'height':0})

        # add something to distinguish between unused and empty addresses
        if hist == [] and is_known:
            hist = ['*']

        with self.cache_lock:
            self.history_cache[addr] = hist
        return hist


    def get_unconfirmed_value(self, addr):
        v = 0
        with self.mempool_lock:
            for txid, delta in self.mempool_hist.get(addr, []):
                v += delta
        return v


    def get_status(self, addr, cache_only=False):
        tx_points = self.get_history(addr, cache_only)
        if cache_only and tx_points == -1:
            return -1

        if not tx_points:
            return None
        if tx_points == ['*']:
            return '*'
        status = ''
        for tx in tx_points:
            status += tx.get('tx_hash') + ':%d:' % tx.get('height')
        return hashlib.sha256(status).digest().encode('hex')

    def get_merkle(self, tx_hash, height):

        block_hash = self.bitcoind('getblockhash', [height])
        b = self.bitcoind('getblock', [block_hash])
        tx_list = b.get('tx')
        tx_pos = tx_list.index(tx_hash)

        merkle = map(hash_decode, tx_list)
        target_hash = hash_decode(tx_hash)
        s = []
        while len(merkle) != 1:
            if len(merkle) % 2:
                merkle.append(merkle[-1])
            n = []
            while merkle:
                new_hash = Hash(merkle[0] + merkle[1])
                if merkle[0] == target_hash:
                    s.append(hash_encode(merkle[1]))
                    target_hash = new_hash
                elif merkle[1] == target_hash:
                    s.append(hash_encode(merkle[0]))
                    target_hash = new_hash
                n.append(new_hash)
                merkle = merkle[2:]
            merkle = n

        return {"block_height": height, "merkle": s, "pos": tx_pos}


    def add_to_history(self, addr, tx_hash, tx_pos, tx_height):
        # keep it sorted
        s = self.serialize_item(tx_hash, tx_pos, tx_height) + 40*chr(0)
        assert len(s) == 80

        serialized_hist = self.batch_list[addr]

        l = len(serialized_hist)/80
        for i in range(l-1, -1, -1):
            item = serialized_hist[80*i:80*(i+1)]
            item_height = int(rev_hex(item[36:39].encode('hex')), 16)
            if item_height <= tx_height:
                serialized_hist = serialized_hist[0:80*(i+1)] + s + serialized_hist[80*(i+1):]
                break
        else:
            serialized_hist = s + serialized_hist

        self.batch_list[addr] = serialized_hist

        # backlink
        txo = (tx_hash + int_to_hex(tx_pos, 4)).decode('hex')
        self.batch_txio[txo] = addr






    def deserialize_block(self, block):
        txlist = block.get('tx')
        tx_hashes = []  # ordered txids
        txdict = {}     # deserialized tx
        is_coinbase = True
        for raw_tx in txlist:
            tx_hash = hash_encode(Hash(raw_tx.decode('hex')))
            vds = deserialize.BCDataStream()
            vds.write(raw_tx.decode('hex'))
            try:
                tx = deserialize.parse_Transaction(vds, is_coinbase)
            except:
                print_log("ERROR: cannot parse", tx_hash)
                continue
            tx_hashes.append(tx_hash)
            txdict[tx_hash] = tx
            is_coinbase = False
        return tx_hashes, txdict



    def import_block(self, block, block_hash, block_height, sync, revert=False):

        touched_addr = set([])

        # deserialize transactions
        tx_hashes, txdict = self.deserialize_block(block)

        # undo info
        if revert:
            undo_info = self.storage.get_undo_info(block_height)
            tx_hashes.reverse()
        else:
            undo_info = {}

        for txid in tx_hashes:  # must be ordered
            tx = txdict[txid]
            if not revert:
                undo = self.storage.import_transaction(txid, tx, block_height, touched_addr)
                undo_info[txid] = undo
            else:
                undo = undo_info.pop(txid)
                self.storage.revert_transaction(txid, tx, block_height, touched_addr, undo)

        if revert: 
            assert undo_info == {}

        # add undo info
        if not revert:
            self.storage.write_undo_info(block_height, self.bitcoind_height, undo_info)

        # add the max
        self.storage.db_undo.put('height', repr( (block_hash, block_height, self.storage.db_version) ))

        for addr in touched_addr:
            self.invalidate_cache(addr)

        self.storage.update_hashes()


    def add_request(self, session, request):
        # see if we can get if from cache. if not, add to queue
        if self.process(session, request, cache_only=True) == -1:
            self.queue.put((session, request))


    def do_subscribe(self, method, params, session):
        with self.watch_lock:
            if method == 'blockchain.numblocks.subscribe':
                if session not in self.watch_blocks:
                    self.watch_blocks.append(session)

            elif method == 'blockchain.headers.subscribe':
                if session not in self.watch_headers:
                    self.watch_headers.append(session)

            elif method == 'blockchain.address.subscribe':
                address = params[0]
                l = self.watched_addresses.get(address)
                if l is None:
                    self.watched_addresses[address] = [session]
                elif session not in l:
                    l.append(session)


    def do_unsubscribe(self, method, params, session):
        with self.watch_lock:
            if method == 'blockchain.numblocks.subscribe':
                if session in self.watch_blocks:
                    self.watch_blocks.remove(session)
            elif method == 'blockchain.headers.subscribe':
                if session in self.watch_headers:
                    self.watch_headers.remove(session)
            elif method == "blockchain.address.subscribe":
                addr = params[0]
                l = self.watched_addresses.get(addr)
                if not l:
                    return
                if session in l:
                    l.remove(session)
                if session in l:
                    print_log("error rc!!")
                    self.shared.stop()
                if l == []:
                    self.watched_addresses.pop(addr)


    def process(self, session, request, cache_only=False):
        
        message_id = request['id']
        method = request['method']
        params = request.get('params', [])
        result = None
        error = None

        if method == 'blockchain.numblocks.subscribe':
            result = self.storage.height

        elif method == 'blockchain.headers.subscribe':
            result = self.header

        elif method == 'blockchain.address.subscribe':
            try:
                address = str(params[0])
                result = self.get_status(address, cache_only)
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log("error:", error)

        elif method == 'blockchain.address.get_history':
            try:
                address = str(params[0])
                result = self.get_history(address, cache_only)
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log("error:", error)

        elif method == 'blockchain.address.get_mempool':
            try:
                address = str(params[0])
                result = self.get_unconfirmed_history(address, cache_only)
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log("error:", error)

        elif method == 'blockchain.address.get_balance':
            try:
                address = str(params[0])
                confirmed = self.storage.get_balance(address)
                unconfirmed = self.get_unconfirmed_value(address)
                result = { 'confirmed':confirmed, 'unconfirmed':unconfirmed }
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log("error:", error)

        elif method == 'blockchain.address.get_proof':
            try:
                address = str(params[0])
                result = self.storage.get_proof(address)
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log("error:", error)

        elif method == 'blockchain.address.listunspent':
            try:
                address = str(params[0])
                result = self.storage.listunspent(address)
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log("error:", error)

        elif method == 'blockchain.utxo.get_address':
            try:
                txid = str(params[0])
                pos = int(params[1])
                txi = (txid + int_to_hex(pos, 4)).decode('hex')
                result = self.storage.get_address(txi)
            except BaseException, e:
                error = str(e)
                print_log("error:", error, params)

        elif method == 'blockchain.block.get_header':
            if cache_only:
                result = -1
            else:
                try:
                    height = int(params[0])
                    result = self.get_header(height)
                except BaseException, e:
                    error = str(e) + ': %d' % height
                    print_log("error:", error)

        elif method == 'blockchain.block.get_chunk':
            if cache_only:
                result = -1
            else:
                try:
                    index = int(params[0])
                    result = self.get_chunk(index)
                except BaseException, e:
                    error = str(e) + ': %d' % index
                    print_log("error:", error)

        elif method == 'blockchain.transaction.broadcast':
            try:
                txo = self.bitcoind('sendrawtransaction', params)
                print_log("sent tx:", txo)
                result = txo
            except BaseException, e:
                result = str(e)  # do not send an error
                print_log("error:", result, params)

        elif method == 'blockchain.transaction.get_merkle':
            if cache_only:
                result = -1
            else:
                try:
                    tx_hash = params[0]
                    tx_height = params[1]
                    result = self.get_merkle(tx_hash, tx_height)
                except BaseException, e:
                    error = str(e) + ': ' + repr(params)
                    print_log("get_merkle error:", error)

        elif method == 'blockchain.transaction.get':
            try:
                tx_hash = params[0]
                result = self.bitcoind('getrawtransaction', [tx_hash, 0])
            except BaseException, e:
                error = str(e) + ': ' + repr(params)
                print_log("tx get error:", error)

        else:
            error = "unknown method:%s" % method

        if cache_only and result == -1:
            return -1

        if error:
            self.push_response(session, {'id': message_id, 'error': error})
        elif result != '':
            self.push_response(session, {'id': message_id, 'result': result})


    def getfullblock(self, block_hash):
        block = self.bitcoind('getblock', [block_hash])

        rawtxreq = []
        i = 0
        for txid in block['tx']:
            rawtxreq.append({
                "method": "getrawtransaction",
                "params": [txid],
                "id": i,
            })
            i += 1

        postdata = dumps(rawtxreq)
        try:
            respdata = urllib.urlopen(self.bitcoind_url, postdata).read()
        except:
            print_log("bitcoind error (getfullblock)")
            traceback.print_exc(file=sys.stdout)
            self.shared.stop()

        r = loads(respdata)
        rawtxdata = []
        for ir in r:
            if ir['error'] is not None:
                self.shared.stop()
                print_log("Error: make sure you run bitcoind with txindex=1; use -reindex if needed.")
                raise BaseException(ir['error'])
            rawtxdata.append(ir['result'])
        block['tx'] = rawtxdata
        return block

    def catch_up(self, sync=True):

        prev_root_hash = None
        while not self.shared.stopped():

            self.mtime('')

            # are we done yet?
            info = self.bitcoind('getinfo')
            self.bitcoind_height = info.get('blocks')
            bitcoind_block_hash = self.bitcoind('getblockhash', [self.bitcoind_height])
            if self.storage.last_hash == bitcoind_block_hash:
                self.up_to_date = True
                break

            # fixme: this is unsafe, if we revert when the undo info is not yet written
            revert = (random.randint(1, 100) == 1) if self.test_reorgs else False

            # not done..
            self.up_to_date = False
            try:
                next_block_hash = self.bitcoind('getblockhash', [self.storage.height + 1])
                next_block = self.getfullblock(next_block_hash)
            except BaseException, e:
                revert = True
                next_block = self.getfullblock(self.storage.last_hash)

            self.mtime('daemon')

            if (next_block.get('previousblockhash') == self.storage.last_hash) and not revert:

                prev_root_hash = self.storage.get_root_hash()

                self.import_block(next_block, next_block_hash, self.storage.height+1, sync)
                self.storage.height = self.storage.height + 1
                self.write_header(self.block2header(next_block), sync)
                self.storage.last_hash = next_block_hash
                self.mtime('import')
            
                if self.storage.height % 1000 == 0 and not sync:
                    t_daemon = self.mtimes.get('daemon')
                    t_import = self.mtimes.get('import')
                    print_log("catch_up: block %d (%.3fs %.3fs)" % (self.storage.height, t_daemon, t_import), self.storage.get_root_hash().encode('hex'))
                    self.mtimes['daemon'] = 0
                    self.mtimes['import'] = 0

            else:

                # revert current block
                block = self.getfullblock(self.storage.last_hash)
                print_log("blockchain reorg", self.storage.height, block.get('previousblockhash'), self.storage.last_hash)
                self.import_block(block, self.storage.last_hash, self.storage.height, sync, revert=True)
                self.pop_header()
                self.flush_headers()

                self.storage.height -= 1

                # read previous header from disk
                self.header = self.read_header(self.storage.height)
                self.storage.last_hash = self.hash_header(self.header)

                if prev_root_hash:
                    assert prev_root_hash == self.storage.get_root_hash()
                    prev_root_hash = None


        self.header = self.block2header(self.bitcoind('getblock', [self.storage.last_hash]))
        self.header['utxo_root'] = self.storage.get_root_hash().encode('hex')

        if self.shared.stopped(): 
            print_log( "closing database" )
            self.storage.close()


    def memorypool_update(self):
        mempool_hashes = set(self.bitcoind('getrawmempool'))
        touched_addresses = set([])

        # get new transactions
        new_tx = {}
        for tx_hash in mempool_hashes:
            if tx_hash in self.mempool_hashes:
                continue

            tx = self.get_mempool_transaction(tx_hash)
            if not tx:
                continue

            new_tx[tx_hash] = tx
            self.mempool_hashes.add(tx_hash)

        # remove older entries from mempool_hashes
        self.mempool_hashes = mempool_hashes


        # check all tx outputs
        for tx_hash, tx in new_tx.items():
            mpa = self.mempool_addresses.get(tx_hash, {})
            out_values = []
            for x in tx.get('outputs'):
                out_values.append( x['value'] )

                addr = x.get('address')
                if not addr:
                    continue
                v = mpa.get(addr,0)
                v += x['value']
                mpa[addr] = v
                touched_addresses.add(addr)

            self.mempool_addresses[tx_hash] = mpa
            self.mempool_values[tx_hash] = out_values

        # check all inputs
        for tx_hash, tx in new_tx.items():
            mpa = self.mempool_addresses.get(tx_hash, {})
            for x in tx.get('inputs'):
                # we assume that the input address can be parsed by deserialize(); this is true for Electrum transactions
                addr = x.get('address')
                if not addr:
                    continue

                v = self.mempool_values.get(x.get('prevout_hash'))
                if v:
                    value = v[ x.get('prevout_n')]
                else:
                    txi = (x.get('prevout_hash') + int_to_hex(x.get('prevout_n'), 4)).decode('hex')
                    try:
                        value = self.storage.get_utxo_value(addr,txi)
                    except:
                        print_log("utxo not in database; postponing mempool update")
                        return

                v = mpa.get(addr,0)
                v -= value
                mpa[addr] = v
                touched_addresses.add(addr)

            self.mempool_addresses[tx_hash] = mpa


        # remove deprecated entries from mempool_addresses
        for tx_hash, addresses in self.mempool_addresses.items():
            if tx_hash not in self.mempool_hashes:
                self.mempool_addresses.pop(tx_hash)
                self.mempool_values.pop(tx_hash)
                for addr in addresses:
                    touched_addresses.add(addr)

        # rebuild mempool histories
        new_mempool_hist = {}
        for tx_hash, addresses in self.mempool_addresses.items():
            for addr, delta in addresses.items():
                h = new_mempool_hist.get(addr, [])
                if tx_hash not in h:
                    h.append((tx_hash, delta))
                new_mempool_hist[addr] = h

        with self.mempool_lock:
            self.mempool_hist = new_mempool_hist

        # invalidate cache for touched addresses
        for addr in touched_addresses:
            self.invalidate_cache(addr)


    def invalidate_cache(self, address):
        with self.cache_lock:
            if address in self.history_cache:
                print_log("cache: invalidating", address)
                self.history_cache.pop(address)

        with self.watch_lock:
            sessions = self.watched_addresses.get(address)

        if sessions:
            # TODO: update cache here. if new value equals cached value, do not send notification
            self.address_queue.put((address,sessions))

    
    def close(self):
        self.timer.join()
        print_log("Closing database...")
        self.storage.close()
        print_log("Database is closed")


    def main_iteration(self):
        if self.shared.stopped():
            print_log("Stopping timer")
            return

        with self.dblock:
            t1 = time.time()
            self.catch_up()
            t2 = time.time()

        self.memorypool_update()

        if self.sent_height != self.storage.height:
            self.sent_height = self.storage.height
            for session in self.watch_blocks:
                self.push_response(session, {
                        'id': None,
                        'method': 'blockchain.numblocks.subscribe',
                        'params': [self.storage.height],
                        })

        if self.sent_header != self.header:
            print_log("blockchain: %d (%.3fs)" % (self.storage.height, t2 - t1))
            self.sent_header = self.header
            for session in self.watch_headers:
                self.push_response(session, {
                        'id': None,
                        'method': 'blockchain.headers.subscribe',
                        'params': [self.header],
                        })

        while True:
            try:
                addr, sessions = self.address_queue.get(False)
            except:
                break

            status = self.get_status(addr)
            for session in sessions:
                self.push_response(session, {
                        'id': None,
                        'method': 'blockchain.address.subscribe',
                        'params': [addr, status],
                        })

        # next iteration 
        self.timer = threading.Timer(10, self.main_iteration)
        self.timer.start()

