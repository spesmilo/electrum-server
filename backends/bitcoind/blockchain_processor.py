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


class BlockchainProcessor(Processor):

    def __init__(self, config, shared):
        Processor.__init__(self)

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

        self.mempool_addresses = {}
        self.mempool_hist = {}
        self.mempool_hashes = set([])
        self.mempool_lock = threading.Lock()

        self.address_queue = Queue()

        try:
            self.use_plyvel = config.getboolean('leveldb', 'use_plyvel')
        except:
            self.use_plyvel = False
        print_log('use_plyvel:', self.use_plyvel)

        # don't use the same database for plyvel, because python-leveldb uses snappy compression
        self.dbpath = config.get('leveldb', 'path_plyvel' if self.use_plyvel else 'path')

        self.pruning_limit = config.getint('leveldb', 'pruning_limit')
        self.db_version = 1 # increase this when database needs to be updated

        self.dblock = threading.Lock()
        try:
            if self.use_plyvel:
                import plyvel
                self.db = plyvel.DB(self.dbpath, create_if_missing=True, paranoid_checks=None, compression=None)
            else:
                import leveldb
                self.db = leveldb.LevelDB(self.dbpath, paranoid_checks=False)
        except:
            traceback.print_exc(file=sys.stdout)
            self.shared.stop()

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

        self.height = 0
        self.is_test = False
        self.sent_height = 0
        self.sent_header = None

        try:
            hist = self.deserialize(self.db_get('height'))
            self.last_hash, self.height, db_version = hist[0]
            print_log("Database version", self.db_version)
            print_log("Blockchain height", self.height)
        except:
            traceback.print_exc(file=sys.stdout)
            print_log('initializing database')
            self.height = 0
            self.last_hash = '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f'
            db_version = self.db_version

        # check version
        if self.db_version != db_version:
            print_log("Your database '%s' is deprecated. Please create a new database"%self.dbpath)
            self.shared.stop()
            return

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

        print_log("Blockchain is up to date.")
        self.memorypool_update()
        print_log("Memory pool initialized.")

        threading.Timer(10, self.main_iteration).start()


    def db_get(self, key):
        if self.use_plyvel:
            return self.db.get(key) 
        else:
            try:
                return self.db.Get(key)
            except KeyError:
                return None

    def batch_put(self, batch, key, value):
        if self.use_plyvel:
            batch.put(key, value)
        else:
            batch.Put(key, value)

    def batch_delete(self, batch, key):
        if self.use_plyvel:
            batch.delete(key)
        else:
            batch.Delete(key)

    def batch_write(self, batch, sync):
        if self.use_plyvel:
            batch.write()#, sync=sync)
        else:
            self.db.Write(batch, sync=sync)


    def bitcoind(self, method, params=[]):
        postdata = dumps({"method": method, 'params': params, 'id': 'jsonrpc'})
        try:
            respdata = urllib.urlopen(self.bitcoind_url, postdata).read()
        except:
            traceback.print_exc(file=sys.stdout)
            self.shared.stop()

        r = loads(respdata)
        if r['error'] is not None:
            raise BaseException(r['error'])
        return r.get('result')

    def serialize(self, h):
        s = ''
        for txid, txpos, height in h:
            s += self.serialize_item(txid, txpos, height)
        return s

    def serialize_item(self, txid, txpos, height, spent=chr(0)):
        s = (txid + int_to_hex(txpos, 4) + int_to_hex(height, 3)).decode('hex') + spent 
        return s

    def deserialize_item(self,s):
        txid = s[0:32].encode('hex')
        txpos = int(rev_hex(s[32:36].encode('hex')), 16)
        height = int(rev_hex(s[36:39].encode('hex')), 16)
        spent = s[39:40]
        return (txid, txpos, height, spent)

    def deserialize(self, s):
        h = []
        while s:
            txid, txpos, height, spent = self.deserialize_item(s[0:40])
            h.append((txid, txpos, height))
            if spent == chr(1):
                txid, txpos, height, spent = self.deserialize_item(s[40:80])
                h.append((txid, txpos, height))
            s = s[80:]
        return h

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
        self.headers_filename = os.path.join(self.dbpath, 'blockchain_headers')

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
                height = height + 1
                header = self.get_header(height)
                if height > 1:
                    assert prev_hash == header.get('prev_block_hash')
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
                hist = self.deserialize(self.db_get(str((addr))))
                is_known = True
            except:
                self.shared.stop()
                raise
            if hist:
                is_known = True
            else:
                hist = []
                is_known = False

        # sort history, because redeeming transactions are next to the corresponding txout
        hist.sort(key=lambda tup: tup[2])

        # add memory pool
        with self.mempool_lock:
            for txid in self.mempool_hist.get(addr, []):
                hist.append((txid, 0, 0))

        # uniqueness
        hist = set(map(lambda x: (x[0], x[2]), hist))

        # convert to dict
        hist = map(lambda x: {'tx_hash': x[0], 'height': x[1]}, hist)

        # add something to distinguish between unused and empty addresses
        if hist == [] and is_known:
            hist = ['*']

        with self.cache_lock:
            self.history_cache[addr] = hist
        return hist

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



    def revert_add_to_history(self, addr, tx_hash, tx_pos, tx_height):

        serialized_hist = self.batch_list[addr]
        s = self.serialize_item(tx_hash, tx_pos, tx_height) + 40*chr(0)
        if serialized_hist.find(s) == -1: raise
        serialized_hist = serialized_hist.replace(s, '')
        self.batch_list[addr] = serialized_hist



    def prune_history(self, addr, undo):
        # remove items that have bit set to one
        if undo.get(addr) is None: undo[addr] = []

        serialized_hist = self.batch_list[addr]
        l = len(serialized_hist)/80
        for i in range(l):
            if len(serialized_hist)/80 < self.pruning_limit: break
            item = serialized_hist[80*i:80*(i+1)] 
            if item[39:40] == chr(1):
                assert item[79:80] == chr(2)
                serialized_hist = serialized_hist[0:80*i] + serialized_hist[80*(i+1):]
                undo[addr].append(item)  # items are ordered
        self.batch_list[addr] = serialized_hist


    def revert_prune_history(self, addr, undo):
        # restore removed items
        serialized_hist = self.batch_list[addr]

        if undo.get(addr) is not None: 
            itemlist = undo.pop(addr)
        else:
            return 

        if not itemlist: return

        l = len(serialized_hist)/80
        tx_item = ''
        for i in range(l-1, -1, -1):
            if tx_item == '':
                if not itemlist: 
                    break
                else:
                    tx_item = itemlist.pop(-1) # get the last element
                    tx_height = int(rev_hex(tx_item[36:39].encode('hex')), 16)
            
            item = serialized_hist[80*i:80*(i+1)]
            item_height = int(rev_hex(item[36:39].encode('hex')), 16)

            if item_height < tx_height:
                serialized_hist = serialized_hist[0:80*(i+1)] + tx_item + serialized_hist[80*(i+1):]
                tx_item = ''

        else:
            serialized_hist = ''.join(itemlist) + tx_item + serialized_hist

        self.batch_list[addr] = serialized_hist


    def set_spent_bit(self, addr, txi, is_spent, txid=None, index=None, height=None):
        serialized_hist = self.batch_list[addr]
        l = len(serialized_hist)/80
        for i in range(l):
            item = serialized_hist[80*i:80*(i+1)]
            if item[0:36] == txi:
                if is_spent:
                    new_item = item[0:39] + chr(1) + self.serialize_item(txid, index, height, chr(2))
                else:
                    new_item = item[0:39] + chr(0) + chr(0)*40 
                serialized_hist = serialized_hist[0:80*i] + new_item + serialized_hist[80*(i+1):]
                break
        else:
            self.shared.stop()
            hist = self.deserialize(serialized_hist)
            raise BaseException("prevout not found", addr, hist, txi.encode('hex'))

        self.batch_list[addr] = serialized_hist


    def unset_spent_bit(self, addr, txi):
        self.set_spent_bit(addr, txi, False)
        self.batch_txio[txi] = addr


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

    def get_undo_info(self, height):
        s = self.db_get("undo%d" % (height % 100))
        return eval(s)

    def write_undo_info(self, batch, height, undo_info):
        if self.is_test or height > self.bitcoind_height - 100:
            self.batch_put(batch, "undo%d" % (height % 100), repr(undo_info))

    def import_block(self, block, block_hash, block_height, sync, revert=False):

        self.batch_list = {}  # address -> history
        self.batch_txio = {}  # transaction i/o -> address

        block_inputs = set([])
        block_outputs = set([])
        addr_to_read = set([])

        # deserialize transactions
        t0 = time.time()
        tx_hashes, txdict = self.deserialize_block(block)

        t00 = time.time()

        # undo info
        if revert:
            undo_info = self.get_undo_info(block_height)
        else:
            undo_info = {}


        if not revert:
            # read addresses of tx inputs
            for tx in txdict.values():
                for x in tx.get('inputs'):
                    txi = (x.get('prevout_hash') + int_to_hex(x.get('prevout_n'), 4)).decode('hex')
                    block_inputs.add(txi)

            #block_inputs.sort()
            for txi in sorted(block_inputs):
                try:
                    addr = self.db_get(txi)
                    if addr is None:
                        # the input could come from the same block
                        continue
                except:
                    traceback.print_exc(file=sys.stdout)
                    self.shared.stop()
                    raise

                self.batch_txio[txi] = addr
                addr_to_read.add(addr)

        else:
            for txid, tx in txdict.items():
                for x in tx.get('outputs'):
                    txo = (txid + int_to_hex(x.get('index'), 4)).decode('hex')
                    block_outputs.add(txo)
                    addr_to_read.add( x.get('address') )

                undo = undo_info.get(txid)
                for i, x in enumerate(tx.get('inputs')):
                    addr = undo['prev_addr'][i]
                    addr_to_read.add(addr)


        #time spent reading txio
        t000 = time.time()

        # read histories of addresses
        for txid, tx in txdict.items():
            for x in tx.get('outputs'):
                addr_to_read.add(x.get('address'))

        #addr_to_read.sort()
        for addr in sorted(addr_to_read):
            try:
                h = self.db_get(addr)
                self.batch_list[addr] = '' if h is None else h
            except:
                print "db get error", addr
                traceback.print_exc(file=sys.stdout)
                self.shared.stop()
                raise


        # process
        t1 = time.time()

        if revert:
            tx_hashes = tx_hashes[::-1]


        for txid in tx_hashes:  # must be ordered
            tx = txdict[txid]
            if not revert:

                undo = { 'prev_addr':[] } # contains the list of pruned items for each address in the tx; also, 'prev_addr' is a list of prev addresses
                
                prev_addr = []
                for i, x in enumerate(tx.get('inputs')):
                    txi = (x.get('prevout_hash') + int_to_hex(x.get('prevout_n'), 4)).decode('hex')
                    addr = self.batch_txio[txi]

                    # add redeem item to the history.
                    # add it right next to the input txi? this will break history sorting, but it's ok if I neglect tx inputs during search
                    self.set_spent_bit(addr, txi, True, txid, i, block_height)

                    # when I prune, prune a pair
                    self.prune_history(addr, undo)
                    prev_addr.append(addr)

                undo['prev_addr'] = prev_addr 

                # here I add only the outputs to history; maybe I want to add inputs too (that's in the other loop)
                for x in tx.get('outputs'):
                    addr = x.get('address')
                    self.add_to_history(addr, txid, x.get('index'), block_height)
                    self.prune_history(addr, undo)  # prune here because we increased the length of the history

                undo_info[txid] = undo

            else:

                undo = undo_info.pop(txid)

                for x in tx.get('outputs'):
                    addr = x.get('address')
                    self.revert_prune_history(addr, undo)
                    self.revert_add_to_history(addr, txid, x.get('index'), block_height)

                prev_addr = undo.pop('prev_addr')
                for i, x in enumerate(tx.get('inputs')):
                    addr = prev_addr[i]
                    self.revert_prune_history(addr, undo)
                    txi = (x.get('prevout_hash') + int_to_hex(x.get('prevout_n'), 4)).decode('hex')
                    self.unset_spent_bit(addr, txi)

                assert undo == {}

        if revert: 
            assert undo_info == {}


        # write
        max_len = 0
        max_addr = ''
        t2 = time.time()

        if self.use_plyvel:
            batch = self.db.write_batch()
        else:
            import leveldb
            batch = leveldb.WriteBatch()

        for addr, serialized_hist in self.batch_list.items():
            self.batch_put(batch, addr, serialized_hist)
            l = len(serialized_hist)/80
            if l > max_len:
                max_len = l
                max_addr = addr

        if not revert:
            # add new created outputs
            for txio, addr in self.batch_txio.items():
                self.batch_put(batch, txio, addr)
            # delete spent inputs
            for txi in block_inputs:
                self.batch_delete(batch, txi)
            # add undo info
            self.write_undo_info(batch, block_height, undo_info)
        else:
            # restore spent inputs
            for txio, addr in self.batch_txio.items():
                # print "restoring spent input", repr(txio)
                self.batch_put(batch, txio, addr)
            # delete spent outputs
            for txo in block_outputs:
                self.batch_delete(batch, txo)

        # add the max
        self.batch_put(batch, 'height', self.serialize([(block_hash, block_height, self.db_version)]))

        # actual write
        self.batch_write(batch, sync)

        t3 = time.time()
        if t3 - t0 > 0 and not sync:
            print_log("block %d  "%block_height,
                      "total:%0.2f  " % (t3 - t0),
                      #"parse:%0.2f  " % (t00 - t0),
                      "read_txio[%4d]:%0.2f " % (len(block_inputs), t000 - t00),
                      "read_addr[%4d]:%0.2f " % (len(addr_to_read), t1 - t000),
                      #"proc:%.2f " % (t2-t1),
                      "write:%.2f " % (t3-t2),
                      "max:", max_len, max_addr)

        for addr in self.batch_list.keys():
            self.invalidate_cache(addr)

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
                    print "error rc!!"
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
            result = self.height

        elif method == 'blockchain.headers.subscribe':
            result = self.header

        elif method == 'blockchain.address.subscribe':
            try:
                address = params[0]
                result = self.get_status(address, cache_only)
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log("error:", error)

        elif method == 'blockchain.address.get_history':
            try:
                address = params[0]
                result = self.get_history(address, cache_only)
            except BaseException, e:
                error = str(e) + ': ' + address
                print_log("error:", error)

        elif method == 'blockchain.block.get_header':
            if cache_only:
                result = -1
            else:
                try:
                    height = params[0]
                    result = self.get_header(height)
                except BaseException, e:
                    error = str(e) + ': %d' % height
                    print_log("error:", error)

        elif method == 'blockchain.block.get_chunk':
            if cache_only:
                result = -1
            else:
                try:
                    index = params[0]
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
        t1 = time.time()

        while not self.shared.stopped():
            # are we done yet?
            info = self.bitcoind('getinfo')
            self.bitcoind_height = info.get('blocks')
            bitcoind_block_hash = self.bitcoind('getblockhash', [self.bitcoind_height])
            if self.last_hash == bitcoind_block_hash:
                self.up_to_date = True
                break

            # not done..
            self.up_to_date = False
            next_block_hash = self.bitcoind('getblockhash', [self.height + 1])
            next_block = self.getfullblock(next_block_hash)

            # fixme: this is unsafe, if we revert when the undo info is not yet written
            revert = (random.randint(1, 100) == 1) if self.is_test else False

            if (next_block.get('previousblockhash') == self.last_hash) and not revert:

                self.import_block(next_block, next_block_hash, self.height+1, sync)
                self.height = self.height + 1
                self.write_header(self.block2header(next_block), sync)
                self.last_hash = next_block_hash

                if self.height % 100 == 0 and not sync:
                    t2 = time.time()
                    print_log("catch_up: block %d (%.3fs)" % (self.height, t2 - t1))
                    t1 = t2

            else:
                # revert current block
                block = self.getfullblock(self.last_hash)
                print_log("blockchain reorg", self.height, block.get('previousblockhash'), self.last_hash)
                self.import_block(block, self.last_hash, self.height, sync, revert=True)
                self.pop_header()
                self.flush_headers()

                self.height -= 1

                # read previous header from disk
                self.header = self.read_header(self.height)
                self.last_hash = self.hash_header(self.header)

        self.header = self.block2header(self.bitcoind('getblock', [self.last_hash]))

        if self.shared.stopped() and self.use_plyvel: 
            print_log( "closing database" )
            self.db.close()


    def memorypool_update(self):
        mempool_hashes = set(self.bitcoind('getrawmempool'))
        touched_addresses = set([])

        for tx_hash in mempool_hashes:
            if tx_hash in self.mempool_hashes:
                continue

            tx = self.get_mempool_transaction(tx_hash)
            if not tx:
                continue

            mpa = self.mempool_addresses.get(tx_hash, [])
            for x in tx.get('inputs'):
                # we assume that the input address can be parsed by deserialize(); this is true for Electrum transactions
                addr = x.get('address')
                if addr and addr not in mpa:
                    mpa.append(addr)
                    touched_addresses.add(addr)

            for x in tx.get('outputs'):
                addr = x.get('address')
                if addr and addr not in mpa:
                    mpa.append(addr)
                    touched_addresses.add(addr)

            self.mempool_addresses[tx_hash] = mpa
            self.mempool_hashes.add(tx_hash)

        # remove older entries from mempool_hashes
        self.mempool_hashes = mempool_hashes

        # remove deprecated entries from mempool_addresses
        for tx_hash, addresses in self.mempool_addresses.items():
            if tx_hash not in self.mempool_hashes:
                self.mempool_addresses.pop(tx_hash)
                for addr in addresses:
                    touched_addresses.add(addr)

        # rebuild mempool histories
        new_mempool_hist = {}
        for tx_hash, addresses in self.mempool_addresses.items():
            for addr in addresses:
                h = new_mempool_hist.get(addr, [])
                if tx_hash not in h:
                    h.append(tx_hash)
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

    def main_iteration(self):
        if self.shared.stopped():
            print_log("blockchain processor terminating")
            if self.use_plyvel:
                self.db.close()
            return

        with self.dblock:
            t1 = time.time()
            self.catch_up()
            t2 = time.time()

        self.memorypool_update()

        if self.sent_height != self.height:
            self.sent_height = self.height
            for session in self.watch_blocks:
                self.push_response(session, {
                        'id': None,
                        'method': 'blockchain.numblocks.subscribe',
                        'params': [self.height],
                        })

        if self.sent_header != self.header:
            print_log("blockchain: %d (%.3fs)" % (self.height, t2 - t1))
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

        if not self.shared.stopped():
            threading.Timer(10, self.main_iteration).start()
        else:
            print_log("blockchain processor terminating")
