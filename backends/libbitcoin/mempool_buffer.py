import bitcoin
from bitcoin import _1, _2
import multimap

class MemoryPoolBuffer:

    def __init__(self, service, txpool):
        self.wrap = bitcoin.Strand(service).wrap
        self.txpool = txpool
        # prevout: inpoint
        self.lookup_input = {}
        # payment_address: outpoint
        self.lookup_address = multimap.MultiMap()

    def recv_tx(self, tx):
        tx_hash = bitcoin.hash_transaction(tx)
        desc = (tx_hash, [], [])
        for input in tx.inputs:
            desc[1].append(input.previous_output)
        for idx, output in enumerate(tx.outputs):
            address = bitcoin.payment_address()
            if address.extract(output.output_script):
                desc[2].append((idx, address))
        self.txpool.store(tx,
            self.wrap(self.confirmed, _1, desc),
            self.wrap(self.mempool_stored, _1, desc))

    def mempool_stored(self, ec, desc):
        tx_hash, prevouts, addrs = desc
        if ec:
            print "Error storing memory pool transaction", tx_hash, ec
            return
        print "Accepted transaction", tx_hash
        for idx, prevout in enumerate(prevouts):
            inpoint = bitcoin.input_point()
            inpoint.hash, inpoint.index = tx_hash, idx
            self.lookup_input[prevout] = inpoint
        for idx, address in addrs:
            outpoint = bitcoin.output_point()
            outpoint.hash, outpoint.index = tx_hash, idx
            self.lookup_address[str(address)] = outpoint

    def confirmed(self, ec, desc):
        tx_hash, prevouts, addrs = desc
        if ec:
            print "Problem confirming transaction", tx_hash, ec
            return
        print "Confirmed", tx_hash
        for idx, prevout in enumerate(prevouts):
            inpoint = bitcoin.input_point()
            inpoint.hash, inpoint.index = tx_hash, idx
            assert self.lookup_input[prevout] == inpoint
            del self.lookup_input[prevout]
        for idx, address in addrs:
            outpoint = bitcoin.output_point()
            outpoint.hash, outpoint.index = tx_hash, idx
            self.lookup_address.delete(str(address), outpoint)

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

    service = bitcoin.async_service(1)
    prefix = "/home/genjix/libbitcoin/database"
    chain = bitcoin.bdb_blockchain(service, prefix, blockchain_started)
    txpool = bitcoin.transaction_pool(service, chain)
    local_service = bitcoin.AsyncService()
    membuf = MemoryPoolBuffer(local_service, txpool)
    membuf.recv_tx(tx_a)
    membuf.recv_tx(tx_b)
    print "Started."
    raw_input()
    print "Stopping..."

