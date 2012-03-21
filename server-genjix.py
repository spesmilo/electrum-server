import bitcoin
import stratum
import threading
import time

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

    def subscribe(self, session, request):
        last = self.latest.get()
        session.push_response({"id": request["id"], "result": last})

    def set_last_depth(self, ec, last_depth):
        if ec:
            print "Error retrieving last depth", ec
        else:
            with self.lock:
                self.latest.set(last_depth)

    def reorganize(self, ec, fork_point, arrivals, replaced):
        pass

class LibbitcoinProcessor(stratum.Processor):

    def __init__(self):
        self.backend = Backend()
        self.numblocks_subscribe = NumblocksSubscribe(self.backend)
        stratum.Processor.__init__(self)

    def stop(self):
        self.backend.stop()

    def process(self, session):
        request = session.pop_request()
        if request["method"] == "numblocks.subscribe":
            self.numblocks_subscribe.subscribe(session, request)
        print "New request (lib)", request
        # Execute and when ready, you call
        # session.push_response(response)

if __name__ == "__main__":
    processor = LibbitcoinProcessor()
    app = stratum.Stratum()
    app.start(processor)

