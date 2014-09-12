import plyvel
import ast
import hashlib
import os
import sys

from processor import print_log, logger
from utils import bc_address_to_hash_160, hash_160_to_pubkey_address, hex_to_int, int_to_hex, Hash

global GENESIS_HASH
GENESIS_HASH = '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f'

"""
Patricia tree for hashing unspents

"""

DEBUG = 0
KEYLENGTH = 20 + 32 + 4   #56

class Storage(object):

    def __init__(self, config, shared, test_reorgs):

        self.dbpath = config.get('leveldb', 'path')
        if not os.path.exists(self.dbpath):
            os.mkdir(self.dbpath)
        self.pruning_limit = config.getint('leveldb', 'pruning_limit')
        self.shared = shared
        self.hash_list = {}
        self.parents = {}

        self.test_reorgs = test_reorgs
        try:
            self.db_utxo = plyvel.DB(os.path.join(self.dbpath,'utxo'), create_if_missing=True, compression=None)
            self.db_addr = plyvel.DB(os.path.join(self.dbpath,'addr'), create_if_missing=True, compression=None)
            self.db_hist = plyvel.DB(os.path.join(self.dbpath,'hist'), create_if_missing=True, compression=None)
            self.db_undo = plyvel.DB(os.path.join(self.dbpath,'undo'), create_if_missing=True, compression=None)
        except:
            logger.error('db init', exc_info=True)
            self.shared.stop()

        self.db_version = 3 # increase this when database needs to be updated
        try:
            self.last_hash, self.height, db_version = ast.literal_eval(self.db_undo.get('height'))
            print_log("Database version", self.db_version)
            print_log("Blockchain height", self.height)
        except:
            print_log('initializing database')
            self.height = 0
            self.last_hash = GENESIS_HASH
            db_version = self.db_version
            # write root
            self.put_node('', {})

        # check version
        if self.db_version != db_version:
            print_log("Your database '%s' is deprecated. Please create a new database"%self.dbpath)
            self.shared.stop()
            return


        # compute root hash
        d = self.get_node('')
        self.root_hash, v = self.get_node_hash('',d,None)
        print_log("UTXO tree root hash:", self.root_hash.encode('hex'))
        print_log("Coins in database:", v)

    # convert between bitcoin addresses and 20 bytes keys used for storage. 
    def address_to_key(self, addr):
        return bc_address_to_hash_160(addr)

    def key_to_address(self, addr):
        return hash_160_to_pubkey_address(addr)


    def get_proof(self, addr):
        key = self.address_to_key(addr)
        i = self.db_utxo.iterator(start=key)
        k, _ = i.next()

        p = self.get_path(k) 
        p.append(k)

        out = []
        for item in p:
            v = self.db_utxo.get(item)
            out.append((item.encode('hex'), v.encode('hex')))

        return out


    def get_balance(self, addr):
        key = self.address_to_key(addr)
        i = self.db_utxo.iterator(start=key)
        k, _ = i.next()
        if not k.startswith(key): 
            return 0
        p = self.get_parent(k)
        d = self.get_node(p)
        letter = k[len(p)]
        return d[letter][1]


    def listunspent(self, addr):
        key = self.address_to_key(addr)
        if key is None:
            raise BaseException('Invalid Bitcoin address', addr)

        out = []
        for k, v in self.db_utxo.iterator(start=key):
            if not k.startswith(key):
                break
            if len(k) == KEYLENGTH:
                txid = k[20:52].encode('hex')
                txpos = hex_to_int(k[52:56])
                h = hex_to_int(v[8:12])
                v = hex_to_int(v[0:8])
                out.append({'tx_hash': txid, 'tx_pos':txpos, 'height': h, 'value':v})

        out.sort(key=lambda x:x['height'])
        return out


    def get_history(self, addr):
        out = []

        o = self.listunspent(addr)
        for item in o:
            out.append((item['tx_hash'], item['height']))

        h = self.db_hist.get(addr)
        
        while h:
            item = h[0:80]
            h = h[80:]
            txi = item[0:32].encode('hex')
            hi = hex_to_int(item[36:40])
            txo = item[40:72].encode('hex')
            ho = hex_to_int(item[76:80])
            out.append((txi, hi))
            out.append((txo, ho))

        # sort
        out.sort(key=lambda x:x[1])

        # uniqueness
        out = set(out)

        return map(lambda x: {'tx_hash':x[0], 'height':x[1]}, out)



    def get_address(self, txi):
        return self.db_addr.get(txi)


    def get_undo_info(self, height):
        s = self.db_undo.get("undo_info_%d" % (height % 100))
        if s is None: print_log("no undo info for ", height)
        return eval(s)


    def write_undo_info(self, height, bitcoind_height, undo_info):
        if height > bitcoind_height - 100 or self.test_reorgs:
            self.db_undo.put("undo_info_%d" % (height % 100), repr(undo_info))


    def common_prefix(self, word1, word2):
        max_len = min(len(word1),len(word2))
        for i in range(max_len):
            if word2[i] != word1[i]:
                index = i
                break
        else:
            index = max_len
        return word1[0:index]


    def put_node(self, key, d, batch=None):
        k = 0
        serialized = ''
        for i in range(256):
            if chr(i) in d.keys():
                k += 1<<i
                h, v = d[chr(i)]
                if h is None: h = chr(0)*32
                vv = int_to_hex(v, 8).decode('hex')
                item = h + vv
                assert len(item) == 40
                serialized += item

        k = "0x%0.64X" % k # 32 bytes
        k = k[2:].decode('hex')
        assert len(k) == 32
        out = k + serialized
        if batch:
            batch.put(key, out)
        else:
            self.db_utxo.put(key, out) 


    def get_node(self, key):

        s = self.db_utxo.get(key)
        if s is None: 
            return 

        #print "get node", key.encode('hex'), len(key), s.encode('hex')

        k = int(s[0:32].encode('hex'), 16)
        s = s[32:]
        d = {}
        for i in range(256):
            if k % 2 == 1: 
                _hash = s[0:32]
                value = hex_to_int(s[32:40])
                d[chr(i)] = (_hash, value)
                s = s[40:]
            k = k/2

        #cache
        return d


    def add_address(self, target, value, height):
        assert len(target) == KEYLENGTH

        word = target
        key = ''
        path = [ '' ]
        i = self.db_utxo.iterator()

        while key != target:

            items = self.get_node(key)

            if word[0] in items.keys():
  
                i.seek(key + word[0])
                new_key, _ = i.next()

                if target.startswith(new_key):
                    # add value to the child node
                    key = new_key
                    word = target[len(key):]
                    if key == target:
                        break
                    else:
                        assert key not in path
                        path.append(key)
                else:
                    # prune current node and add new node
                    prefix = self.common_prefix(new_key, target)
                    index = len(prefix)

                    ## get hash and value of new_key from parent (if it's a leaf)
                    if len(new_key) == KEYLENGTH:
                        parent_key = self.get_parent(new_key)
                        parent = self.get_node(parent_key)
                        z = parent[ new_key[len(parent_key)] ]
                        self.put_node(prefix, { target[index]:(None,0), new_key[index]:z } )
                    else:
                        # if it is not a leaf, update the hash of new_key because skip_string changed
                        h, v = self.get_node_hash(new_key, self.get_node(new_key), prefix)
                        self.put_node(prefix, { target[index]:(None,0), new_key[index]:(h,v) } )

                    path.append(prefix)
                    self.parents[new_key] = prefix
                    break

            else:
                assert key in path
                items[ word[0] ] = (None,0)
                self.put_node(key,items)
                break

        # write 
        s = (int_to_hex(value, 8) + int_to_hex(height,4)).decode('hex')
        self.db_utxo.put(target, s)
        # the hash of a node is the txid
        _hash = target[20:52]
        self.update_node_hash(target, path, _hash, value)


    def update_node_hash(self, node, path, _hash, value):
        c = node
        for x in path[::-1]:
            self.parents[c] = x
            c = x

        self.hash_list[node] = (_hash, value)


    def update_hashes(self):

        nodes = {} # nodes to write

        for i in range(KEYLENGTH, -1, -1):

            for node in self.hash_list.keys():
                if len(node) != i: continue

                node_hash, node_value = self.hash_list.pop(node)

                # for each node, compute its hash, send it to the parent
                if node == '':
                    self.root_hash = node_hash
                    self.root_value = node_value
                    break

                parent = self.parents[node]

                # read parent.. do this in add_address
                d = nodes.get(parent)
                if d is None:
                    d = self.get_node(parent)
                    assert d is not None

                letter = node[len(parent)]
                assert letter in d.keys()

                if i != KEYLENGTH and node_hash is None:
                    d2 = self.get_node(node)
                    node_hash, node_value = self.get_node_hash(node, d2, parent)

                assert node_hash is not None
                # write new value
                d[letter] = (node_hash, node_value)
                nodes[parent] = d

                # iterate
                grandparent = self.parents[parent] if parent != '' else None
                parent_hash, parent_value = self.get_node_hash(parent, d, grandparent)
                self.hash_list[parent] = (parent_hash, parent_value)

        
        # batch write modified nodes 
        batch = self.db_utxo.write_batch()
        for k, v in nodes.items():
            self.put_node(k, v, batch)
        batch.write()

        # cleanup
        assert self.hash_list == {}
        self.parents = {}


    def get_node_hash(self, x, d, parent):

        # final hash
        if x != '':
            skip_string = x[len(parent)+1:]
        else:
            skip_string = ''

        d2 = sorted(d.items())
        values = map(lambda x: x[1][1], d2)
        hashes = map(lambda x: x[1][0], d2)
        value = sum( values )
        _hash = self.hash( skip_string + ''.join(hashes) )
        return _hash, value


    def get_path(self, target):
        word = target
        key = ''
        path = [ '' ]
        i = self.db_utxo.iterator(start='')

        while key != target:

            i.seek(key + word[0])
            try:
                new_key, _ = i.next()
                is_child = new_key.startswith(key + word[0])
            except StopIteration:
                is_child = False

            if is_child:
  
                if target.startswith(new_key):
                    # add value to the child node
                    key = new_key
                    word = target[len(key):]
                    if key == target:
                        break
                    else:
                        assert key not in path
                        path.append(key)
                else:
                    print_log('not in tree', self.db_utxo.get(key+word[0]), new_key.encode('hex'))
                    return False
            else:
                assert key in path
                break

        return path


    def delete_address(self, leaf):
        path = self.get_path(leaf)
        if path is False:
            print_log("addr not in tree", leaf.encode('hex'), self.key_to_address(leaf[0:20]), self.db_utxo.get(leaf))
            raise

        s = self.db_utxo.get(leaf)
        
        self.db_utxo.delete(leaf)
        if leaf in self.hash_list:
            self.hash_list.pop(leaf)

        parent = path[-1]
        letter = leaf[len(parent)]
        items = self.get_node(parent)
        items.pop(letter)

        # remove key if it has a single child
        if len(items) == 1:
            letter, v = items.items()[0]

            self.db_utxo.delete(parent)
            if parent in self.hash_list: 
                self.hash_list.pop(parent)

            # we need the exact length for the iteration
            i = self.db_utxo.iterator()
            i.seek(parent+letter)
            k, v = i.next()

            # note: k is not necessarily a leaf
            if len(k) == KEYLENGTH:
                 _hash, value = k[20:52], hex_to_int(v[0:8])
            else:
                _hash, value = None, None

            self.update_node_hash(k, path[:-1], _hash, value)

        else:
            self.put_node(parent, items)
            _hash, value = None, None
            self.update_node_hash(parent, path[:-1], _hash, value)

        return s


    def get_children(self, x):
        i = self.db_utxo.iterator()
        l = 0
        while l <256:
            i.seek(x+chr(l))
            k, v = i.next()
            if k.startswith(x+chr(l)): 
                yield k, v
                l += 1
            elif k.startswith(x): 
                yield k, v
                l = ord(k[len(x)]) + 1
            else: 
                break




    def get_parent(self, x):
        """ return parent and skip string"""
        i = self.db_utxo.iterator()
        for j in range(len(x)):
            p = x[0:-j-1]
            i.seek(p)
            k, v = i.next()
            if x.startswith(k) and x!=k: 
                break
        else: raise
        return k

        
    def hash(self, x):
        if DEBUG: return "hash("+x+")"
        return Hash(x)


    def get_root_hash(self):
        return self.root_hash


    def close(self):
        self.db_utxo.close()
        self.db_addr.close()
        self.db_hist.close()
        self.db_undo.close()


    def add_to_history(self, addr, tx_hash, tx_pos, value, tx_height):
        key = self.address_to_key(addr)
        txo = (tx_hash + int_to_hex(tx_pos, 4)).decode('hex')

        # write the new history
        self.add_address(key + txo, value, tx_height)

        # backlink
        self.db_addr.put(txo, addr)



    def revert_add_to_history(self, addr, tx_hash, tx_pos, value, tx_height):
        key = self.address_to_key(addr)
        txo = (tx_hash + int_to_hex(tx_pos, 4)).decode('hex')

        # delete
        self.delete_address(key + txo)

        # backlink
        self.db_addr.delete(txo)


    def get_utxo_value(self, addr, txi):
        key = self.address_to_key(addr)
        leaf = key + txi
        s = self.db_utxo.get(leaf)
        value = hex_to_int(s[0:8])
        return value


    def set_spent(self, addr, txi, txid, index, height, undo):
        key = self.address_to_key(addr)
        leaf = key + txi

        s = self.delete_address(leaf)
        value = hex_to_int(s[0:8])
        in_height = hex_to_int(s[8:12])
        undo[leaf] = value, in_height

        # delete backlink txi-> addr
        self.db_addr.delete(txi)

        # add to history
        s = self.db_hist.get(addr)
        if s is None: s = ''
        txo = (txid + int_to_hex(index,4) + int_to_hex(height,4)).decode('hex')
        s += txi + int_to_hex(in_height,4).decode('hex') + txo
        s = s[ -80*self.pruning_limit:]
        self.db_hist.put(addr, s)



    def revert_set_spent(self, addr, txi, undo):
        key = self.address_to_key(addr)
        leaf = key + txi

        # restore backlink
        self.db_addr.put(txi, addr)

        v, height = undo.pop(leaf)
        self.add_address(leaf, v, height)

        # revert add to history
        s = self.db_hist.get(addr)
        # s might be empty if pruning limit was reached
        if not s:
            return

        assert s[-80:-44] == txi
        s = s[:-80]
        self.db_hist.put(addr, s)




        

    def import_transaction(self, txid, tx, block_height, touched_addr):

        undo = { 'prev_addr':[] } # contains the list of pruned items for each address in the tx; also, 'prev_addr' is a list of prev addresses
                
        prev_addr = []
        for i, x in enumerate(tx.get('inputs')):
            txi = (x.get('prevout_hash') + int_to_hex(x.get('prevout_n'), 4)).decode('hex')
            addr = self.get_address(txi)
            if addr is not None: 
                self.set_spent(addr, txi, txid, i, block_height, undo)
                touched_addr.add(addr)
            prev_addr.append(addr)

        undo['prev_addr'] = prev_addr 

        # here I add only the outputs to history; maybe I want to add inputs too (that's in the other loop)
        for x in tx.get('outputs'):
            addr = x.get('address')
            if addr is None: continue
            self.add_to_history(addr, txid, x.get('index'), x.get('value'), block_height)
            touched_addr.add(addr)

        return undo


    def revert_transaction(self, txid, tx, block_height, touched_addr, undo):
        #print_log("revert tx", txid)
        for x in reversed(tx.get('outputs')):
            addr = x.get('address')
            if addr is None: continue
            self.revert_add_to_history(addr, txid, x.get('index'), x.get('value'), block_height)
            touched_addr.add(addr)

        prev_addr = undo.pop('prev_addr')
        for i, x in reversed(list(enumerate(tx.get('inputs')))):
            addr = prev_addr[i]
            if addr is not None:
                txi = (x.get('prevout_hash') + int_to_hex(x.get('prevout_n'), 4)).decode('hex')
                self.revert_set_spent(addr, txi, undo)
                touched_addr.add(addr)

        assert undo == {}

