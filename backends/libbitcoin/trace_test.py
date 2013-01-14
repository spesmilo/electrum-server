import bitcoin

import trace_tx


def blockchain_started(ec, chain):
    print "Blockchain initialisation:", ec


def handle_tx(ec, tx):
    if ec:
        print ec
    trace_tx.trace_tx(service.internal_ptr, chain.internal_ptr, tx, finish)


def finish(ec, result):
    print ec
    print result


if __name__ == '__main__':
    service = bitcoin.async_service(1)
    chain = bitcoin.bdb_blockchain(service, "/home/genjix/libbitcoin/database",
                                   blockchain_started)
    chain.fetch_transaction(
        bitcoin.hash_digest("16e3e3bfbaa072e33e6a9be1df7a13ecde5ad46a8d4d4893dbecaf0c0aeeb842"),
        handle_tx
    )

    raw_input()
