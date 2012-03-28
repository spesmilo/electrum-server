import bitcoin
from processor import Processor
import threading
import time

import composed 

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
        self.blockchain = bitcoin.bdb_blockchain(self.disk_service, db_prefix)
        self.poller = bitcoin.poller(self.blockchain)
        self.transaction_pool = \
            bitcoin.transaction_pool(self.mempool_service, self.blockchain)

        self.protocol.subscribe_channel(self.monitor_tx)
        self.session = \
            bitcoin.session(self.hosts, self.handshake, self.network,
                            self.protocol, self.blockchain, self.poller,
                            self.transaction_pool)
        self.session.start(self.handle_start)

    def handle_start(self, ec):
        if ec:
            print "Error starting backend:", ec

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
        node.subscribe_transaction(
            bitcoin.bind(self.recv_tx, bitcoin._1, bitcoin._2, node))
        # Re-subscribe to next new node
        self.protocol.subscribe_channel(self.monitor_tx)

    def recv_tx(self, ec, tx, node):
        if ec:
            print "Error with new transaction:", ec
            return
        tx_hash = bitcoin.hash_transaction(tx)
        # If we want to ignore this transaction, we can set
        # the 2 handlers to be null handlers that do nothing.
        self.transaction_pool.store(tx,
            bitcoin.bind(self.tx_confirmed, bitcoin._1, tx_hash),
            bitcoin.bind(self.handle_mempool_store, bitcoin._1, tx_hash))
        # Re-subscribe to new transactions from node
        node.subscribe_transaction(
            bitcoin.bind(self.recv_tx, bitcoin._1, bitcoin._2, node))

    def handle_mempool_store(self, ec, tx_hash):
        if ec:
            print "Error storing memory pool transaction", tx_hash, ec
        else:
            print "Accepted transaction", tx_hash

    def tx_confirmed(self, ec, tx_hash):
        if ec:
            print "Problem confirming transaction", tx_hash, ec
        else:
            print "Confirmed", tx_hash

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

    def __init__(self, backend):
        self.backend = backend
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
        self.push_response({"method":"numblocks.subscribe", "result": latest})
        self.backend.blockchain.subscribe_reorganize(self.reorganize)


class AddressGetHistory:

    def __init__(self, backend):
        self.backend = backend

    def get(self, request):
        address = str(request["params"])
        composed.payment_history(self.backend.blockchain, address,
            bitcoin.bind(self.respond, request, bitcoin._1))

    def respond(self, request, result):
        self.push_response({"id": request["id"], "method":request["method"], "params":request["params"], "result": result})

class LibbitcoinProcessor(Processor):

    def __init__(self):
        self.backend = Backend()
        self.numblocks_subscribe = NumblocksSubscribe(self.backend)
        self.address_get_history = AddressGetHistory(self.backend)
        Processor.__init__(self)

    def stop(self):
        self.backend.stop()

    def process(self, request):

        print "New request (lib)", request
        if request["method"] == "numblocks.subscribe":
            self.numblocks_subscribe.subscribe(session, request)
        elif request["method"] == "address.get_history":
            self.address_get_history.get(request)
        elif request["method"] == "server.banner":
            self.push_response({"id": request["id"], "method": request["method"], "params":request["params"],
                "result": "libbitcoin using python-bitcoin bindings"})
        elif request["method"] == "transaction.broadcast":
            self.broadcast_transaction(request)
        # Execute and when ready, you call
        # self.push_response(response)

    def broadcast_transaction(self, request):
        raw_tx = bitcoin.data_chunk(str(request["params"]))
        exporter = bitcoin.satoshi_exporter()
        try:
            tx = exporter.load_transaction(raw_tx)
        except RuntimeError:
            response = {"id": request["id"], "method": request["method"], "params":request["params"], "result": None,
                "error": {"message": 
                    "Exception while parsing the transaction data.",
                    "code": -4}}
        else:
            self.backend.protocol.broadcast_transaction(tx)
            tx_hash = str(bitcoin.hash_transaction(tx))
            response = {"id": request["id"], "method": request["method"], "params":request["params"], "result": tx_hash}
        self.push_response(response)



def run(processor):
    #processor = LibbitcoinProcessor()
    print "Warning: pre-alpha prototype. Full of bugs."
    while not processor.shared.stopped():
        if raw_input() == "quit":
            shared.stop()
        time.sleep(1)

