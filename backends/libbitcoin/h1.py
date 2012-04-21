import bitcoin
import history1 as history

def blockchain_started(ec, chain):
    print "Blockchain initialisation:", ec

def finish(ec, result):
    print "Finish:", ec
    print result

a = bitcoin.async_service(1)
chain = bitcoin.bdb_blockchain(a, "/home/genjix/libbitcoin/database",
                               blockchain_started)
txpool = bitcoin.transaction_pool(a, chain)
address = "1Jqu2PVGDvNv4La113hgCJsvRUCDb3W65D"
history.payment_history(a, chain, txpool, address, finish)
raw_input()

