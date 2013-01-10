import threading
import time

import bitcoin
from bitcoin import bind, _1, _2, _3
import multimap


class ExpiryQueue(threading.Thread):

    def __init__(self):
        self.lock = threading.Lock()
        self.items = []
        threading.Thread.__init__(self)
        self.daemon = True

    def run(self):
        # Garbage collection
        while True:
            with self.lock:
                self.items = [i for i in self.items if not i.stopped()]
            time.sleep(0.1)

    def add(self, item):
        with self.lock:
            self.items.append(item)


expiry_queue = ExpiryQueue()


class MemoryPoolBuffer:

    def __init__(self, txpool, chain, monitor):
        self.txpool = txpool
        self.chain = chain
        self.monitor = monitor
        # prevout: inpoint
        self.lookup_input = {}
        # payment_address: outpoint
        self.lookup_address = multimap.MultiMap()
        # transaction timestamps
        self.timestamps = {}

    def recv_tx(self, tx, handle_store):
        tx_hash = str(bitcoin.hash_transaction(tx))
        desc = (tx_hash, [], [])
        for input in tx.inputs:
            prevout = input.previous_output
            desc[1].append((str(prevout.hash), prevout.index))
        for idx, output in enumerate(tx.outputs):
            address = bitcoin.payment_address()
            if address.extract(output.output_script):
                desc[2].append((idx, str(address)))
        self.txpool.store(
            tx,
            bind(self.confirmed, _1, desc),
            bind(self.mempool_stored, _1, desc, handle_store)
        )

    def mempool_stored(self, ec, desc, handle_store):
        tx_hash, prevouts, addrs = desc
        if ec:
            handle_store(ec)
            return
        for idx, prevout in enumerate(prevouts):
            #inpoint = bitcoin.input_point()
            #inpoint.hash, inpoint.index = tx_hash, idx
            prevout = "%s:%s" % prevout
            self.lookup_input[prevout] = tx_hash, idx
        for idx, address in addrs:
            #outpoint = bitcoin.output_point()
            #outpoint.hash, outpoint.index = tx_hash, idx
            self.lookup_address[str(address)] = tx_hash, idx
        self.timestamps[tx_hash] = int(time.time())
        handle_store(ec)
        self.monitor.tx_stored(desc)

    def confirmed(self, ec, desc):
        tx_hash, prevouts, addrs = desc
        if ec:
            print "Problem confirming transaction", tx_hash, ec
            return
        print "Confirmed", tx_hash
        for idx, prevout in enumerate(prevouts):
            #inpoint = bitcoin.input_point()
            #inpoint.hash, inpoint.index = tx_hash, idx
            prevout = "%s:%s" % prevout
            assert self.lookup_input[prevout] == (tx_hash, idx)
            del self.lookup_input[prevout]
        for idx, address in addrs:
            #outpoint = bitcoin.output_point()
            #outpoint.hash, outpoint.index = tx_hash, idx
            outpoint = tx_hash, idx
            self.lookup_address.delete(str(address), outpoint)
        del self.timestamps[tx_hash]
        self.monitor.tx_confirmed(desc)

    def check(self, output_points, address, handle):
        class ExtendableDict(dict):
            pass
        result = []
        for outpoint in output_points:
            if str(outpoint) in self.lookup_input:
                point = self.lookup_input[str(outpoint)]
                info = ExtendableDict()
                info["tx_hash"] = point[0]
                info["index"] = point[1]
                info["is_input"] = 1
                info["timestamp"] = self.timestamps[info["tx_hash"]]
                result.append(info)
        if str(address) in self.lookup_address:
            addr_points = self.lookup_address[str(address)]
            for point in addr_points:
                info = ExtendableDict()
                info["tx_hash"] = str(point.hash)
                info["index"] = point.index
                info["is_input"] = 0
                info["timestamp"] = self.timestamps[info["tx_hash"]]
                result.append(info)
        handle(result)


