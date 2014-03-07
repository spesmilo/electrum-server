Electrum-server for the Electrum client
=========================================

  * Author: Thomas Voegtlin (ThomasV on the bitcointalk forum)
  * Language: Python

Features
--------

  * The server indexes UTXOs by address, in a Patricia tree structure
    described by Alan Reiner (see the 'ultimate blockchain
    compression' thread in the Bitcointalk forum)

  * Te server requires bitcoind, leveldb and plyvel

  * The server code is open source. Anyone can run a server, removing
    single points of failure concerns.

  * The server knows which set of Bitcoin addresses belong to the same
    wallet, which might raise concerns about anonymity. However, it
    should be possible to write clients capable of using several
    servers.

Installation
------------

  1. To install and run a server, see README.leveldb. For greater
     detail on the installation process, see HOWTO.md.

  2. To start the server, use the 'start' script. If you do not have a
     database, it will propose you o download it from the Electrum
     foundry.

  3. To stop the server, use the 'stop' script. It will shutdown the
     database cleanly.



License
-------

Electrum-server is made available under the terms of the [GNU Affero General
Public License](http://www.gnu.org/licenses/agpl.html), version 3. See the 
included `LICENSE` for more details.
