import bitcoin
import history1 as history
import membuf


def blockchain_started(ec, chain):
    print "Blockchain initialisation:", ec


def finish(ec, result):
    print "Finish:", ec
    for line in result:
        for k, v in line.iteritems():
            begin = k + ":"
            print begin, " " * (12 - len(begin)), v
        print


a = bitcoin.async_service(1)
chain = bitcoin.bdb_blockchain(a, "/home/genjix/libbitcoin/database",
                               blockchain_started)
txpool = bitcoin.transaction_pool(a, chain)
txdat = bitcoin.data_chunk("0100000001d6cad920a04acd6c0609cd91fe4dafa1f3b933ac90e032c78fdc19d98785f2bb010000008b483045022043f8ce02784bd7231cb362a602920f2566c18e1877320bf17d4eabdac1019b2f022100f1fd06c57330683dff50e1b4571fb0cdab9592f36e3d7e98d8ce3f94ce3f255b01410453aa8d5ddef56731177915b7b902336109326f883be759ec9da9c8f1212c6fa3387629d06e5bf5e6bcc62ec5a70d650c3b1266bb0bcc65ca900cff5311cb958bffffffff0280969800000000001976a9146025cabdbf823949f85595f3d1c54c54cd67058b88ac602d2d1d000000001976a914c55c43631ab14f7c4fd9c5f153f6b9123ec32c8888ac00000000")
ex = bitcoin.satoshi_exporter()
tx = ex.load_transaction(txdat)


def stored(ec):
    print "mbuff", ec

mbuff = membuf.memory_buffer(a.internal_ptr, chain.internal_ptr,
                             txpool.internal_ptr)
mbuff.receive(tx, stored)
address = "1AA6mgxqSrvJTxRrYrikSnLaAGupVzvx4f"
raw_input()
history.payment_history(a, chain, txpool, mbuff, address, finish)
raw_input()