class PaymentEntry:

    def __init__(self, output_point):
        self.lock = threading.Lock()
        self.output_point = output_point
        self.output_loaded = None
        self.input_point = None
        self.input_loaded = None
        self.raw_output_script = None

    def is_loaded(self):
        with self.lock:
            if self.output_loaded is None:
                return False
            elif self.has_input() and self.input_loaded is None:
                return False
        return True

    def has_input(self):
        return self.input_point is not False


class History:

    def __init__(self, chain, txpool, membuf):
        self.chain = chain
        self.txpool = txpool
        self.membuf = membuf
        self.lock = threading.Lock()
        self._stopped = False

    def start(self, address, handle_finish):
        self.statement = []
        self.membuf_result = None
        self.address = address
        self.handle_finish = handle_finish

        address = bitcoin.payment_address(address)
        # To begin we fetch all the outputs (payments in)
        # associated with this address
        self.chain.fetch_outputs(address, bind(self.check_membuf, _1, _2))

    def stop(self):
        with self.lock:
            assert self._stopped is False
            self._stopped = True

    def stopped(self):
        with self.lock:
            return self._stopped

    def stop_on_error(self, ec):
        if ec:
            self.handle_finish(None)
            self.stop()
        return self.stopped()

    def check_membuf(self, ec, output_points):
        if self.stop_on_error(ec):
            return
        self.membuf.check(output_points, self.address, bind(self.start_loading, _1, output_points))

    def start_loading(self, membuf_result, output_points):
        if len(membuf_result) == 0 and len(output_points) == 0:
            self.handle_finish([])
            self.stopped()
        # Create a bunch of entry lines which are outputs and
        # then their corresponding input (if it exists)
        for outpoint in output_points:
            entry = PaymentEntry(outpoint)
            with self.lock:
                self.statement.append(entry)
            # Attempt to fetch the spend of this output
            self.chain.fetch_spend(outpoint, bind(self.load_spend, _1, _2, entry))
            self.load_tx_info(outpoint, entry, False)
        # Load memory pool transactions
        with self.lock:
            self.membuf_result = membuf_result
        for info in self.membuf_result:
            self.txpool.fetch(bitcoin.hash_digest(info["tx_hash"]), bind(self.load_pool_tx, _1, _2, info))

    def load_spend(self, ec, inpoint, entry):
        # Need a custom self.stop_on_error(...) as a missing spend
        # is not an error in this case.
        if not self.stopped() and ec and ec != bitcoin.error.unspent_output:
            self.handle_finish(None)
            self.stop()
        if self.stopped():
            return
        with entry.lock:
            if ec == bitcoin.error.unspent_output:
                # This particular entry.output_point
                # has not been spent yet
                entry.input_point = False
            else:
                entry.input_point = inpoint
        if ec == bitcoin.error.unspent_output:
            # Attempt to stop if all the info for the inputs and outputs
            # has been loaded.
            self.finish_if_done()
        else:
            # We still have to load at least one more payment outwards
            self.load_tx_info(inpoint, entry, True)

    def finish_if_done(self):
        with self.lock:
            # Still have more entries to finish loading, so return
            if any(not entry.is_loaded() for entry in self.statement):
                return
            # Memory buffer transactions finished loading?
            if any("height" not in info for info in self.membuf_result):
                return
        # Whole operation completed successfully! Finish up.
        result = []
        for entry in self.statement:
            if entry.input_point:
                # value of the input is simply the inverse of
                # the corresponding output
                entry.input_loaded["value"] = -entry.output_loaded["value"]
                # output should come before the input as it's chronological
                result.append(entry.output_loaded)
                result.append(entry.input_loaded)
            else:
                # Unspent outputs have a raw_output_script field
                assert entry.raw_output_script is not None
                entry.output_loaded["raw_output_script"] = \
                    entry.raw_output_script
                result.append(entry.output_loaded)
        mempool_result = []
        for info in self.membuf_result:
            # Lookup prevout in result
            # Set "value" field
            if info["is_input"] == 1:
                prevout_tx = None
                for prevout_info in result:
                    if prevout_info["tx_hash"] == info.previous_output.hash:
                        prevout_tx = prevout_info
                assert prevout_tx is not None
                info["value"] = -prevout_info["value"]
            mempool_result.append(info)
        result.extend(mempool_result)
        self.handle_finish(result)
        self.stop()

    def load_tx_info(self, point, entry, is_input):
        info = {}
        info["tx_hash"] = str(point.hash)
        info["index"] = point.index
        info["is_input"] = 1 if is_input else 0
        # Before loading the transaction, Stratum requires the hash
        # of the parent block, so we load the block depth and then
        # fetch the block header and hash it.
        self.chain.fetch_transaction_index(point.hash, bind(self.tx_index, _1, _2, _3, entry, info))

    def tx_index(self, ec, block_depth, offset, entry, info):
        if self.stop_on_error(ec):
            return
        info["height"] = block_depth
        # And now for the block hash
        self.chain.fetch_block_header_by_depth(block_depth, bind(self.block_header, _1, _2, entry, info))

    def block_header(self, ec, blk_head, entry, info):
        if self.stop_on_error(ec):
            return
        info["timestamp"] = blk_head.timestamp
        info["block_hash"] = str(bitcoin.hash_block_header(blk_head))
        tx_hash = bitcoin.hash_digest(info["tx_hash"])
        # Now load the actual main transaction for this input or output
        self.chain.fetch_transaction(tx_hash, bind(self.load_chain_tx, _1, _2, entry, info))

    def load_pool_tx(self, ec, tx, info):
        if self.stop_on_error(ec):
            return
        # block_hash = mempool:5
        # inputs (load from prevtx)
        # outputs (load from tx)
        # raw_output_script (load from tx)
        # height is always None
        # value (get from finish_if_done)
        self.load_tx(tx, info)
        if info["is_input"] == 0:
            our_output = tx.outputs[info["index"]]
            info["value"] = our_output.value
            # Save serialised output script in case this output is unspent
            info["raw_output_script"] = \
                str(bitcoin.save_script(our_output.output_script))
        else:
            assert(info["is_input"] == 1)
            info.previous_output = tx.inputs[info["index"]].previous_output
        # If all the inputs are loaded
        if self.inputs_all_loaded(info["inputs"]):
            # We are the sole input
            assert(info["is_input"] == 1)
            # No more inputs left to load
            # This info has finished loading
            info["height"] = None
            info["block_hash"] = "mempool"
            self.finish_if_done()
        create_handler = lambda prevout_index, input_index: \
            bind(self.load_input_pool_tx, _1, _2, prevout_index, info, input_index)
        self.fetch_input_txs(tx, info, create_handler)

    def load_tx(self, tx, info):
        # List of output addresses
        outputs = []
        for tx_out in tx.outputs:
            address = bitcoin.payment_address()
            # Attempt to extract address from output script
            if address.extract(tx_out.output_script):
                outputs.append(address.encoded())
            else:
                # ... otherwise append "Unknown"
                outputs.append("Unknown")
        info["outputs"] = outputs
        # For the inputs, we need the originator address which has to
        # be looked up in the blockchain.
        # Create list of Nones and then populate it.
        # Loading has finished when list is no longer all None.
        info["inputs"] = [None for i in tx.inputs]
        # If this transaction was loaded for an input, then we already
        # have a source address for at least one input.
        if info["is_input"] == 1:
            info["inputs"][info["index"]] = self.address

    def fetch_input_txs(self, tx, info, create_handler):
        # Load the previous_output for every input so we can get
        # the output address
        for input_index, tx_input in enumerate(tx.inputs):
            if info["is_input"] == 1 and info["index"] == input_index:
                continue
            prevout = tx_input.previous_output
            handler = create_handler(prevout.index, input_index)
            self.chain.fetch_transaction(prevout.hash, handler)

    def load_chain_tx(self, ec, tx, entry, info):
        if self.stop_on_error(ec):
            return
        self.load_tx(tx, info)
        if info["is_input"] == 0:
            our_output = tx.outputs[info["index"]]
            info["value"] = our_output.value
            # Save serialised output script in case this output is unspent
            with entry.lock:
                entry.raw_output_script = \
                    str(bitcoin.save_script(our_output.output_script))
        # If all the inputs are loaded
        if self.inputs_all_loaded(info["inputs"]):
            # We are the sole input
            assert(info["is_input"] == 1)
            with entry.lock:
                entry.input_loaded = info
            self.finish_if_done()
        create_handler = lambda prevout_index, input_index: \
            bind(self.load_input_chain_tx, _1, _2, prevout_index, entry, info, input_index)
        self.fetch_input_txs(tx, info, create_handler)

    def inputs_all_loaded(self, info_inputs):
        return not [empty_in for empty_in in info_inputs if empty_in is None]

    def load_input_tx(self, tx, output_index, info, input_index):
        # For our input, we load the previous tx so we can get the
        # corresponding output.
        # We need the output to extract the address.
        script = tx.outputs[output_index].output_script
        address = bitcoin.payment_address()
        if address.extract(script):
            info["inputs"][input_index] = address.encoded()
        else:
            info["inputs"][input_index] = "Unknown"

    def load_input_chain_tx(self, ec, tx, output_index,
                            entry, info, input_index):
        if self.stop_on_error(ec):
            return
        self.load_input_tx(tx, output_index, info, input_index)
        # If all the inputs are loaded, then we have finished loading
        # the info for this input-output entry pair
        if self.inputs_all_loaded(info["inputs"]):
            with entry.lock:
                if info["is_input"] == 1:
                    entry.input_loaded = info
                else:
                    entry.output_loaded = info
        self.finish_if_done()

    def load_input_pool_tx(self, ec, tx, output_index, info, input_index):
        if self.stop_on_error(ec):
            return
        self.load_input_tx(tx, output_index, info, input_index)
        if not [inp for inp in info["inputs"] if inp is None]:
            # No more inputs left to load
            # This info has finished loading
            info["height"] = None
            info["block_hash"] = "mempool"
        self.finish_if_done()


