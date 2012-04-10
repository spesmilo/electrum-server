import bitcoin
from bitcoin import bind, _1, _2, _3
import threading

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

    def __init__(self, chain):
        self.chain = chain
        self.lock = threading.Lock()
        self.statement = []
        self._stopped = False

    def start(self, address, handle_finish):
        self.address = address
        self.handle_finish = handle_finish

        address = bitcoin.payment_address(address)
        # To begin we fetch all the outputs (payments in)
        # associated with this address
        self.chain.fetch_outputs(address, self.start_loading)

    def stop(self):
        with self.lock:
            assert self._stopped == False
            self._stopped = True

    def stopped(self):
        with self.lock:
            return self._stopped

    def stop_on_error(self, ec):
        if ec:
            self.handle_finish(None)
            self.stop()
        return self.stopped()

    def start_loading(self, ec, output_points):
        if self.stop_on_error(ec):
            return
        # Create a bunch of entry lines which are outputs and
        # then their corresponding input (if it exists)
        for outpoint in output_points:
            entry = PaymentEntry(outpoint)
            with self.lock:
                self.statement.append(entry)
            # Attempt to fetch the spend of this output
            self.chain.fetch_spend(outpoint,
                bind(self.load_spend, _1, _2, entry))
            self.load_tx_info(outpoint, entry, False)

    def load_spend(self, ec, inpoint, entry):
        # Need a custom self.stop_on_error(...) as a missing spend
        # is not an error in this case.
        if ec and ec != bitcoin.error.missing_object:
            self.stop()
        if self.stopped():
            return
        with entry.lock:
            if ec == bitcoin.error.missing_object:
                # This particular entry.output_point
                # has not been spent yet
                entry.input_point = False
            else:
                entry.input_point = inpoint
        if ec == bitcoin.error.missing_object:
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
        self.chain.fetch_transaction_index(point.hash,
            bind(self.tx_index, _1, _2, _3, entry, info))

    def tx_index(self, ec, block_depth, offset, entry, info):
        if self.stop_on_error(ec):
            return
        info["height"] = block_depth
        # And now for the block hash
        self.chain.fetch_block_header_by_depth(block_depth,
            bind(self.block_header, _1, _2, entry, info))

    def block_header(self, ec, blk_head, entry, info):
        if self.stop_on_error(ec):
            return
        info["timestamp"] = blk_head.timestamp
        info["block_hash"] = str(bitcoin.hash_block_header(blk_head))
        tx_hash = bitcoin.hash_digest(info["tx_hash"])
        # Now load the actual main transaction for this input or output
        self.chain.fetch_transaction(tx_hash,
            bind(self.load_tx, _1, _2, entry, info))

    def load_tx(self, ec, tx, entry, info):
        if self.stop_on_error(ec):
            return
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
        else:
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
        # Load the previous_output for every input so we can get
        # the output address
        for input_index, tx_input in enumerate(tx.inputs):
            if info["is_input"] == 1 and info["index"] == input_index:
                continue
            prevout = tx_input.previous_output
            self.chain.fetch_transaction(prevout.hash,
                bind(self.load_input_tx, _1, _2,
                     prevout.index, entry, info, input_index))

    def inputs_all_loaded(self, info_inputs):
        return not [empty_in for empty_in in info_inputs if empty_in is None]

    def load_input_tx(self, ec, tx, output_index, entry, info, input_index):
        if self.stop_on_error(ec):
            return
        # For our input, we load the previous tx so we can get the
        # corresponding output.
        # We need the output to extract the address.
        script = tx.outputs[output_index].output_script
        address = bitcoin.payment_address()
        if address.extract(script):
            info["inputs"][input_index] = address.encoded()
        else:
            info["inputs"][input_index] = "Unknown"
        # If all the inputs are loaded, then we have finished loading
        # the info for this input-output entry pair
        if self.inputs_all_loaded(info["inputs"]):
            with entry.lock:
                if info["is_input"] == 1:
                    entry.input_loaded = info
                else:
                    entry.output_loaded = info
        self.finish_if_done()

if __name__ == "__main__":
    def blockchain_started(ec, chain):
        print "Blockchain initialisation:", ec
    def finish(result):
        print result

    service = bitcoin.async_service(1)
    prefix = "/home/genjix/libbitcoin/database"
    chain = bitcoin.bdb_blockchain(service, prefix, blockchain_started)
    address = "1Pbn3DLXfjqF1fFV9YPdvpvyzejZwkHhZE"
    print "Looking up", address
    h = History(chain)
    h.start(address, finish)
    raw_input()
    print "Stopping..."

