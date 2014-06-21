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

apt-get install commands are suggestions for required dependencies.
They conform to an Ubuntu 13.10 system but may well work with Debian
or earlier and later versions of Ubuntu.

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

**Software.** A recent Linux 64-bit distribution with the following software
installed: `python`, `easy_install`, `git`, standard C/C++
build chain. You will need root access in order to install other software or
Python libraries. 

**Hardware.** The lightest setup is a pruning server with diskspace 
requirements of about 10 GB for the electrum database. However note that 
you also need to run bitcoind and keep a copy of the full blockchain, 
which is roughly 20 GB in April 2014. If you have less than 2 GB of RAM 
make sure you limit bitcoind to 8 concurrent connections. If you have more 
ressources to  spare you can run the server with a higher limit of historic 
transactions per address. CPU speed is important, mostly for the initial block 
chain import, but also if you plan to run a public Electrum server, which 
could serve tens of concurrent requests. Any multi-core x86 CPU ~2009 or
newer other than Atom should do for good performance. An ideal setup
has enough RAM to hold and procss the leveldb database in tmpfs (e.g. /dev/shm).

Instructions
------------

### Step 1. Create a user for running bitcoind and Electrum server

This step is optional, but for better security and resource separation I
suggest you create a separate user just for running `bitcoind` and Electrum.
We will also use the `~/bin` directory to keep locally installed files
(others might want to use `/usr/local/bin` instead). We will download source
code files to the `~/src` directory.

    $ sudo adduser bitcoin --disabled-password
    $ sudo apt-get install git
    # su - bitcoin
    $ mkdir ~/bin ~/src
    $ echo $PATH

If you don't see `/home/bitcoin/bin` in the output, you should add this line
to your `.bashrc`, `.profile` or `.bash_profile`, then logout and relogin:

    PATH="$HOME/bin:$PATH"

### Step 2. Download and install Electrum

We will download the latest git snapshot for Electrum and 'install' it in
our ~/bin directory:

    $ mkdir -p ~/electrum-server
    $ git clone https://github.com/spesmilo/electrum-server.git

### Step 3. Download bitcoind

Older versions of Electrum used to require a patched version of bitcoind. 
This is not the case anymore since bitcoind supports the 'txindex' option.
We currently recommend bitcoind 0.9.2 stable.

If your package manager does not supply a recent bitcoind and prefer to compile
here are some pointers for Ubuntu:

    # apt-get install make g++ python-leveldb libboost-all-dev libssl-dev libdb++-dev pkg-config
    # su - bitcoin
    $ cd ~/src && wget https://bitcoin.org/bin/0.9.2/bitcoin-0.9.2-linux.tar.gz
    $ sha256sum bitcoin-0.9.2-linux.tar.gz | grep 58a77aeb4c81b54d3903d85abce4f0fb580694a3611a415c5fe69a27dea5935b
    $ tar xfz bitcoin-0.9.2-linux.tar.gz
    $ cd bitcoin-0.9.2-linux/src
    $ tar xfz bitcoin-0.9.2.tar.gz
    $ cd bitcoin-0.9.2
    $ ./configure --disable-wallet --without-miniupnpc
    $ make
    $ strip ~/src/bitcoin-0.9.2-linux/src/bitcoin-0.9.2/src/bitcoind
    $ cp -a ~/src/bitcoin-0.9.2-linux/src/bitcoin-0.9.2/src/bitcoind ~/bin/bitcoind

### Step 4. Configure and start bitcoind

In order to allow Electrum to "talk" to `bitcoind`, we need to set up a RPC
username and password for `bitcoind`. We will then start `bitcoind` and
wait for it to complete downloading the blockchain.

    $ mkdir ~/.bitcoin
    $ $EDITOR ~/.bitcoin/bitcoin.conf

Write this in `bitcoin.conf`:

    rpcuser=<rpc-username>
    rpcpassword=<rpc-password>
    daemon=1
    txindex=1


If you have an existing installation of bitcoind and have not previously
set txindex=1 you need to reindex the blockchain by running

    $ bitcoind -reindex

If you have a fresh copy of bitcoind start `bitcoind`:

    $ bitcoind

Allow some time to pass, so `bitcoind` connects to the network and starts
downloading blocks. You can check its progress by running:

    $ bitcoind getinfo

Before starting electrum server your bitcoind should have processed all 
blockes and caught up to the current height of the network.
You should also set up your system to automatically start bitcoind at boot
time, running as the 'bitcoin' user. Check your system documentation to
find out the best way to do this.

### Step 5. Install Electrum dependencies

Electrum server depends on various standard Python libraries. These will be
already installed on your distribution, or can be installed with your
package manager. Electrum also depends on two Python libraries which we will
need to install "by hand": `JSONRPClib`.

    $ sudo apt-get install python-setuptools python-openssl
    $ sudo easy_install jsonrpclib

### Step 6. Install leveldb and plyvel

    $ sudo apt-get install python-leveldb libleveldb-dev
    $ sudo easy_install plyvel
 
See the steps in README.leveldb for further details, especially if your system
doesn't have the python-leveldb package or if plyvel installation fails.

leveldb should be at least version 1.1.9. Earlier version are believed to be buggy.

### Step 7. Select your limit

Electrum server uses leveldb to store transactions. You can choose
how many spent transactions per address you want to store on the server.
The default is 100, but there are also servers with 1000 or even 10000.
Few addresses have more than 10000 transactions. A limit this high
can be considered to be equivalent to a "full" server. Full servers previously
used abe to store the blockchain. The use of abe for electrum servers is now
deprecated.