def payment_history(chain, txpool, membuf, address, handle_finish):
    h = History(chain, txpool, membuf)
    expiry_queue.add(h)
    h.start(address, handle_finish)


if __name__ == "__main__":
    ex = bitcoin.satoshi_exporter()
    tx_a = bitcoin.data_chunk("0100000003d0406a31f628e18f5d894b2eaf4af719906dc61be4fb433a484ed870f6112d15000000008b48304502210089c11db8c1524d8839243803ac71e536f3d876e8265bbb3bc4a722a5d0bd40aa022058c3e59a7842ef1504b1c2ce048f9af2d69bbf303401dced1f68b38d672098a10141046060f6c8e355b94375eec2cc1d231f8044e811552d54a7c4b36fe8ee564861d07545c6c9d5b9f60d16e67d683b93486c01d3bd3b64d142f48af70bb7867d0ffbffffffff6152ed1552b1f2635317cea7be06615a077fc0f4aa62795872836c4182ca0f25000000008b48304502205f75a468ddb08070d235f76cb94c3f3e2a75e537bc55d087cc3e2a1559b7ac9b022100b17e4c958aaaf9b93359f5476aa5ed438422167e294e7207d5cfc105e897ed91014104a7108ec63464d6735302085124f3b7a06aa8f9363eab1f85f49a21689b286eb80fbabda7f838d9b6bff8550b377ad790b41512622518801c5230463dbbff6001ffffffff01c52914dcb0f3d8822e5a9e3374e5893a7b6033c9cfce5a8e5e6a1b3222a5cb010000008c4930460221009561f7206cc98f40f3eab5f3308b12846d76523bd07b5f058463f387694452b2022100b2684ec201760fa80b02954e588f071e46d0ff16562c1ab393888416bf8fcc44014104a7108ec63464d6735302085124f3b7a06aa8f9363eab1f85f49a21689b286eb80fbabda7f838d9b6bff8550b377ad790b41512622518801c5230463dbbff6001ffffffff02407e0f00000000001976a914c3b98829108923c41b3c1ba6740ecb678752fd5e88ac40420f00000000001976a914424648ea6548cc1c4ea707c7ca58e6131791785188ac00000000")
    tx_a = ex.load_transaction(tx_a)
    assert bitcoin.hash_transaction(tx_a) == "e72e4f025695446cfd5c5349d1720beb38801f329a00281f350cb7e847153397"
    tx_b = bitcoin.data_chunk("0100000001e269f0d74b8e6849233953715bc0be3ba6727afe0bc5000d015758f9e67dde34000000008c4930460221008e305e3fdf4420203a8cced5be20b73738a3b51186dfda7c6294ee6bebe331b7022100c812ded044196132f5e796dbf4b566b6ee3246cc4915eca3cf07047bcdf24a9301410493b6ce24182a58fc3bd0cbee0ddf5c282e00c0c10b1293c7a3567e95bfaaf6c9a431114c493ba50398ad0a82df06254605d963d6c226db615646fadd083ddfd9ffffffff020f9c1208000000001976a91492fffb2cb978d539b6bcd12c968b263896c6aacf88ac8e3f7600000000001976a914654dc745e9237f86b5fcdfd7e01165af2d72909588ac00000000")
    tx_b = ex.load_transaction(tx_b)
    assert bitcoin.hash_transaction(tx_b) == "acfda6dbf4ae1b102326bfb7c9541702d5ebb0339bc57bd74d36746855be8eac"

    def blockchain_started(ec, chain):
        print "Blockchain initialisation:", ec

    def store_tx(ec):
        print "Tx", ec

    def finish(result):
        print "Finish"
        if result is None:
            return
        for line in result:
            for k, v in line.iteritems():
                begin = k + ":"
                print begin, " " * (12 - len(begin)), v
            print

    class FakeMonitor:
        def tx_stored(self, tx):
            pass

        def tx_confirmed(self, tx):
            pass

    service = bitcoin.async_service(1)
    prefix = "/home/genjix/libbitcoin/database"
    chain = bitcoin.bdb_blockchain(service, prefix, blockchain_started)
    txpool = bitcoin.transaction_pool(service, chain)
    membuf = MemoryPoolBuffer(txpool, chain, FakeMonitor())
    membuf.recv_tx(tx_a, store_tx)
    membuf.recv_tx(tx_b, store_tx)

    txdat = bitcoin.data_chunk("0100000001d6cad920a04acd6c0609cd91fe4dafa1f3b933ac90e032c78fdc19d98785f2bb010000008b483045022043f8ce02784bd7231cb362a602920f2566c18e1877320bf17d4eabdac1019b2f022100f1fd06c57330683dff50e1b4571fb0cdab9592f36e3d7e98d8ce3f94ce3f255b01410453aa8d5ddef56731177915b7b902336109326f883be759ec9da9c8f1212c6fa3387629d06e5bf5e6bcc62ec5a70d650c3b1266bb0bcc65ca900cff5311cb958bffffffff0280969800000000001976a9146025cabdbf823949f85595f3d1c54c54cd67058b88ac602d2d1d000000001976a914c55c43631ab14f7c4fd9c5f153f6b9123ec32c8888ac00000000")
    req = {"id": 110, "params": ["1GULoCDnGjhfSWzHs6zDzBxbKt9DR7uRbt"]}
    ex = bitcoin.satoshi_exporter()
    tx = ex.load_transaction(txdat)
    time.sleep(4)
    membuf.recv_tx(tx, store_tx)

    raw_input()
    address = "1Jqu2PVGDvNv4La113hgCJsvRUCDb3W65D", "1GULoCDnGjhfSWzHs6zDzBxbKt9DR7uRbt"
    #address = "1Pbn3DLXfjqF1fFV9YPdvpvyzejZwkHhZE"
    print "Looking up", address
    payment_history(chain, txpool, membuf, address[0], finish)
    #payment_history(chain, txpool, membuf, address[1], finish)
    raw_input()
    print "Stopping..."
