import threading
import time

import bitcoin
from bitcoin import bind, _1, _2, _3

from processor import Processor
import history1 as history
import membuf


class HistoryCache:

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}

    def store(self, address, result):
        with self.lock:
            self.cache[address] = result

    def fetch(self, address):
        try:
            with self.lock:
                return self.cache[address]
        except KeyError:
            return None

    def clear(self, addresses):
        with self.lock:
            for address in addresses:
                if address in self.cache:
                    del self.cache[address]


class MonitorAddress:

    def __init__(self, processor, cache, backend):
        self.processor = processor
        self.cache = cache
        self.backend = backend
        self.lock = threading.Lock()
        # key is hash:index, value is address
        self.monitor_output = {}
        # key is address
        self.monitor_address = set()

        backend.memory_buffer.set_handles(self.tx_stored, self.tx_confirmed)

    def monitor(self, address, result):
        for info in result:
            if "raw_output_script" not in info:
                continue
            assert info["is_input"] == 0
            tx_hash = info["tx_hash"]
            output_index = info["index"]
            outpoint = "%s:%s" % (tx_hash, output_index)
            with self.lock:
                self.monitor_output[outpoint] = address
        with self.lock:
            self.monitor_address.add(address)

    def unpack(self, tx):
        tx_hash = bitcoin.hash_transaction(tx)
        previous_outputs = []
        for input in tx.inputs:
            prevout = input.previous_output
            prevout = "%s:%s" % (prevout.hash, prevout.index)
            previous_outputs.append(prevout)
        addrs = []
        for output_index, output in enumerate(tx.outputs):
            address = bitcoin.payment_address()
            if address.extract(output.output_script):
                addrs.append((output_index, str(address)))
        return tx_hash, previous_outputs, addrs

    def effect_notify(self, tx, delete_outs):
        affected_addrs = set()
        tx_hash, previous_outputs, addrs = self.unpack(tx)
        for prevout in previous_outputs:
            try:
                with self.lock:
                    affected_addrs.add(self.monitor_output[prevout])
                    if delete_outs:
                        del self.monitor_output[prevout]
            except KeyError:
                pass
        for idx, address in addrs:
            with self.lock:
                if address in self.monitor_address:
                    affected_addrs.add(address)
        self.cache.clear(affected_addrs)
        self.notify(affected_addrs)
        # Used in confirmed txs
        return tx_hash, addrs, affected_addrs

    def tx_stored(self, tx):
        self.effect_notify(tx, False)

    def tx_confirmed(self, tx):
        tx_hash, addrs, affected_addrs = self.effect_notify(tx, True)
        # add new outputs to monitor
        for idx, address in addrs:
            outpoint = "%s:%s" % (tx_hash, idx)
            if address in affected_addrs:
                with self.lock:
                    self.monitor_output[outpoint] = address

    def notify(self, affected_addrs):
        service = self.backend.mempool_service
        chain = self.backend.blockchain
        txpool = self.backend.transaction_pool
        memory_buff = self.backend.memory_buffer
        for address in affected_addrs:
            response = {"id": None,
                        "method": "blockchain.address.subscribe",
                        "params": [str(address)]}
            history.payment_history(service, chain, txpool, memory_buff, address,
                                    bind(self.send_notify, _1, _2, response))

    def mempool_n(self, result):
        assert result is not None
        if len(result) == 0:
            return None
        # mempool:n status
        # Order by time, grab last item (latest)
        last_info = sorted(result, key=lambda k: k['timestamp'])[-1]
        if last_info["block_hash"] == "mempool":
            last_id = "mempool:%s" % len(result)
        else:
            last_id = last_info["block_hash"]
        return last_id

    def send_notify(self, ec, result, response):
        if ec:
            print "Error: Monitor.send_notify()", ec
            return
        assert len(response["params"]) == 1
        response["params"].append(self.mempool_n(result))
        self.processor.push_response(response)


class Backend:

    def __init__(self):
        # Create 3 thread-pools each with 1 thread
        self.network_service = bitcoin.async_service(1)
        self.disk_service = bitcoin.async_service(1)
        self.mempool_service = bitcoin.async_service(1)

        self.hosts = bitcoin.hosts(self.network_service)
        self.handshake = bitcoin.handshake(self.network_service)
        self.network = bitcoin.network(self.network_service)
        self.protocol = bitcoin.protocol(self.network_service, self.hosts,
                                         self.handshake, self.network)

        db_prefix = "/home/genjix/libbitcoin/database"
        self.blockchain = bitcoin.bdb_blockchain(self.disk_service, db_prefix,
                                                 self.blockchain_started)
        self.poller = bitcoin.poller(self.mempool_service, self.blockchain)
        self.transaction_pool = \
            bitcoin.transaction_pool(self.mempool_service, self.blockchain)

        self.protocol.subscribe_channel(self.monitor_tx)
        self.session = \
            bitcoin.session(self.network_service, self.hosts, self.handshake,
                            self.network, self.protocol, self.blockchain,
                            self.poller, self.transaction_pool)
        self.session.start(self.handle_start)

        self.memory_buffer = \
            membuf.memory_buffer(self.mempool_service.internal_ptr,
                                 self.blockchain.internal_ptr,
                                 self.transaction_pool.internal_ptr)

    def handle_start(self, ec):
        if ec:
            print "Error starting backend:", ec

    def blockchain_started(self, ec, chain):
        print "Blockchain initialisation:", ec

    def stop(self):
        self.session.stop(self.handle_stop)

    def handle_stop(self, ec):
        if ec:
            print "Error stopping backend:", ec
        print "Stopped backend"

    def monitor_tx(self, node):
        # We will be notified here when connected to new bitcoin nodes
        # Here we subscribe to new transactions from them which we
        # add to the transaction_pool. That way we can track which
        # transactions we are interested in.
        node.subscribe_transaction(bind(self.recv_tx, _1, _2, node))
        # Re-subscribe to next new node
        self.protocol.subscribe_channel(self.monitor_tx)

    def recv_tx(self, ec, tx, node):
        if ec:
            print "Error with new transaction:", ec
            return
        tx_hash = bitcoin.hash_transaction(tx)
        self.memory_buffer.receive(tx, bind(self.store_tx, _1, tx_hash))
        # Re-subscribe to new transactions from node
        node.subscribe_transaction(bind(self.recv_tx, _1, _2, node))

    def store_tx(self, ec, tx_hash):
        if ec:
            print "Error storing memory pool transaction", tx_hash, ec
        else:
            print "Accepted transaction", tx_hash


