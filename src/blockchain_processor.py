#!/usr/bin/env python
# Copyright(C) 2011-2016 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import hashlib
from json import dumps, load
import os
from Queue import Queue
import random
import sys
import time
import threading
import urllib

import deserialize
from processor import Processor, print_log
from storage import Storage
from utils import logger, hash_decode, hash_encode, Hash, header_from_string, header_to_string, ProfiledThread, \
    rev_hex, int_to_hex4

class BlockchainProcessor(Processor):

    def __init__(self, config, shared):
        Processor.__init__(self)

        # monitoring
        self.avg_time = 0,0,0
        self.time_ref = time.time()

        self.shared = shared
        self.config = config
        self.up_to_date = False

        self.watch_lock = threading.Lock()
        self.watch_blocks = []
        self.watch_headers = []
        self.watched_addresses = {}

        self.history_cache = {}
        self.merkle_cache = {}
        self.max_cache_size = 100000
        self.chunk_cache = {}
        self.cache_lock = threading.Lock()
        self.headers_data = ''
        self.headers_path = config.get('leveldb', 'path')

        self.mempool_fees = {}
        self.mempool_values = {}
        self.mempool_addresses = {}
        self.mempool_hist = {} # addr -> (txid, delta)
        self.mempool_unconfirmed = {} # txid -> set of unconfirmed inputs
        self.mempool_hashes = set()
        self.mempool_lock = threading.Lock()

        self.address_queue = Queue()

        try:
            self.test_reorgs = config.getboolean('leveldb', 'test_reorgs')   # simulate random blockchain reorgs
        except:
            self.test_reorgs = False
        self.storage = Storage(config, shared, self.test_reorgs)

        self.bitcoind_url = 'http://%s:%s@%s:%s/' % (
            config.get('bitcoind', 'bitcoind_user'),
            config.get('bitcoind', 'bitcoind_password'),
            config.get('bitcoind', 'bitcoind_host'),
            config.get('bitcoind', 'bitcoind_port'))

        self.sent_height = 0
        self.sent_header = None

        # catch_up headers
        self.init_headers(self.storage.height)
        # start catch_up thread
        if config.getboolean('leveldb', 'profiler'):
            filename = os.path.join(config.get('leveldb', 'path'), 'profile')
            print_log('profiled thread', filename)
            self.blockchain_thread = ProfiledThread(filename, target = self.do_catch_up)
        else:
            self.blockchain_thread = threading.Thread(target = self.do_catch_up)
        self.blockchain_thread.start()


    def do_catch_up(self):
        self.header = self.block2header(self.bitcoind('getblock', (self.storage.last_hash,)))
        self.header['utxo_root'] = self.storage.get_root_hash().encode('hex')
        self.catch_up(sync=False)
        if not self.shared.stopped():
            print_log("Blockchain is up to date.")
            self.memorypool_update()
            print_log("Memory pool initialized.")

        while not self.shared.stopped():
            self.main_iteration()
            if self.shared.paused():
                print_log("bitcoind is responding")
                self.shared.unpause()
            time.sleep(10)


    def set_time(self):
        self.time_ref = time.time()

    def print_time(self, num_tx):
        delta = time.time() - self.time_ref
        # leaky averages
        seconds_per_block, tx_per_second, n = self.avg_time
        alpha = (1. + 0.01 * n)/(n+1)
        seconds_per_block = (1-alpha) * seconds_per_block + alpha * delta
        alpha2 = alpha * delta / seconds_per_block
        tx_per_second = (1-alpha2) * tx_per_second + alpha2 * num_tx / delta
        self.avg_time = seconds_per_block, tx_per_second, n+1
        if self.storage.height%100 == 0 \
            or (self.storage.height%10 == 0 and self.storage.height >= 100000)\
            or self.storage.height >= 200000:
            msg = "block %d (%d %.2fs) %s" %(self.storage.height, num_tx, delta, self.storage.get_root_hash().encode('hex'))
            msg += " (%.2ftx/s, %.2fs/block)" % (tx_per_second, seconds_per_block)
            run_blocks = self.storage.height - self.start_catchup_height
            remaining_blocks = self.bitcoind_height - self.storage.height
            if run_blocks>0 and remaining_blocks>0:
                remaining_minutes = remaining_blocks * seconds_per_block / 60
                new_blocks = int(remaining_minutes / 10) # number of new blocks expected during catchup
                blocks_to_process = remaining_blocks + new_blocks
                minutes = blocks_to_process * seconds_per_block / 60
                rt = "%.0fmin"%minutes if minutes < 300 else "%.1f hours"%(minutes/60)
                msg += " (eta %s, %d blocks)" % (rt, remaining_blocks)
            print_log(msg)

    def wait_on_bitcoind(self):
        self.shared.pause()
        time.sleep(10)
        if self.shared.stopped():
            # this will end the thread
            raise BaseException()

    def bitcoind(self, method, params=()):
        postdata = dumps({"method": method, 'params': params, 'id': 'jsonrpc'})
        while True:
            try:
                response = urllib.urlopen(self.bitcoind_url, postdata)
                r = load(response)
                response.close()
            except:
                print_log("cannot reach bitcoind...")
                self.wait_on_bitcoind()
            else:
                if r['error'] is not None:
                    if r['error'].get('code') == -28:
                        print_log("bitcoind still warming up...")
                        self.wait_on_bitcoind()
                        continue
                    raise BaseException(r['error'])
                break
        return r.get('result')

    @staticmethod
    def block2header(b):
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
        block_hash = self.bitcoind('getblockhash', (height,))
        b = self.bitcoind('getblock', (block_hash,))
        return self.block2header(b)

    def init_headers(self, db_height):
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

    @staticmethod
    def hash_header(header):
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
            if chunk_index in self.chunk_cache:
                del self.chunk_cache[chunk_index]

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
                if chunk:
                    self.chunk_cache[i] = chunk

        return chunk

    def get_mempool_transaction(self, txid):
        try:
            raw_tx = self.bitcoind('getrawtransaction', (txid, 0))
        except:
            return None
        vds = deserialize.BCDataStream()
        vds.write(raw_tx.decode('hex'))
        try:
            return deserialize.parse_Transaction(vds, is_coinbase=False)
        except:
            print_log("ERROR: cannot parse", txid)
            return None

    def get_unconfirmed_history(self, addr):
        hist = []
        with self.mempool_lock:
            for tx_hash, delta in self.mempool_hist.get(addr, ()):
                height = -1 if self.mempool_unconfirmed.get(tx_hash) else 0
                fee = self.mempool_fees.get(tx_hash)
                hist.append({'tx_hash':tx_hash, 'height':height, 'fee':fee})
        return hist

    def get_history(self, addr, cache_only=False):
        with self.cache_lock:
            hist = self.history_cache.get(addr)
        if hist is not None:
            return hist
        if cache_only:
            return -1
        hist = self.storage.get_history(addr)
        hist.extend(self.get_unconfirmed_history(addr))
        with self.cache_lock:
            if len(self.history_cache) > self.max_cache_size:
                logger.info("clearing cache")
                self.history_cache.clear()
            self.history_cache[addr] = hist
        return hist

    def get_unconfirmed_value(self, addr):
        v = 0
        with self.mempool_lock:
            for txid, delta in self.mempool_hist.get(addr, ()):
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
        status = ''.join(tx.get('tx_hash') + ':%d:' % tx.get('height') for tx in tx_points)
        return hashlib.sha256(status).digest().encode('hex')

    def get_merkle(self, tx_hash, height, cache_only):
        with self.cache_lock:
            out = self.merkle_cache.get(tx_hash)
        if out is not None:
            return out
        if cache_only:
            return -1

        block_hash = self.bitcoind('getblockhash', (height,))
        b = self.bitcoind('getblock', (block_hash,))
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

        out = {"block_height": height, "merkle": s, "pos": tx_pos}
        with self.cache_lock:
            if len(self.merkle_cache) > self.max_cache_size:
                logger.info("clearing merkle cache")
                self.merkle_cache.clear()
            self.merkle_cache[tx_hash] = out
        return out

    @staticmethod
    def deserialize_block(block):
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



    def import_block(self, block, block_hash, block_height, revert=False):

        touched_addr = set()

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
        self.storage.save_height(block_hash, block_height)

        for addr in touched_addr:
            self.invalidate_cache(addr)

        self.storage.update_hashes()
        # batch write modified nodes 
        self.storage.batch_write()
        # return length for monitoring
        return len(tx_hashes)


    def add_request(self, session, request):
        # see if we can get if from cache. if not, add request to queue
        message_id = request.get('id')
        try:
            result = self.process(request, cache_only=True)
        except BaseException as e:
            self.push_response(session, {'id': message_id, 'error': str(e)})
            return 

        if result == -1:
            self.queue.put((session, request))
        else:
            self.push_response(session, {'id': message_id, 'result': result})


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
                    del self.watched_addresses[addr]


    def process(self, request, cache_only=False):
        
        message_id = request['id']
        method = request['method']
        params = request.get('params', ())
        result = None
        error = None

        if method == 'blockchain.numblocks.subscribe':
            result = self.storage.height

        elif method == 'blockchain.headers.subscribe':
            result = self.header

        elif method == 'blockchain.address.subscribe':
            address = str(params[0])
            result = self.get_status(address, cache_only)

        elif method == 'blockchain.address.get_history':
            address = str(params[0])
            result = self.get_history(address, cache_only)

        elif method == 'blockchain.address.get_mempool':
            address = str(params[0])
            result = self.get_unconfirmed_history(address)

        elif method == 'blockchain.address.get_balance':
            address = str(params[0])
            confirmed = self.storage.get_balance(address)
            unconfirmed = self.get_unconfirmed_value(address)
            result = { 'confirmed':confirmed, 'unconfirmed':unconfirmed }

        elif method == 'blockchain.address.get_proof':
            address = str(params[0])
            result = self.storage.get_proof(address)

        elif method == 'blockchain.address.listunspent':
            address = str(params[0])
            result = self.storage.listunspent(address)

        elif method == 'blockchain.utxo.get_address':
            txid = str(params[0])
            pos = int(params[1])
            txi = (txid + int_to_hex4(pos)).decode('hex')
            result = self.storage.get_address(txi)

        elif method == 'blockchain.block.get_header':
            if cache_only:
                result = -1
            else:
                height = int(params[0])
                result = self.get_header(height)

        elif method == 'blockchain.block.get_chunk':
            if cache_only:
                result = -1
            else:
                index = int(params[0])
                result = self.get_chunk(index)

        elif method == 'blockchain.transaction.broadcast':
            try:
                txo = self.bitcoind('sendrawtransaction', params)
                print_log("sent tx:", txo)
                result = txo
            except BaseException, e:
                error = e.args[0]
                if error["code"] == -26:
                    # If we return anything that's not the transaction hash,
                    #  it's considered an error message
                    message = error["message"]
                    if "non-mandatory-script-verify-flag" in message:
                        result = "Your client produced a transaction that is not accepted by the Bitcoin network any more. Please upgrade to Electrum 2.5.1 or newer\n"
                    else:
                        result = "The transaction was rejected by network rules.(" + message + ")\n" \
                            "[" + params[0] + "]"
                else:
                    result = error["message"]  # do send an error
                print_log("error:", result)

        elif method == 'blockchain.transaction.get_merkle':
            tx_hash = params[0]
            tx_height = params[1]
            result = self.get_merkle(tx_hash, tx_height, cache_only)

        elif method == 'blockchain.transaction.get':
            tx_hash = params[0]
            result = self.bitcoind('getrawtransaction', (tx_hash, 0))

        elif method == 'blockchain.estimatefee':
            num = int(params[0])
            result = self.bitcoind('estimatefee', (num,))

        elif method == 'blockchain.relayfee':
            result = self.relayfee

        else:
            raise BaseException("unknown method:%s" % method)

        return result


    def get_block(self, block_hash):
        block = self.bitcoind('getblock', (block_hash,))

        rawtxreq = []
        i = 0
        for txid in block['tx']:
            rawtxreq.append({
                "method": "getrawtransaction",
                "params": (txid,),
                "id": i,
            })
            i += 1
        postdata = dumps(rawtxreq)

        while True:
            try:
                response = urllib.urlopen(self.bitcoind_url, postdata)
                r = load(response)
                response.close()
            except:
                logger.error("bitcoind error (getfullblock)")
                self.wait_on_bitcoind()
                continue
            try:
                rawtxdata = []
                for ir in r:
                    assert ir['error'] is None, "Error: make sure you run bitcoind with txindex=1; use -reindex if needed."
                    rawtxdata.append(ir['result'])
            except BaseException as e:
                logger.error(str(e))
                self.wait_on_bitcoind()
                continue

            block['tx'] = rawtxdata
            return block



    def catch_up(self, sync=True):

        self.start_catchup_height = self.storage.height
        prev_root_hash = None
        n = 0

        while not self.shared.stopped():
            # are we done yet?
            info = self.bitcoind('getinfo')
            self.relayfee = info.get('relayfee')
            self.bitcoind_height = info.get('blocks')
            bitcoind_block_hash = self.bitcoind('getblockhash', (self.bitcoind_height,))
            if self.storage.last_hash == bitcoind_block_hash:
                self.up_to_date = True
                break

            self.set_time()

            revert = (random.randint(1, 100) == 1) if self.test_reorgs and self.storage.height>100 else False

            # not done..
            self.up_to_date = False
            try:
                next_block_hash = self.bitcoind('getblockhash', (self.storage.height + 1,))
            except BaseException, e:
                revert = True

            next_block = self.get_block(next_block_hash if not revert else self.storage.last_hash)

            if (next_block.get('previousblockhash') == self.storage.last_hash) and not revert:

                prev_root_hash = self.storage.get_root_hash()

                n = self.import_block(next_block, next_block_hash, self.storage.height+1)
                self.storage.height = self.storage.height + 1
                self.write_header(self.block2header(next_block), sync)
                self.storage.last_hash = next_block_hash

            else:

                # revert current block
                block = self.get_block(self.storage.last_hash)
                print_log("blockchain reorg", self.storage.height, block.get('previousblockhash'), self.storage.last_hash)
                n = self.import_block(block, self.storage.last_hash, self.storage.height, revert=True)
                self.pop_header()
                self.flush_headers()

                self.storage.height -= 1

                # read previous header from disk
                self.header = self.read_header(self.storage.height)
                self.storage.last_hash = self.hash_header(self.header)

                if prev_root_hash:
                    assert prev_root_hash == self.storage.get_root_hash()
                    prev_root_hash = None

            # print time
            self.print_time(n)

        self.header = self.block2header(self.bitcoind('getblock', (self.storage.last_hash,)))
        self.header['utxo_root'] = self.storage.get_root_hash().encode('hex')

        if self.shared.stopped(): 
            print_log( "closing database" )
            self.storage.close()


    def memorypool_update(self):
        t0 = time.time()
        mempool_hashes = set(self.bitcoind('getrawmempool'))
        touched_addresses = set()

        # get new transactions
        new_tx = {}
        for tx_hash in mempool_hashes:
            if tx_hash in self.mempool_hashes:
                continue

            tx = self.get_mempool_transaction(tx_hash)
            if not tx:
                continue

            new_tx[tx_hash] = tx

        # remove older entries from mempool_hashes
        self.mempool_hashes = mempool_hashes

        # check all tx outputs
        for tx_hash, tx in new_tx.iteritems():
            mpa = self.mempool_addresses.get(tx_hash, {})
            out_values = []
            out_sum = 0
            for x in tx.get('outputs'):
                addr = x.get('address', '')
                value = x['value']
                out_values.append((addr, value))
                if not addr:
                    continue
                v = mpa.get(addr, 0)
                v += value
                mpa[addr] = v
                touched_addresses.add(addr)
                out_sum += value

            self.mempool_fees[tx_hash] = -out_sum
            self.mempool_addresses[tx_hash] = mpa
            self.mempool_values[tx_hash] = out_values
            self.mempool_unconfirmed[tx_hash] = set()

        # check all inputs
        for tx_hash, tx in new_tx.iteritems():
            mpa = self.mempool_addresses.get(tx_hash, {})
            # are we spending unconfirmed inputs?
            input_sum = 0
            for x in tx.get('inputs'):
                prev_hash = x.get('prevout_hash')
                prev_n = x.get('prevout_n')
                mpv = self.mempool_values.get(prev_hash)
                if mpv:
                    addr, value = mpv[prev_n]
                    self.mempool_unconfirmed[tx_hash].add(prev_hash)
                else:
                    txi = (prev_hash + int_to_hex4(prev_n)).decode('hex')
                    try:
                        addr = self.storage.get_address(txi)
                        value = self.storage.get_utxo_value(addr,txi)
                    except:
                        print_log("utxo not in database; postponing mempool update")
                        return
                # we can proceed
                input_sum += value
                if not addr:
                    continue
                v = mpa.get(addr, 0)
                v -= value
                mpa[addr] = v
                touched_addresses.add(addr)
            self.mempool_addresses[tx_hash] = mpa
            self.mempool_fees[tx_hash] += input_sum

        # remove deprecated entries from mempool_addresses
        for tx_hash, addresses in self.mempool_addresses.items():
            if tx_hash not in self.mempool_hashes:
                del self.mempool_addresses[tx_hash]
                del self.mempool_values[tx_hash]
                del self.mempool_unconfirmed[tx_hash]
                del self.mempool_fees[tx_hash]
                touched_addresses.update(addresses)

        # remove deprecated entries from mempool_hist
        new_mempool_hist = {}
        for addr in self.mempool_hist.iterkeys():
            h = self.mempool_hist[addr]
            hh = []
            for tx_hash, delta in h:
                if tx_hash in self.mempool_addresses:
                    hh.append((tx_hash, delta))
            if hh:
                new_mempool_hist[addr] = hh
        # add new transactions to mempool_hist
        for tx_hash in new_tx.iterkeys():
            addresses = self.mempool_addresses[tx_hash]
            for addr, delta in addresses.iteritems():
                h = new_mempool_hist.get(addr, [])
                if (tx_hash, delta) not in h:
                    h.append((tx_hash, delta))
                new_mempool_hist[addr] = h

        with self.mempool_lock:
            self.mempool_hist = new_mempool_hist

        # invalidate cache for touched addresses
        for addr in touched_addresses:
            self.invalidate_cache(addr)

        t1 = time.time()
        if t1-t0>1:
            print_log('mempool_update', t1-t0, len(self.mempool_hashes), len(self.mempool_hist))


    def invalidate_cache(self, address):
        with self.cache_lock:
            if address in self.history_cache:
                # print_log("cache: invalidating", address)
                del self.history_cache[address]

        with self.watch_lock:
            sessions = self.watched_addresses.get(address)

        if sessions:
            # TODO: update cache here. if new value equals cached value, do not send notification
            self.address_queue.put((address,sessions))

    
    def close(self):
        self.blockchain_thread.join()
        print_log("Closing database...")
        self.storage.close()
        print_log("Database is closed")


    def main_iteration(self):
        if self.shared.stopped():
            print_log("Stopping timer")
            return

        self.catch_up()

        self.memorypool_update()

        if self.sent_height != self.storage.height:
            self.sent_height = self.storage.height
            for session in self.watch_blocks:
                self.push_response(session, {
                        'id': None,
                        'method': 'blockchain.numblocks.subscribe',
                        'params': (self.storage.height,),
                        })

        if self.sent_header != self.header:
            self.sent_header = self.header
            for session in self.watch_headers:
                self.push_response(session, {
                        'id': None,
                        'method': 'blockchain.headers.subscribe',
                        'params': (self.header,),
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
                        'params': (addr, status),
                        })


