Electrum-server for the Electrum client
=========================================

  * Author: thomasv@gitorious
  * Language: Python

Features
--------

  * The server uses a patched version of the Bitcoin daemon that can forward
    transactions, and bitcoin-abe.
  * The server code is open source. Anyone can run a server, removing single
    points of failure concerns.
  * The server knows which set of Bitcoin addresses belong to the same wallet,
    which might raise concerns about anonymity. However, it should be possible
    to write clients capable of using several servers.

Installation
------------

  1. Patch and recompile bitcoin: see `patches/` for any necessary patches.
  2. Install [bitcoin-abe](https://github.com/jtobey/bitcoin-abe).
  3. Install [jsonrpclib](https://code.google.com/p/jsonrpclib/).
  4. Launch the server: `nohup python -u server.py > /var/log/electrum.log &`
     or use the included `start` script.

See the included `HOWTO.md` for greater detail on the installation process.

### Important Note

Do not run bitcoin-abe and electrum-server simultaneously, because they will
both try to update the database. 

If you want bitcoin-abe to be available on your website, run it with 
the `--no-update` option.

### Upgrading Abe

If you upgrade abe, you might need to update the database. In the abe directory, type:

    python -m Abe.abe --config=abe.conf --upgrade

License
-------

Electrum-server is made available under the terms of the [GNU Affero General
Public License](http://www.gnu.org/licenses/agpl.html), version 3. See the 
included `LICENSE` for more details.
