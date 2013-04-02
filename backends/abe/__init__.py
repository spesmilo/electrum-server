import binascii
from json import dumps, loads
import operator
from Queue import Queue
import sys
import thread
import threading
import time
import traceback
import urllib

from Abe import DataStore, readconf, BCDataStream, deserialize
from Abe.util import hash_to_address, decode_check_address

from processor import Processor, print_log
from utils import *


class AbeStore(DataStore.DataStore):

    def __init__(self, config):
        conf = DataStore.CONFIG_DEFAULTS
        args, argv = readconf.parse_argv([], conf)
        args.dbtype = config.get('database', 'type')
        if args.dbtype == 'sqlite3':
            args.connect_args = {'database': config.get('database', 'database')}
        elif args.dbtype == 'MySQLdb':
            args.connect_args = {'db': config.get('database', 'database'), 'user': config.get('database', 'username'), 'passwd': config.get('database', 'password')}
        elif args.dbtype == 'psycopg2':
            args.connect_args = {'database': config.get('database', 'database')}

        coin = config.get('server', 'coin')
        self.addrtype = 0
        if coin == 'litecoin':
            print_log('Litecoin settings:')
            datadir = config.get('server', 'datadir')
            print_log('  datadir = ' + datadir)
            args.datadir = [{"dirname": datadir, "chain": "Litecoin", "code3": "LTC", "address_version": "\u0030"}]
            print_log('  addrtype = 48')
            self.addrtype = 48

        DataStore.DataStore.__init__(self, args)

        # Use 1 (Bitcoin) if chain_id is not sent
        self.chain_id = self.datadirs[0]["chain_id"] or 1
        print_log('Coin chain_id = %d' % self.chain_id)

        self.sql_limit = int(config.get('database', 'limit'))

        self.tx_cache = {}
        self.bitcoind_url = 'http://%s:%s@%s:%s/' % (config.get('bitcoind', 'user'), config.get('bitcoind', 'password'), config.get('bitcoind', 'host'), config.get('bitcoind', 'port'))

        self.chunk_cache = {}

        self.address_queue = Queue()

        self.lock = threading.Lock()        # for the database
        self.cache_lock = threading.Lock()  # for the cache
        self.last_tx_id = 0
        self.known_mempool_hashes = []

    def import_tx(self, tx, is_coinbase):
        tx_id = super(AbeStore, self).import_tx(tx, is_coinbase)
        self.last_tx_id = tx_id
        return tx_id

    def import_block(self, b, chain_ids=frozenset()):
        #print_log("import block")
        block_id = super(AbeStore, self).import_block(b, chain_ids)
        for pos in xrange(len(b['transactions'])):
            tx = b['transactions'][pos]
            if 'hash' not in tx:
                tx['hash'] = Hash(tx['tx'])
            tx_id = self.tx_find_id_and_value(tx)
            if tx_id:
                self.update_tx_cache(tx_id)
            else:
                print_log("error: import_block: no tx_id")
        return block_id

    def update_tx_cache(self, txid):
        inrows = self.get_tx_inputs(txid, False)
        for row in inrows:
            _hash = self.binout(row[6])
            if not _hash:
                #print_log("WARNING: missing tx_in for tx", txid)
                continue

            address = hash_to_address(chr(self.addrtype), _hash)
            with self.cache_lock:
                if address in self.tx_cache:
                    print_log("cache: invalidating", address)
                    self.tx_cache.pop(address)

            self.address_queue.put(address)

        outrows = self.get_tx_outputs(txid, False)
        for row in outrows:
            _hash = self.binout(row[6])
            if not _hash:
                #print_log("WARNING: missing tx_out for tx", txid)
                continue

            address = hash_to_address(chr(self.addrtype), _hash)
            with self.cache_lock:
                if address in self.tx_cache:
                    print_log("cache: invalidating", address)
                    self.tx_cache.pop(address)

            self.address_queue.put(address)

    def safe_sql(self, sql, params=(), lock=True):
        error = False
        try:
            if lock:
                self.lock.acquire()
            ret = self.selectall(sql, params)
        except:
            error = True
            traceback.print_exc(file=sys.stdout)
        finally:
            if lock:
                self.lock.release()

        if error:
            raise Exception('sql error')

        return ret

    def get_tx_outputs(self, tx_id, lock=True):
        return self.safe_sql("""SELECT
                txout.txout_pos,
                txout.txout_scriptPubKey,
                txout.txout_value,
                nexttx.tx_hash,
                nexttx.tx_id,
                txin.txin_pos,
                pubkey.pubkey_hash
              FROM txout
              LEFT JOIN txin ON (txin.txout_id = txout.txout_id)
              LEFT JOIN pubkey ON (pubkey.pubkey_id = txout.pubkey_id)
              LEFT JOIN tx nexttx ON (txin.tx_id = nexttx.tx_id)
             WHERE txout.tx_id = %d
             ORDER BY txout.txout_pos
        """ % (tx_id), (), lock)

    def get_tx_inputs(self, tx_id, lock=True):
        return self.safe_sql(""" SELECT
                txin.txin_pos,
                txin.txin_scriptSig,
                txout.txout_value,
                COALESCE(prevtx.tx_hash, u.txout_tx_hash),
                prevtx.tx_id,
                COALESCE(txout.txout_pos, u.txout_pos),
                pubkey.pubkey_hash
              FROM txin
              LEFT JOIN txout ON (txout.txout_id = txin.txout_id)
              LEFT JOIN pubkey ON (pubkey.pubkey_id = txout.pubkey_id)
              LEFT JOIN tx prevtx ON (txout.tx_id = prevtx.tx_id)
              LEFT JOIN unlinked_txin u ON (u.txin_id = txin.txin_id)
             WHERE txin.tx_id = %d
             ORDER BY txin.txin_pos
             """ % (tx_id,), (), lock)

    def get_address_out_rows(self, dbhash):
        out = self.safe_sql(""" SELECT
                b.block_nTime,
                cc.chain_id,
                b.block_height,
                1,
                b.block_hash,
                tx.tx_hash,
                tx.tx_id,
                txin.txin_pos,
                -prevout.txout_value
              FROM chain_candidate cc
              JOIN block b ON (b.block_id = cc.block_id)
              JOIN block_tx ON (block_tx.block_id = b.block_id)
              JOIN tx ON (tx.tx_id = block_tx.tx_id)
              JOIN txin ON (txin.tx_id = tx.tx_id)
              JOIN txout prevout ON (txin.txout_id = prevout.txout_id)
              JOIN pubkey ON (pubkey.pubkey_id = prevout.pubkey_id)
             WHERE pubkey.pubkey_hash = ?
               AND cc.chain_id = ?
               AND cc.in_longest = 1
             LIMIT ? """, (dbhash, self.chain_id, self.sql_limit))

        if len(out) == self.sql_limit:
            raise Exception('limit reached')
        return out

    def get_address_out_rows_memorypool(self, dbhash):
        out = self.safe_sql(""" SELECT
                1,
                tx.tx_hash,
                tx.tx_id,
                txin.txin_pos,
                -prevout.txout_value
              FROM tx
              JOIN txin ON (txin.tx_id = tx.tx_id)
              JOIN txout prevout ON (txin.txout_id = prevout.txout_id)
              JOIN pubkey ON (pubkey.pubkey_id = prevout.pubkey_id)
             WHERE pubkey.pubkey_hash = ?
             LIMIT ? """, (dbhash, self.sql_limit))

        if len(out) == self.sql_limit:
            raise Exception('limit reached')
        return out

    def get_address_in_rows(self, dbhash):
        out = self.safe_sql(""" SELECT
                b.block_nTime,
                cc.chain_id,
                b.block_height,
                0,
                b.block_hash,
                tx.tx_hash,
                tx.tx_id,
                txout.txout_pos,
                txout.txout_value
              FROM chain_candidate cc
              JOIN block b ON (b.block_id = cc.block_id)
              JOIN block_tx ON (block_tx.block_id = b.block_id)
              JOIN tx ON (tx.tx_id = block_tx.tx_id)
              JOIN txout ON (txout.tx_id = tx.tx_id)
              JOIN pubkey ON (pubkey.pubkey_id = txout.pubkey_id)
             WHERE pubkey.pubkey_hash = ?
               AND cc.chain_id = ?
               AND cc.in_longest = 1
               LIMIT ? """, (dbhash, self.chain_id, self.sql_limit))

        if len(out) == self.sql_limit:
            raise Exception('limit reached')
        return out

    def get_address_in_rows_memorypool(self, dbhash):
        out = self.safe_sql(""" SELECT
                0,
                tx.tx_hash,
                tx.tx_id,
                txout.txout_pos,
                txout.txout_value
              FROM tx
              JOIN txout ON (txout.tx_id = tx.tx_id)
              JOIN pubkey ON (pubkey.pubkey_id = txout.pubkey_id)
             WHERE pubkey.pubkey_hash = ?
             LIMIT ? """, (dbhash, self.sql_limit))

        if len(out) == self.sql_limit:
            raise Exception('limit reached')
        return out

    def get_history(self, addr, cache_only=False):
        # todo: make this more efficient. it iterates over txpoints multiple times
        with self.cache_lock:
            cached_version = self.tx_cache.get(addr)
            if cached_version is not None:
                return cached_version

        if cache_only:
            return -1

        version, binaddr = decode_check_address(addr)
        if binaddr is None:
            return None

        dbhash = self.binin(binaddr)
        rows = []
        rows += self.get_address_out_rows(dbhash)
        rows += self.get_address_in_rows(dbhash)

        txpoints = []
        known_tx = []

        for row in rows:
            try:
                nTime, chain_id, height, is_in, blk_hash, tx_hash, tx_id, pos, value = row
            except:
                print_log("cannot unpack row", row)
                break
            tx_hash = self.hashout_hex(tx_hash)

            txpoints.append({
                "timestamp": int(nTime),
                "height": int(height),
                "is_input": int(is_in),
                "block_hash": self.hashout_hex(blk_hash),
                "tx_hash": tx_hash,
                "tx_id": int(tx_id),
                "index": int(pos),
                "value": int(value),
            })
            known_tx.append(tx_hash)

        # todo: sort them really...
        txpoints = sorted(txpoints, key=operator.itemgetter("timestamp"))

        # read memory pool
        rows = []
        rows += self.get_address_in_rows_memorypool(dbhash)
        rows += self.get_address_out_rows_memorypool(dbhash)
        address_has_mempool = False

        for row in rows:
            is_in, tx_hash, tx_id, pos, value = row
            tx_hash = self.hashout_hex(tx_hash)
            if tx_hash in known_tx:
                continue

            # discard transactions that are too old
            if self.last_tx_id - tx_id > 50000:
                print_log("discarding tx id", tx_id)
                continue

            # this means that pending transactions were added to the db, even if they are not returned by getmemorypool
            address_has_mempool = True

            #print_log("mempool", tx_hash)
            txpoints.append({
                "timestamp": 0,
                "height": 0,
                "is_input": int(is_in),
                "block_hash": 'mempool',
                "tx_hash": tx_hash,
                "tx_id": int(tx_id),
                "index": int(pos),
                "value": int(value),
            })

        for txpoint in txpoints:
            tx_id = txpoint['tx_id']

            txinputs = []
            inrows = self.get_tx_inputs(tx_id)
            for row in inrows:
                _hash = self.binout(row[6])
                if not _hash:
                    #print_log("WARNING: missing tx_in for tx", tx_id, addr)
                    continue
                address = hash_to_address(chr(self.addrtype), _hash)
                txinputs.append(address)
            txpoint['inputs'] = txinputs
            txoutputs = []
            outrows = self.get_tx_outputs(tx_id)
            for row in outrows:
                _hash = self.binout(row[6])
                if not _hash:
                    #print_log("WARNING: missing tx_out for tx", tx_id, addr)
                    continue
                address = hash_to_address(chr(self.addrtype), _hash)
                txoutputs.append(address)
            txpoint['outputs'] = txoutputs

            # for all unspent inputs, I want their scriptpubkey. (actually I could deduce it from the address)
            if not txpoint['is_input']:
                # detect if already redeemed...
                for row in outrows:
                    if row[6] == dbhash:
                        break
                else:
                    raise
                #row = self.get_tx_output(tx_id,dbhash)
                # pos, script, value, o_hash, o_id, o_pos, binaddr = row
                # if not redeemed, we add the script
                if row:
                    if not row[4]:
                        txpoint['raw_output_script'] = row[1]

            txpoint.pop('tx_id')

        txpoints = map(lambda x: {'tx_hash': x['tx_hash'], 'height': x['height']}, txpoints)
        out = []
        for item in txpoints:
            if item not in out:
                out.append(item)

        # cache result
        ## do not cache mempool results because statuses are ambiguous
        #if not address_has_mempool:
        with self.cache_lock:
            self.tx_cache[addr] = out

        return out

    def get_status(self, addr, cache_only=False):
        # for 0.5 clients
        tx_points = self.get_history(addr, cache_only)
        if cache_only and tx_points == -1:
            return -1

        if not tx_points:
            return None
        status = ''
        for tx in tx_points:
            status += tx.get('tx_hash') + ':%d:' % tx.get('height')
        return hashlib.sha256(status).digest().encode('hex')

    def get_block_header(self, block_height):
        out = self.safe_sql("""
            SELECT
                block_hash,
                block_version,
                block_hashMerkleRoot,
                block_nTime,
                block_nBits,
                block_nNonce,
                block_height,
                prev_block_hash,
                block_id
              FROM chain_summary
             WHERE block_height = %d AND in_longest = 1""" % block_height)

        if not out:
            raise Exception("block not found")
        row = out[0]
        (block_hash, block_version, hashMerkleRoot, nTime, nBits, nNonce, height, prev_block_hash, block_id) \
            = (self.hashout_hex(row[0]), int(row[1]), self.hashout_hex(row[2]), int(row[3]), int(row[4]), int(row[5]), int(row[6]), self.hashout_hex(row[7]), int(row[8]))

        return {
            "block_height": block_height,
            "version": block_version,
            "prev_block_hash": prev_block_hash,
            "merkle_root": hashMerkleRoot,
            "timestamp": nTime,
            "bits": nBits,
            "nonce": nNonce,
        }

    def get_chunk(self, index):
        with self.cache_lock:
            msg = self.chunk_cache.get(index)
            if msg:
                return msg

        sql = """
            SELECT
                block_hash,
                block_version,
                block_hashMerkleRoot,
                block_nTime,
                block_nBits,
                block_nNonce,
                block_height,
                prev_block_hash,
                block_height
              FROM chain_summary
             WHERE block_height >= %d AND block_height< %d AND in_longest = 1 ORDER BY block_height""" % (index * 2016, (index+1) * 2016)

        out = self.safe_sql(sql)
        msg = ''
        for row in out:
            (block_hash, block_version, hashMerkleRoot, nTime, nBits, nNonce, height, prev_block_hash, block_height) \
                = (self.hashout_hex(row[0]), int(row[1]), self.hashout_hex(row[2]), int(row[3]), int(row[4]), int(row[5]), int(row[6]), self.hashout_hex(row[7]), int(row[8]))
            h = {
                "block_height": block_height,
                "version": block_version,
                "prev_block_hash": prev_block_hash,
                "merkle_root": hashMerkleRoot,
                "timestamp": nTime,
                "bits": nBits,
                "nonce": nNonce,
            }

            if h.get('block_height') == 0:
                h['prev_block_hash'] = "0" * 64
            msg += header_to_string(h)

            #print_log("hash", encode(Hash(msg.decode('hex'))))
            #if h.get('block_height')==1:break

        with self.cache_lock:
            self.chunk_cache[index] = msg
        print_log("get_chunk", index, len(msg))
        return msg

    def get_raw_tx(self, tx_hash, height):
        postdata = dumps({"method": 'getrawtransaction', 'params': [tx_hash, 0, height], 'id': 'jsonrpc'})
        respdata = urllib.urlopen(self.bitcoind_url, postdata).read()
        r = loads(respdata)
        if r['error'] is not None:
            raise Exception(r['error'])

        return r.get('result')

    def get_tx_merkle(self, tx_hash):
        out = self.safe_sql("""
             SELECT block_tx.block_id FROM tx
             JOIN block_tx on tx.tx_id = block_tx.tx_id
             JOIN chain_summary on chain_summary.block_id = block_tx.block_id
             WHERE tx_hash='%s' AND in_longest = 1""" % tx_hash)

        if not out:
            raise Exception("not in a block")
        block_id = int(out[0][0])

        # get block height
        out = self.safe_sql("SELECT block_height FROM chain_summary WHERE block_id = %d AND in_longest = 1" % block_id)

        if not out:
            raise Exception("block not found")
        block_height = int(out[0][0])

        merkle = []
        tx_pos = None

        # list all tx in block
        for row in self.safe_sql("""
            SELECT DISTINCT tx_id, tx_pos, tx_hash
              FROM txin_detail
             WHERE block_id = ?
             ORDER BY tx_pos""", (block_id,)):
            _id, _pos, _hash = row
            merkle.append(_hash)
            if _hash == tx_hash:
                tx_pos = int(_pos)

        # find subset.
        # TODO: do not compute this on client request, better store the hash tree of each block in a database...

        merkle = map(hash_decode, merkle)
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

        # send result
        return {"block_height": block_height, "merkle": s, "pos": tx_pos}

    def memorypool_update(store):
        ds = BCDataStream.BCDataStream()
        postdata = dumps({"method": 'getrawmempool', 'params': [], 'id': 'jsonrpc'})
        respdata = urllib.urlopen(store.bitcoind_url, postdata).read()

        r = loads(respdata)
        if r['error'] is not None:
            print_log(r['error'])
            return

        mempool_hashes = r.get('result')
        num_new_tx = 0

        for tx_hash in mempool_hashes:

            if tx_hash in store.known_mempool_hashes:
                continue
            store.known_mempool_hashes.append(tx_hash)
            num_new_tx += 1

            postdata = dumps({"method": 'getrawtransaction', 'params': [tx_hash], 'id': 'jsonrpc'})
            respdata = urllib.urlopen(store.bitcoind_url, postdata).read()
            r = loads(respdata)
            if r['error'] is not None:
                continue
            hextx = r.get('result')
            ds.clear()
            ds.write(hextx.decode('hex'))
            tx = deserialize.parse_Transaction(ds)
            tx['hash'] = Hash(tx['tx'])

            if store.tx_find_id_and_value(tx):
                pass
            else:
                tx_id = store.import_tx(tx, False)
                store.update_tx_cache(tx_id)
                #print_log(tx_hash)

        store.commit()
        store.known_mempool_hashes = mempool_hashes
        return num_new_tx

    def send_tx(self, tx):
        postdata = dumps({"method": 'sendrawtransaction', 'params': [tx], 'id': 'jsonrpc'})
        respdata = urllib.urlopen(self.bitcoind_url, postdata).read()
        r = loads(respdata)
        if r['error'] is not None:
            msg = r['error'].get('message')
            out = "error: transaction rejected by memorypool: " + msg + "\n" + tx
        else:
            out = r['result']
        return out

    def main_iteration(self):
        with self.lock:
            t1 = time.time()
            self.catch_up()
            t2 = time.time()
            time_catch_up = t2 - t1
            n = self.memorypool_update()
            time_mempool = time.time() - t2
            height = self.get_block_number(self.chain_id)

        with self.cache_lock:
            try:
                self.chunk_cache.pop(height/2016)
            except:
                pass

        block_header = self.get_block_header(height)
        return block_header, time_catch_up, time_mempool, n

    def catch_up(store):
        # if there is an exception, do rollback and then re-raise the exception
        for dircfg in store.datadirs:
            try:
                store.catch_up_dir(dircfg)
            except Exception, e:
                store.log.exception("Failed to catch up %s", dircfg)
                store.rollback()
                raise e