class GhostValue:

    def __init__(self):
        self.event = threading.Event()
        self.value = None

    def get(self):
        self.event.wait()
        return self.value

    def set(self, value):
        self.value = value
        self.event.set()


class NumblocksSubscribe:

    def __init__(self, backend, processor):
        self.backend = backend
        self.processor = processor
        self.lock = threading.Lock()
        self.backend.blockchain.subscribe_reorganize(self.reorganize)
        self.backend.blockchain.fetch_last_depth(self.set_last_depth)
        self.latest = GhostValue()

    def set_last_depth(self, ec, last_depth):
        if ec:
            print "Error retrieving last depth", ec
        else:
            self.latest.set(last_depth)

    def reorganize(self, ec, fork_point, arrivals, replaced):
        latest = fork_point + len(arrivals)
        self.latest.set(latest)
        response = {"id": None, "method": "blockchain.numblocks.subscribe",
                    "params": [latest]}
        self.processor.push_response(response)
        self.backend.blockchain.subscribe_reorganize(self.reorganize)

    def subscribe(self, request):
        latest = self.latest.get()
        response = {"id": request["id"],
                    "result": latest,
                    "error": None}
        self.processor.push_response(response)


class AddressGetHistory:

    def __init__(self, backend, processor):
        self.backend = backend
        self.processor = processor

    def get(self, request):
        address = str(request["params"][0])
        service = self.backend.mempool_service
        chain = self.backend.blockchain
        txpool = self.backend.transaction_pool
        memory_buff = self.backend.memory_buffer
        history.payment_history(service, chain, txpool, memory_buff, address,
                                bind(self.respond, _1, _2, request))

    def respond(self, ec, result, request):
        if ec:
            response = {"id": request["id"], "result": None,
                        "error": {"message": str(ec), "code": -4}}
        else:
            response = {"id": request["id"], "result": result, "error": None}
        self.processor.push_response(response)


class AddressSubscribe:

    def __init__(self, backend, processor, cache, monitor):
        self.backend = backend
        self.processor = processor
        self.cache = cache
        self.monitor = monitor

    def subscribe(self, request):
        address = str(request["params"][0])
        service = self.backend.mempool_service
        chain = self.backend.blockchain
        txpool = self.backend.transaction_pool
        memory_buff = self.backend.memory_buffer
        history.payment_history(service, chain, txpool, memory_buff, address,
                                bind(self.construct, _1, _2, request))

    def construct(self, ec, result, request):
        if ec:
            response = {"id": request["id"], "result": None,
                        "error": {"message": str(ec), "code": -4}}
            self.processor.push_response(response)
            return
        last_id = self.monitor.mempool_n(result)
        response = {"id": request["id"], "result": last_id, "error": None}
        self.processor.push_response(response)
        address = request["params"][0]
        self.monitor.monitor(address, result)
        # Cache result for get_history
        self.cache.store(address, result)

    def fetch_cached(self, request):
        address = request["params"][0]
        cached = self.cache.fetch(address)
        if cached is None:
            return False
        response = {"id": request["id"], "result": cached, "error": None}
        self.processor.push_response(response)
        return True


class BlockchainProcessor(Processor):

    def __init__(self, config):
        Processor.__init__(self)
        cache = HistoryCache()
        self.backend = Backend()
        monitor = MonitorAddress(self, cache, self.backend)
        self.numblocks_subscribe = NumblocksSubscribe(self.backend, self)
        self.address_get_history = AddressGetHistory(self.backend, self)
        self.address_subscribe = \
            AddressSubscribe(self.backend, self, cache, monitor)

    def stop(self):
        self.backend.stop()

    def process(self, request):
        print "New request (lib)", request
        if request["method"] == "blockchain.numblocks.subscribe":
            self.numblocks_subscribe.subscribe(request)
        elif request["method"] == "blockchain.address.subscribe":
            self.address_subscribe.subscribe(request)
        elif request["method"] == "blockchain.address.get_history":
            if not self.address_subscribe.fetch_cached(request):
                self.address_get_history.get(request)
        elif request["method"] == "blockchain.transaction.broadcast":
            self.broadcast_transaction(request)

    def broadcast_transaction(self, request):
        raw_tx = bitcoin.data_chunk(str(request["params"][0]))
        exporter = bitcoin.satoshi_exporter()
        try:
            tx = exporter.load_transaction(raw_tx)
        except RuntimeError:
            response = {
                "id": request["id"],
                "result": None,
                "error": {
                    "message": "Exception while parsing the transaction data.",
                    "code": -4,
                }
            }
        else:
            self.backend.protocol.broadcast_transaction(tx)
            tx_hash = str(bitcoin.hash_transaction(tx))
            response = {"id": request["id"], "result": tx_hash, "error": None}
        self.push_response(response)
