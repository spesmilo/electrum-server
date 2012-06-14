from Abe.util import hash_to_address, decode_check_address
from Abe.DataStore import DataStore as Datastore_class
from Abe import DataStore, readconf, BCDataStream,  deserialize, util, base58

import binascii

import thread, traceback, sys, urllib, operator
from json import dumps, loads
from Queue import Queue
import time, threading



class AbeStore(Datastore_class):

    def __init__(self, config):
        conf = DataStore.CONFIG_DEFAULTS
        args, argv = readconf.parse_argv( [], conf)
        args.dbtype = config.get('database','type')
        if args.dbtype == 'sqlite3':
            args.connect_args = { 'database' : config.get('database','database') }
        elif args.dbtype == 'MySQLdb':
            args.connect_args = { 'db' : config.get('database','database'), 'user' : config.get('database','username'), 'passwd' : config.get('database','password') }
        elif args.dbtype == 'psycopg2':
            args.connect_args = { 'database' : config.get('database','database') }

        Datastore_class.__init__(self,args)

        self.sql_limit = config.get('database','limit')

        self.tx_cache = {}
        self.bitcoind_url = 'http://%s:%s@%s:%s/' % ( config.get('bitcoind','user'), config.get('bitcoind','password'), config.get('bitcoind','host'), config.get('bitcoind','port'))

        self.address_queue = Queue()

        self.dblock = thread.allocate_lock()



    def import_block(self, b, chain_ids=frozenset()):
        #print "import block"
        block_id = super(AbeStore, self).import_block(b, chain_ids)
        for pos in xrange(len(b['transactions'])):
            tx = b['transactions'][pos]
            if 'hash' not in tx:
                tx['hash'] = util.double_sha256(tx['tx'])
            tx_id = self.tx_find_id_and_value(tx)
            if tx_id:
                self.update_tx_cache(tx_id)
            else:
                print "error: import_block: no tx_id"
        return block_id


    def update_tx_cache(self, txid):
        inrows = self.get_tx_inputs(txid, False)
        for row in inrows:
            _hash = self.binout(row[6])
            if not _hash:
                #print "WARNING: missing tx_in for tx", txid
                continue

            address = hash_to_address(chr(0), _hash)
            if self.tx_cache.has_key(address):
                print "cache: invalidating", address
                self.tx_cache.pop(address)
            self.address_queue.put(address)

        outrows = self.get_tx_outputs(txid, False)
        for row in outrows:
            _hash = self.binout(row[6])
            if not _hash:
                #print "WARNING: missing tx_out for tx", txid
                continue

            address = hash_to_address(chr(0), _hash)
            if self.tx_cache.has_key(address):
                print "cache: invalidating", address
                self.tx_cache.pop(address)
            self.address_queue.put(address)

    def safe_sql(self,sql, params=(), lock=True):

        error = False
        try:
            if lock: self.dblock.acquire()
            ret = self.selectall(sql,params)
        except:
            error = True
        finally:
            if lock: self.dblock.release()

        if error: 
            raise BaseException('sql error')

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
        """%(tx_id), (), lock)

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
             """%(tx_id,), (), lock)


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
               AND cc.in_longest = 1
             LIMIT ? """, (dbhash,self.sql_limit))

        if len(out)==self.sql_limit: 
            raise BaseException('limit reached')
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
             LIMIT ? """, (dbhash,self.sql_limit))

        if len(out)==self.sql_limit: 
            raise BaseException('limit reached')
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
               AND cc.in_longest = 1
               LIMIT ? """, (dbhash,self.sql_limit))

        if len(out)==self.sql_limit: 
            raise BaseException('limit reached')
        return out

    def get_address_in_rows_memorypool(self, dbhash):
        out = self.safe_sql( """ SELECT
                0,
                tx.tx_hash,
                tx.tx_id,
                txout.txout_pos,
                txout.txout_value
              FROM tx
              JOIN txout ON (txout.tx_id = tx.tx_id)
              JOIN pubkey ON (pubkey.pubkey_id = txout.pubkey_id)
             WHERE pubkey.pubkey_hash = ?
             LIMIT ? """, (dbhash,self.sql_limit))

        if len(out)==self.sql_limit: 
            raise BaseException('limit reached')
        return out

    def get_history(self, addr):

        cached_version = self.tx_cache.get( addr )
        if cached_version is not None:
            return cached_version

        version, binaddr = decode_check_address(addr)
        if binaddr is None:
            return None

        dbhash = self.binin(binaddr)
        rows = []
        rows += self.get_address_out_rows( dbhash )
        rows += self.get_address_in_rows( dbhash )

        txpoints = []
        known_tx = []

        for row in rows:
            try:
                nTime, chain_id, height, is_in, blk_hash, tx_hash, tx_id, pos, value = row
            except:
                print "cannot unpack row", row
                break
            tx_hash = self.hashout_hex(tx_hash)
            txpoint = {
                    "timestamp":    int(nTime),
                    "height":   int(height),
                    "is_input":    int(is_in),
                    "block_hash": self.hashout_hex(blk_hash),
                    "tx_hash":  tx_hash,
                    "tx_id":    int(tx_id),
                    "index":      int(pos),
                    "value":    int(value),
                    }

            txpoints.append(txpoint)
            known_tx.append(self.hashout_hex(tx_hash))


        # todo: sort them really...
        txpoints = sorted(txpoints, key=operator.itemgetter("timestamp"))

        # read memory pool
        rows = []
        rows += self.get_address_in_rows_memorypool( dbhash )
        rows += self.get_address_out_rows_memorypool( dbhash )
        address_has_mempool = False

        current_id = self.new_id("tx")

        for row in rows:
            is_in, tx_hash, tx_id, pos, value = row
            tx_hash = self.hashout_hex(tx_hash)
            if tx_hash in known_tx:
                continue

            # this means that pending transactions were added to the db, even if they are not returned by getmemorypool
            address_has_mempool = True

            # fixme: we need to detect transactions that became invalid
            if current_id - tx_id > 10000:
                continue


            #print "mempool", tx_hash
            txpoint = {
                    "timestamp":    0,
                    "height":   0,
                    "is_input":    int(is_in),
                    "block_hash": 'mempool', 
                    "tx_hash":  tx_hash,
                    "tx_id":    int(tx_id),
                    "index":      int(pos),
                    "value":    int(value),
                    }
            txpoints.append(txpoint)


        for txpoint in txpoints:
            tx_id = txpoint['tx_id']
            
            txinputs = []
            inrows = self.get_tx_inputs(tx_id)
            for row in inrows:
                _hash = self.binout(row[6])
                if not _hash:
                    #print "WARNING: missing tx_in for tx", tx_id, addr
                    continue
                address = hash_to_address(chr(0), _hash)
                txinputs.append(address)
            txpoint['inputs'] = txinputs
            txoutputs = []
            outrows = self.get_tx_outputs(tx_id)
            for row in outrows:
                _hash = self.binout(row[6])
                if not _hash:
                    #print "WARNING: missing tx_out for tx", tx_id, addr
                    continue
                address = hash_to_address(chr(0), _hash)
                txoutputs.append(address)
            txpoint['outputs'] = txoutputs

            # for all unspent inputs, I want their scriptpubkey. (actually I could deduce it from the address)
            if not txpoint['is_input']:
                # detect if already redeemed...
                for row in outrows:
                    if row[6] == dbhash: break
                else:
                    raise
                #row = self.get_tx_output(tx_id,dbhash)
                # pos, script, value, o_hash, o_id, o_pos, binaddr = row
                # if not redeemed, we add the script
                if row:
                    if not row[4]: txpoint['raw_output_script'] = row[1]

        # cache result
        # do not cache mempool results because statuses are ambiguous
        if not address_has_mempool:
            self.tx_cache[addr] = txpoints
        
        return txpoints


    def get_status(self,addr):
        # get address status, i.e. the last block for that address.
        tx_points = self.get_history(addr)
        if not tx_points:
            status = None
        else:
            lastpoint = tx_points[-1]
            status = lastpoint['block_hash']
            # this is a temporary hack; move it up once old clients have disappeared
            if status == 'mempool': # and session['version'] != "old":
                status = status + ':%d'% len(tx_points)
        return status



    def memorypool_update(store):

        ds = BCDataStream.BCDataStream()
        postdata = dumps({"method": 'getmemorypool', 'params': [], 'id':'jsonrpc'})

        respdata = urllib.urlopen(store.bitcoind_url, postdata).read()
        r = loads(respdata)
        if r['error'] != None:
            return

        v = r['result'].get('transactions')
        for hextx in v:
            ds.clear()
            ds.write(hextx.decode('hex'))
            tx = deserialize.parse_Transaction(ds)
            tx['hash'] = util.double_sha256(tx['tx'])
            tx_hash = store.hashin(tx['hash'])

            if store.tx_find_id_and_value(tx):
                pass
            else:
                tx_id = store.import_tx(tx, False)
                store.update_tx_cache(tx_id)
    
        store.commit()


    def send_tx(self,tx):
        postdata = dumps({"method": 'importtransaction', 'params': [tx], 'id':'jsonrpc'})
        respdata = urllib.urlopen(self.bitcoind_url, postdata).read()
        r = loads(respdata)
        if r['error'] != None:
            msg = r['error'].get('message')
            out = "error: transaction rejected by memorypool: " + msg + "\n" + tx
        else:
            out = r['result']
        return out


    def main_iteration(store):
        try:
            store.dblock.acquire()
            store.catch_up()
            store.memorypool_update()
            block_number = store.get_block_number(1)

        except IOError:
            print "IOError: cannot reach bitcoind"
            block_number = 0
        except:
            traceback.print_exc(file=sys.stdout)
            block_number = 0
        finally:
            store.dblock.release()

        return block_number


    def catch_up(store):
        # if there is an exception, do rollback and then re-raise the exception
        for dircfg in store.datadirs:
            try:
                store.catch_up_dir(dircfg)
            except Exception, e:
                store.log.exception("Failed to catch up %s", dircfg)
                store.rollback()
                raise e




from processor import Processor

class BlockchainProcessor(Processor):

    def __init__(self, config):
        Processor.__init__(self)
        self.store = AbeStore(config)
        self.block_number = -1
        self.watched_addresses = []
        threading.Timer(10, self.run_store_iteration).start()

    def process(self, request):
        #print "abe process", request

        message_id = request['id']
        method = request['method']
        params = request.get('params',[])
        result = None
        error = None

        if method == 'blockchain.numblocks.subscribe':
            result = self.block_number

        elif method == 'blockchain.address.subscribe':
            try:
                address = params[0]
                result = self.store.get_status(address)
                self.watch_address(address)
            except BaseException, e:
                error = str(e) + ': ' + address
                print "error:", error

        elif method == 'blockchain.address.get_history':
            try:
                address = params[0]
                result = self.store.get_history( address ) 
            except BaseException, e:
                error = str(e) + ': ' + address
                print "error:", error

        elif method == 'blockchain.transaction.broadcast':
            txo = self.store.send_tx(params[0])
            print "sent tx:", txo
            result = txo 

        else:
            error = "unknown method:%s"%method


        if error:
            response = { 'id':message_id, 'error':error }
            self.push_response(response)
        elif result != '':
            response = { 'id':message_id, 'result':result }
            self.push_response(response)


    def watch_address(self, addr):
        if addr not in self.watched_addresses:
            self.watched_addresses.append(addr)


    def run_store_iteration(self):
        if self.shared.stopped(): 
            print "exit timer"
            return
        
        block_number = self.store.main_iteration()
        if self.block_number != block_number:
            self.block_number = block_number
            print "block number:", self.block_number
            self.push_response({ 'method':'blockchain.numblocks.subscribe', 'params':[self.block_number] })

        while True:
            try:
                addr = self.store.address_queue.get(False)
            except:
                break
            if addr in self.watched_addresses:
                status = self.store.get_status( addr )
                self.push_response({ 'method':'blockchain.address.subscribe', 'params':[addr, status] })

        threading.Timer(10, self.run_store_iteration).start()


