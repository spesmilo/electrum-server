import bitcoin
from bitcoin import bind, _1, _2, _3
from processor import Processor
import threading
import time

import history 

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
        self.poller = bitcoin.poller(self.blockchain)
        self.transaction_pool = \
            bitcoin.transaction_pool(self.mempool_service, self.blockchain)

        self.protocol.subscribe_channel(self.monitor_tx)
        self.session = \
            bitcoin.session(self.network_service, self.hosts, self.handshake,
                            self.network, self.protocol, self.blockchain,
                            self.poller, self.transaction_pool)
        self.session.start(self.handle_start)

        self.pool_buffer = history.MemoryPoolBuffer(self.transaction_pool,
                                                    self.blockchain)

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
        self.pool_buffer.recv_tx(tx, bind(self.store_tx, _1, tx_hash))
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
                    "result": latest}
        self.processor.push_response(response)
        self.backend.blockchain.subscribe_reorganize(self.reorganize)

    def subscribe(self, request):
        latest = self.latest.get()
        response = {"id": request["id"],
                    "method": "blockchain.numblocks.subscribe",
                    "result": latest,
                    "error": None}
        self.processor.push_response(response)

class AddressGetHistory:

    def __init__(self, backend, processor):
        self.backend = backend
        self.processor = processor

    def get(self, request):
        address = str(request["params"][0])
        chain = self.backend.blockchain
        txpool = self.backend.transaction_pool
        membuf = self.backend.pool_buffer
        history.payment_history(chain, txpool, membuf, address,
            bind(self.respond, _1, request))

    def respond(self, result, request):
        if result is None:
            response = {"id": request["id"], "result": None,
                        "error": {"message": "Error", "code": -4}}
        else:
            response = {"id": request["id"], "result": result, "error": None}
        self.processor.push_response(response)

class AddressSubscribe:

    def __init__(self, backend, processor):
        self.backend = backend
        self.processor = processor

    def subscribe(self, session, request):
        address = str(request["params"][0])
        chain = self.backend.blockchain
        txpool = self.backend.transaction_pool
        membuf = self.backend.pool_buffer
        history.payment_history(chain, txpool, membuf, address,
            bind(self.respond, _1, request))

    def construct(self, result, request):
        if result is None:
            response = {"id": request["id"], "result": None,
                        "error": {"message": "Error", "code": -4}}
            return
        else:
            response = {"id": request["id"], "result": result, "error": None}
        self.processor.push_response(response)

class BlockchainProcessor(Processor):

    def __init__(self, config):
        Processor.__init__(self)
        self.backend = Backend()
        self.numblocks_subscribe = NumblocksSubscribe(self.backend, self)
        self.address_get_history = AddressGetHistory(self.backend, self)
        self.address_subscribe = AddressSubscribe(self.backend, self)

    def stop(self):
        self.backend.stop()

    def process(self, request):
        print "New request (lib)", request
        if request["method"] == "blockchain.numblocks.subscribe":
            self.numblocks_subscribe.subscribe(request)
        elif request["method"] == "blockchain.address.subscribe":
            pass
        elif request["method"] == "blockchain.address.get_history":
            self.address_get_history.get(request)
        elif request["method"] == "blockchain.transaction.broadcast":
            self.broadcast_transaction(request)

    def broadcast_transaction(self, request):
        raw_tx = bitcoin.data_chunk(str(request["params"]))
        exporter = bitcoin.satoshi_exporter()
        try:
            tx = exporter.load_transaction(raw_tx)
        except RuntimeError:
            response = {"id": request["id"], "result": None,
                        "error": {"message": 
                            "Exception while parsing the transaction data.",
                            "code": -4}}
        else:
            self.backend.protocol.broadcast_transaction(tx)
            tx_hash = str(bitcoin.hash_transaction(tx))
            response = {"id": request["id"], "result": tx_hash, "error": None}
        self.push_response(response)

    def run(self):
        print "Warning: pre-alpha prototype. Full of bugs."
        while not self.shared.stopped():
            time.sleep(1)