class BlockchainProcessor(Processor):

    def __init__(self, config, shared):
        Processor.__init__(self)
        self.store = AbeStore(config)
        self.watched_addresses = []
        self.shared = shared

        # catch_up first
        self.block_header, time_catch_up, time_mempool, n = self.store.main_iteration()
        self.block_number = self.block_header.get('block_height')
        print_log("blockchain: %d blocks" % self.block_number)

        threading.Timer(10, self.run_store_iteration).start()

    def add_request(self, request):
        # see if we can get if from cache. if not, add to queue
        if self.process(request, cache_only=True) == -1:
            self.queue.put(request)

    def process(self, request, cache_only=False):
        #print_log("abe process", request)

        message_id = request['id']
        method = request['method']
        params = request.get('params', [])
        result = None
        error = None

        if method == 'blockchain.numblocks.subscribe':
            result = self.block_number

        elif method == 'blockchain.headers.subscribe':
            result = self.block_header

        elif method == 'blockchain.address.subscribe':
            try:
                address = params[0]
                result = self.store.get_status(address, cache_only)
                self.watch_address(address)
            except Exception, e:
                error = str(e) + ': ' + address
                print_log("error:", error)

        elif method == 'blockchain.address.get_history':
            try:
                address = params[0]
                result = self.store.get_history(address, cache_only)
            except Exception, e:
                error = str(e) + ': ' + address
                print_log("error:", error)

        elif method == 'blockchain.block.get_header':
            if cache_only:
                result = -1
            else:
                try:
                    height = params[0]
                    result = self.store.get_block_header(height)
                except Exception, e:
                    error = str(e) + ': %d' % height
                    print_log("error:", error)

        elif method == 'blockchain.block.get_chunk':
            if cache_only:
                result = -1
            else:
                try:
                    index = params[0]
                    result = self.store.get_chunk(index)
                except Exception, e:
                    error = str(e) + ': %d' % index
                    print_log("error:", error)

        elif method == 'blockchain.transaction.broadcast':
            txo = self.store.send_tx(params[0])
            print_log("sent tx:", txo)
            result = txo

        elif method == 'blockchain.transaction.get_merkle':
            if cache_only:
                result = -1
            else:
                try:
                    tx_hash = params[0]
                    result = self.store.get_tx_merkle(tx_hash)
                except Exception, e:
                    error = str(e) + ': ' + tx_hash
                    print_log("error:", error)

        elif method == 'blockchain.transaction.get':
            try:
                tx_hash = params[0]
                height = params[1]
                result = self.store.get_raw_tx(tx_hash, height)
            except Exception, e:
                error = str(e) + ': ' + tx_hash
                print_log("error:", error)

        else:
            error = "unknown method:%s" % method

        if cache_only and result == -1:
            return -1

        if error:
            response = {'id': message_id, 'error': error}
            self.push_response(response)
        elif result != '':
            response = {'id': message_id, 'result': result}
            self.push_response(response)

    def watch_address(self, addr):
        if addr not in self.watched_addresses:
            self.watched_addresses.append(addr)

    def run_store_iteration(self):
        try:
            block_header, time_catch_up, time_mempool, n = self.store.main_iteration()
        except:
            traceback.print_exc(file=sys.stdout)
            print_log("terminating")
            self.shared.stop()

        if self.shared.stopped():
            print_log("exit timer")
            return

        #print_log("block number: %d  (%.3fs)  mempool:%d (%.3fs)"%(self.block_number, time_catch_up, n, time_mempool))

        if self.block_number != block_header.get('block_height'):
            self.block_number = block_header.get('block_height')
            print_log("block number: %d  (%.3fs)" % (self.block_number, time_catch_up))
            self.push_response({'id': None, 'method': 'blockchain.numblocks.subscribe', 'params': [self.block_number]})

        if self.block_header != block_header:
            self.block_header = block_header
            self.push_response({'id': None, 'method': 'blockchain.headers.subscribe', 'params': [self.block_header]})

        while True:
            try:
                addr = self.store.address_queue.get(False)
            except:
                break
            if addr in self.watched_addresses:
                try:
                    status = self.store.get_status(addr)
                    self.push_response({'id': None, 'method': 'blockchain.address.subscribe', 'params': [addr, status]})
                except:
                    break

        threading.Timer(10, self.run_store_iteration).start()
