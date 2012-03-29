#!/usr/bin/env python
# Copyright(C) 2012 thomasv@gitorious

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/agpl.html>.

import time, sys, traceback
import ConfigParser

config = ConfigParser.ConfigParser()
# set some defaults, which will be overwritten by the config file
config.add_section('server')
config.set('server','banner', 'Welcome to Electrum!')
config.set('server', 'host', 'localhost')
config.set('server', 'port', '50000')
config.set('server', 'password', '')
config.set('server', 'irc', 'yes')
config.set('server', 'ircname', 'Electrum server')
config.add_section('database')
config.set('database', 'type', 'psycopg2')
config.set('database', 'database', 'abe')

try:
    f = open('/etc/electrum.conf','r')
    config.readfp(f)
    f.close()
except:
    print "Could not read electrum.conf. I will use the default values."

try:
    f = open('/etc/electrum.banner','r')
    config.set('server','banner', f.read())
    f.close()
except:
    pass

password = config.get('server','password')
host = config.get('server','host')
use_libbitcoin = False


from processor import Dispatcher
from transports.stratum_http import HttpServer
from transports.stratum_tcp import TcpServer
from transports.native import NativeServer
from irc import ServerProcessor
from abe_backend import AbeProcessor

if use_libbitcoin:
    from modules.python_bitcoin import LibBitcoinProcessor as BlockchainProcessor
else:
    from abe_backend import AbeProcessor as BlockchainProcessor

if __name__ == '__main__':

    if len(sys.argv)>1:
        import jsonrpclib
        server = jsonrpclib.Server('http://%s:8081'%host)
        cmd = sys.argv[1]
        if cmd == 'stop':
            out = server.stop(password)
        else:
            out = "Unknown command: '%s'" % cmd
        print out
        sys.exit(0)

    # Create hub
    dispatcher = Dispatcher()
    shared = dispatcher.shared

    # Create and register processors
    abe = BlockchainProcessor(config)
    dispatcher.register('blockchain', abe)

    sb = ServerProcessor(config)
    dispatcher.register('server', sb)

    # Create various transports we need
    transports = [ NativeServer(shared, abe, sb, config.get('server','banner'), host, 50000),
                   TcpServer(dispatcher, host, 50001),
                   HttpServer(dispatcher, host, 8081),
                   ]
    for server in transports:
        server.start()

    print "starting Electrum server on", host
    while not shared.stopped():
        time.sleep(1)
    print "server stopped"
