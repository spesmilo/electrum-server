import threading
import time

import bitcoin


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


class StatementLine:

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
            elif (self.input_point is not False and
                  self.input_loaded is None):
                return False
        return True


class PaymentHistory:

    def __init__(self, chain):
        self.chain = chain
        self.lock = threading.Lock()
        self.statement = []
        self._stopped = False

    def run(self, address, handle_finish):
        self.address = address
        self.handle_finish = handle_finish

        pubkey_hash = bitcoin.address_to_short_hash(address)
        self.chain.fetch_outputs(pubkey_hash, self.start_loading)

    def start_loading(self, ec, output_points):
        with self.lock:
            for outpoint in output_points:
                statement_line = StatementLine(outpoint)
                self.statement.append(statement_line)
                self.chain.fetch_spend(
                    outpoint,
                    bitcoin.bind(self.load_spend, bitcoin._1, bitcoin._2, statement_line)
                )
                self.load_tx_info(outpoint, statement_line, False)

    def load_spend(self, ec, inpoint, statement_line):
        with statement_line.lock:
            if ec:
                statement_line.input_point = False
            else:
                statement_line.input_point = inpoint
        self.finish_if_done()
        if not ec:
            self.load_tx_info(inpoint, statement_line, True)

    def finish_if_done(self):
        with self.lock:
            if any(not line.is_loaded() for line in self.statement):
                return
        result = []
        for line in self.statement:
            if line.input_point:
                line.input_loaded["value"] = -line.output_loaded["value"]
                result.append(line.input_loaded)
            else:
                line.output_loaded["raw_output_script"] = line.raw_output_script
            result.append(line.output_loaded)
        self.handle_finish(result)
        self.stop()

    def stop(self):
        with self.lock:
            self._stopped = True

    def stopped(self):
        with self.lock:
            return self._stopped

    def load_tx_info(self, point, statement_line, is_input):
        info = {}
        info["tx_hash"] = str(point.hash)
        info["index"] = point.index
        info["is_input"] = 1 if is_input else 0
        self.chain.fetch_transaction_index(
            point.hash,
            bitcoin.bind(self.tx_index, bitcoin._1, bitcoin._2, bitcoin._3, statement_line, info)
        )

    def tx_index(self, ec, block_depth, offset, statement_line, info):
        info["height"] = block_depth
        self.chain.fetch_block_header_by_depth(
            block_depth,
            bitcoin.bind(self.block_header, bitcoin._1, bitcoin._2, statement_line, info)
        )

    def block_header(self, ec, blk_head, statement_line, info):
        info["timestamp"] = blk_head.timestamp
        info["block_hash"] = str(bitcoin.hash_block_header(blk_head))
        tx_hash = bitcoin.hash_digest(info["tx_hash"])
        self.chain.fetch_transaction(
            tx_hash,
            bitcoin.bind(self.load_tx, bitcoin._1, bitcoin._2, statement_line, info)
        )

    def load_tx(self, ec, tx, statement_line, info):
        outputs = []
        for tx_out in tx.outputs:
            script = tx_out.output_script
            if script.type() == bitcoin.payment_type.pubkey_hash:
                pkh = bitcoin.short_hash(str(script.operations()[2].data))
                outputs.append(bitcoin.public_key_hash_to_address(pkh))
            else:
                outputs.append("Unknown")
        info["outputs"] = outputs
        info["inputs"] = [None for i in range(len(tx.inputs))]
        if info["is_input"] == 1:
            info["inputs"][info["index"]] = self.address
        else:
            our_output = tx.outputs[info["index"]]
            info["value"] = our_output.value
            with statement_line.lock:
                statement_line.raw_output_script = \
                    str(bitcoin.save_script(our_output.output_script))
        if not [empty_in for empty_in in info["inputs"] if empty_in is None]:
            # We have the sole input
            assert(info["is_input"] == 1)
            with statement_line.lock:
                statement_line.input_loaded = info
            self.finish_if_done()
        for tx_idx, tx_in in enumerate(tx.inputs):
            if info["is_input"] == 1 and info["index"] == tx_idx:
                continue
            self.chain.fetch_transaction(
                tx_in.previous_output.hash,
                bitcoin.bind(self.load_input, bitcoin._1, bitcoin._2, tx_in.previous_output.index, statement_line, info, tx_idx)
            )

    def load_input(self, ec, tx, index, statement_line, info, inputs_index):
        script = tx.outputs[index].output_script
        if script.type() == bitcoin.payment_type.pubkey_hash:
            pkh = bitcoin.short_hash(str(script.operations()[2].data))
            info["inputs"][inputs_index] = \
                bitcoin.public_key_hash_to_address(pkh)
        else:
            info["inputs"][inputs_index] = "Unknown"
        if not [empty_in for empty_in in info["inputs"] if empty_in is None]:
            with statement_line.lock:
                if info["is_input"] == 1:
                    statement_line.input_loaded = info
                else:
                    statement_line.output_loaded = info
        self.finish_if_done()


def payment_history(chain, address, handle_finish):
    ph = PaymentHistory(chain)
    expiry_queue.add(ph)
    ph.run(address, handle_finish)


if __name__ == "__main__":
    def finish(result):
        print result

    def last(ec, depth):
        print "D:", depth

    service = bitcoin.async_service(1)
    prefix = "/home/genjix/libbitcoin/database"
    chain = bitcoin.bdb_blockchain(service, prefix)
    chain.fetch_last_depth(last)
    address = "1Pbn3DLXfjqF1fFV9YPdvpvyzejZwkHhZE"
    print "Looking up", address
    payment_history(chain, address, finish)
    raw_input()
