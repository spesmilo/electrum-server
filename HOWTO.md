How to run your own Electrum server
===================================

Abstract
--------

This document is an easy to follow guide to installing and running your own
Electrum server on Linux. It is structured as a series of steps you need to
follow, ordered in the most logical way. The next two sections describe some
conventions we use in this document and hardware, software and expertise
requirements.

The most up-to date version of this document is available at:

    https://github.com/spesmilo/electrum-server/blob/master/HOWTO.md

Conventions
-----------

In this document, lines starting with a hash sign (#) or a dollar sign ($)
contain commands. Commands starting with a hash should be run as root,
commands starting with a dollar should be run as a normal user (in this
document, we assume that user is called 'bitcoin'). We also assume the
bitcoin user has sudo rights, so we use '$ sudo command' when we need to.

Strings that are surrounded by "lower than" and "greater than" ( < and > )
should be replaced by the user with something appropriate. For example,
<password> should be replaced by a user chosen password. Do not confuse this
notation with shell redirection ('command < file' or 'command > file')!

Lines that lack hash or dollar signs are pastes from config files. They
should be copied verbatim or adapted, without the indentation tab.

Prerequisites
-------------

**Expertise.** You should be familiar with Linux command line and
standard Linux commands. You should have basic understanding of git,
Python packages. You should have knowledge about how to install and
configure software on your Linux distribution. You should be able to
add commands to your distribution's startup scripts. If one of the
commands included in this document is not available or does not
perform the operation described here, you are expected to fix the
issue so you can continue following this howto.

**Software.** A recent Linux distribution with the following software
installed: `python`, `easy_install`, `git`, a SQL server, standard C/C++
build chain. You will need root access in order to install other software or
Python libraries. You will need access to the SQL server to create users and
databases.

**Hardware.** It's recommended to run a pruning server with leveldb.
It is a light setup with diskspace requirements permanently under 1 GB and 
less taxing on I/O and CPU once it's up and running. 
Full (archival) servers on the other hand use SQL. At the time of this writing, 
the Bitcoin blockchain is 5.5 GB large. The corresponding SQL database is 
about 4 time larger, so you should have a minimum of 22 GB free space just 
for SQL, growing continuously. 
CPU speed is also important, mostly for the initial block chain import, but 
also if you plan to run a public Electrum server, which could serve tens 
of concurrent requests. See step 6 below for some initial import benchmarks 
on SQL.

Instructions
------------

### Step 0. Create a user for running bitcoind and Electrum server

This step is optional, but for better security and resource separation I
suggest you create a separate user just for running `bitcoind` and Electrum.
We will also use the `~/bin` directory to keep locally installed files
(others might want to use `/usr/local/bin` instead). We will download source
code files to the `~/src` directory.

    # sudo adduser bitcoin
    # su - bitcoin
    $ mkdir ~/bin ~/src
    $ echo $PATH

If you don't see `/home/bitcoin/bin` in the output, you should add this line
to your `.bashrc`, `.profile` or `.bash_profile`, then logout and relogin:

    PATH="$HOME/bin:$PATH"

### Step 1. Download and install Electrum

We will download the latest git snapshot for Electrum and 'install' it in
our ~/bin directory:

    $ mkdir -p ~/src/electrum
    $ cd ~/src/electrum
    $ git clone https://github.com/spesmilo/electrum-server.git server
    $ chmod +x ~/src/electrum/server/server.py
    $ ln -s ~/src/electrum/server/server.py ~/bin/electrum

### Step 2. Donwnload Bitcoind from git & patch it

In order for the latest versions of Electrum to work properly we will need to use the latest 
build from Git and also patch it with an electrum specific patch.

   $ cd src && git clone git://github.com/bitcoin/bitcoin.git
   $ cd bitcoin 
   $ patch -p1 < ~/src/electrum/server/patch/patch
   $ cd src && make -f makefile.unix

### Step 3. Configure and start bitcoind

In order to allow Electrum to "talk" to `bitcoind`, we need to set up a RPC
username and password for `bitcoind`. We will then start `bitcoind` and
wait for it to complete downloading the blockchain.

    $ mkdir ~/.bitcoin
    $ $EDITOR ~/.bitcoin/bitcoin.conf

Write this in `bitcoin.conf`:

    rpcuser=<rpc-username>
    rpcpassword=<rpc-password>
    daemon=1

Restart `bitcoind`:

    $ bitcoind

Allow some time to pass, so `bitcoind` connects to the network and starts
downloading blocks. You can check its progress by running:

    $ bitcoind getinfo

You should also set up your system to automatically start bitcoind at boot
time, running as the 'bitcoin' user. Check your system documentation to
find out the best way to do this.


### Step 4. Select your backend - pruning leveldb or full abe server

Electrum server can currently be operated in two modes - as a pruning server
or as a full server. The pruning server uses leveldb and keeps a smaller and
faster database by pruning spent transactions. It's a lot quicker to get up
and running and requires less maintenance and diskspace than the full abe
server.

The full version uses abe as a backend. While the blockchain in bitcoind
is at roughly 5.5 GB in January 2013, the abe mysql for a full server requires
~25 GB diskspace for innodb and can take a week or two (!) to freshly index 
on most but the fastest of hardware.

Full servers are useful for recovering all past transactions when restoring 
from seed. Those are then stored in electrum.dat and won't need to be recovered
until electrum.dat is removed. Pruning servers summarize spent transactions
when restoring from seed which can be feature. Once seed recovery is done
switching between pruning and full servers can be done at any time without effect
to the transaction history stored in electrum.dat.

While it's useful for Electrum to have a number of full servers it is 
expected that the vast majority of servers available publicly will be 
pruning servers.

If you decide to setup a pruning server with leveldb take a break from this 
document, read and work through README.leveldb then come back
install jsonrcp (but not abe) from step 5 and then skip to step 8

### Step 5. Install Electrum dependencies

Electrum server depends on various standard Python libraries. These will be
already installed on your distribution, or can be installed with your
package manager. Electrum also depends on two Python libraries which we wil
l need to install "by hand": `Abe` and `JSONRPClib`.

    $ sudo easy_install jsonrpclib
    $ cd ~/src
    $ wget https://github.com/jtobey/bitcoin-abe/archive/v0.7.1.tar.gz
    $ cd bitcoin-abe
    $ sudo python setup.py install

Electrum server does not currently support abe > 0.7.1 so please stick 
with 0.7.1 for the time being. If you're version is < 0.7 you need to upgrade
to 0.7.1!

Please note that the path below might be slightly different on your system,
for example python2.6 or 2.8.

    $ sudo chmod +x /usr/local/lib/python2.7/dist-packages/Abe/abe.py
    $ ln -s /usr/local/lib/python2.7/dist-packages/Abe/abe.py ~/bin/abe


### Step 6. Configure the database

Electrum server uses a SQL database to store the blockchain data. In theory,
it supports all databases supported by Abe. At the time of this writing,
MySQL and PostgreSQL are tested and work ok, SQLite was tested and *does not
work* with Electrum server.

For MySQL:

    $ mysql -u root -p
    mysql> create user 'electrum'@'localhost' identified by '<db-password>';
    mysql> create database electrum;
    mysql> grant all on electrum.* to 'electrum'@'localhost';
    mysql> exit

For PostgreSQL:

    TBW!

### Step 7. Configure Abe and import blockchain into the database

When you run Electrum server for the first time, it will automatically
import the blockchain into the database, so it is safe to skip this step.
However, our tests showed that, at the time of this writing, importing the
blockchain via Abe is much faster (about 20-30 times faster) than
allowing Electrum to do it.

    $ cp ~/src/bitcoin-abe/abe.conf ~/abe.conf
    $ $EDITOR ~/abe.conf

For MySQL, you need these lines:

    dbtype MySQLdb
    connect-args = { "db" : "electrum", "user" : "electrum" , "passwd" : "<database-password>" }

For PostgreSQL, you need these lines:

    TBD!

Start Abe:

    $ abe --config ~/abe.conf

Abe will now start to import blocks. You will see a lot of lines like this:

    'block_tx <block-number> <tx-number>'

You should wait until you see this message on the screen:

    Listening on http://localhost:2750

It means the blockchain is imported and you can exit Abe by pressing CTRL-C.
You will not need to run Abe again after this step, Electrum server will
update the blockchain by itself. We only used Abe because it is much faster
for the initial import.

Important notice: This is a *very* long process. Even on fast machines,
expect it to take hours. Here are some benchmarks for importing
~196K blocks (size of the Bitcoin blockchain at the time of this writing):

  * System 1: ~9 hours.
	  * CPU: Intel Core i7 Q740 @ 1.73GHz
	  * HDD: very fast SSD
  * System 2: ~55 hours.
	  * CPU: Intel Xeon X3430 @ 2.40GHz
	  * HDD: 2 x SATA in a RAID1.

### Step 8. Configure Electrum server

Electrum reads a config file (/etc/electrum.conf) when starting up. This
file includes the database setup, bitcoind RPC setup, and a few other
options.

    $ sudo cp ~/src/electrum/server/electrum.conf.sample /etc/electrum.conf
    $ sudo $EDITOR /etc/electrum.conf

Go through the sample config options and set them to your liking.
If you intend to run the server publicly have a look at README-IRC.md 

### Step 9. (Finally!) Run Electrum server

The magic moment has come: you can now start your Electrum server:

    $ server

You should see this on the screen:

    starting Electrum server
    cache: yes

If you want to stop Electrum server, open another shell and run:

    $ electrum-server stop

You should also take a look at the 'start' and 'stop' scripts in
`~/src/electrum/server`. You can use them as a starting point to create a
init script for your system.

### Step 10. Test the Electrum server

We will assume you have a working Electrum client, a wallet and some
transactions history. You should start the client and click on the green
checkmark (last button on the right of the status bar) to open the Server
selection window. If your server is public, you should see it in the list
and you can select it. If you server is private, you need to enter its IP
or hostname and the port. Press Ok, the client will disconnect from the
current server and connect to your new Electrum server. You should see your
addresses and transactions history. You can see the number of blocks and
response time in the Server selection window. You should send/receive some
bitcoins to confirm that everything is working properly.

### Step 11. Join us on IRC

Say hi to the dev crew, other server operators and fans on 
irc.freenode.net #electrum and we'll try to congratulate you
on supporting the community by running an Electrum node