The pruning server uses leveldb and keeps a smaller and
faster database by pruning spent transactions. It's a lot quicker to get up
and running and requires less maintenance and diskspace than abe.

The section in the electrum server configuration file (see step 10) looks like this:

     [leveldb]
     path_fulltree = /path/to/your/database
     # for each address, history will be pruned if it is longer than this limit
     pruning_limit = 100

### Step 8. Import blockchain into the database or download it

It's recommended to fetch a pre-processed leveldb from the net

You can fetch recent copies of electrum leveldb databases and further instructions 
from the Electrum full archival server foundry at:
http://foundry.electrum.org/ 

Alternatively if you have the time and nerve you can import the blockchain yourself.

As of April 2014 it takes between two days and over a week to import 300k of blocks, depending
on CPU speed, I/O speed and selected pruning limit.

It's considerably faster and strongly recommended to index in memory. You can use /dev/shm or
or create a tmpfs which will also use swap if you run out of memory:

    $ sudo mount -t tmpfs -o rw,nodev,nosuid,noatime,size=15000M,mode=0777 none /tmpfs

If you use tmpfs make sure you have enough RAM and swap to cover the size. If you only have 4 gigs of
RAM but add 15 gigs of swap from a file that's fine too. tmpfs is rather smart to swap out the least
used parts. It's fine to use a file on a SSD for swap in thise case. 

It's not recommended to do initial indexing of the database on a SSD because the indexing process
does at least 20 TB (!) of disk writes and puts considerable wear-and-tear on a SSD. It's a lot better
to use tmpfs and just swap out to disk when necessary.   

Databases have grown to roughly 8 GB in April 2014, give or take a gigabyte between pruning limits 
100 and 10000. Leveldb prunes the database from time to time, so it's not uncommon to see databases
~50% larger at times when it's writing a lot especially when indexing from the beginning.


### Step 9. Create a self-signed SSL cert

To run SSL / HTTPS you need to generate a self-signed certificate
using openssl. You could just comment out the SSL / HTTPS ports in the config and run 
without, but this is not recommended.

Use the sample code below to create a self-signed cert with a recommended validity 
of 5 years. You may supply any information for your sign request to identify your server.
They are not currently checked by the client except for the validity date.
When asked for a challenge password just leave it empty and press enter.

    $ openssl genrsa -des3 -passout pass:x -out server.pass.key 2048
    $ openssl rsa -passin pass:x -in server.pass.key -out server.key
    writing RSA key
    $ rm server.pass.key
    $ openssl req -new -key server.key -out server.csr
    ...
    Country Name (2 letter code) [AU]:US
    State or Province Name (full name) [Some-State]:California
    Common Name (eg, YOUR name) []: electrum-server.tld
    ...
    A challenge password []:
    ...

    $ openssl x509 -req -days 730 -in server.csr -signkey server.key -out server.crt

The server.crt file is your certificate suitable for the ssl_certfile= parameter and
server.key corresponds to ssl_keyfile= in your electrum server config

Starting with Electrum 1.9 the client will learn and locally cache the SSL certificate 
for your server upon the first request to prevent man-in-the middle attacks for all
further connections.

If your certificate is lost or expires on the server side you currently need to run
your server with a different server name along with a new certificate for this server.
Therefore it's a good idea to make an offline backup copy of your certificate and key
in case you need to restore it.

### Step 10. Configure Electrum server

Electrum reads a config file (/etc/electrum.conf) when starting up. This
file includes the database setup, bitcoind RPC setup, and a few other
options.

    $ sudo cp ~/src/electrum/server/electrum.conf.sample /etc/electrum.conf
    $ sudo $EDITOR /etc/electrum.conf

Go through the sample config options and set them to your liking.
If you intend to run the server publicly have a look at README-IRC.md 

### Step 11. Tweak your system for running electrum

Electrum server currently needs quite a few file handles to use leveldb. It also requires
file handles for each connection made to the server. It's good practice to increase the
open files limit to 16k. This is most easily achived by sticking the value in .bashrc of the
root user who usually passes this value to all unprivileged user sessions too.

    $ sudo sed -i '$a ulimit -n 16384' /root/.bashrc

Also make sure the bitcoin user can actually increase the ulimit by allowing it accordingly in
/etc/security/limits.conf

While most bugs are fixed in this regard electrum server may leak some memory and it's good practice to
to restart the server once in a while from cron (preferred) or to at least monitor 
it for crashes and then restart the server. Monthly restarts should be fine for most setups.

Two more things for you to consider:

1. To increase security you may want to close bitcoind for incoming connections and connect outbound only

2. Consider restarting bitcoind (together with electrum-server) on a weekly basis to clear out unconfirmed
   transactions from the local the memory pool which did not propagate over the network

### Step 12. (Finally!) Run Electrum server

The magic moment has come: you can now start your Electrum server:

    $ cd ~/electrum-server
    $ ./start

You should see this in the log file:

    starting Electrum server

If you want to stop Electrum server, use the 'stop' script:

    $ cd ~/electrum-server
    $ ./stop


### Step 13. Test the Electrum server

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

### Step 14. Join us on IRC, subscribe to the server thread

Say hi to the dev crew, other server operators and fans on 
irc.freenode.net #electrum and we'll try to congratulate you
on supporting the community by running an Electrum node

If you're operating a public Electrum server please subscribe
to or regulary check the following thread:
https://bitcointalk.org/index.php?topic=85475.0
It'll contain announcements about important updates to Electrum
server required for a smooth user experience.
