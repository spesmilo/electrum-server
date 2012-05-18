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

def attempt_read_config(config, filename):
    try:
        with open(filename, 'r') as f:
            config.readfp(f)
    except IOError:
        pass

def create_config():
    config = ConfigParser.ConfigParser()
    # set some defaults, which will be overwritten by the config file
    config.add_section('server')
    config.set('server', 'banner', 'Welcome to Electrum!')
    config.set('server', 'host', 'localhost')
    config.set('server', 'native_port', '50000')
    config.set('server', 'stratum_tcp_port', '50001')
    config.set('server', 'stratum_http_port', '8081')
    config.set('server', 'password', '')
    config.set('server', 'irc', 'yes')
    config.set('server', 'irc_nick', '')
    config.add_section('database')
    config.set('database', 'type', 'psycopg2')
    config.set('database', 'database', 'abe')
    config.set('server', 'backend', 'abe')

    for path in ('/etc/', ''):
        filename = path + 'electrum.conf'
        attempt_read_config(config, filename)

    try:
        with open('/etc/electrum.banner', 'r') as f:
            config.set('server','banner', f.read())
    except IOError:
        pass

    return config

def run_rpc_command(command, stratum_tcp_port):
    import socket, json
    s = socket.socket( socket.AF_INET, socket.SOCK_STREAM )
    s.connect(( host, int(stratum_tcp_port )))
    method = 'server.' + command
    request = json.dumps( { 'id':0, 'method':method, 'params':[password] } )
    s.send(request + '\n')
    msg = s.recv(1024)
    s.close()
    r = json.loads(msg).get('result')
    if command == 'stop': print r
    elif command == 'info': 
        for item in r:
            print '%15s   %3s  %7s'%( item.get('address'), item.get('subscriptions'), item.get('version') )

if __name__ == '__main__':
    config = create_config()
    password = config.get('server', 'password')
    host = config.get('server', 'host')
    native_port = config.get('server', 'native_port')
    stratum_tcp_port = config.get('server', 'stratum_tcp_port')
    stratum_http_port = config.get('server', 'stratum_http_port')

    if len(sys.argv) > 1:
        run_rpc_command(sys.argv[1], stratum_tcp_port)
        sys.exit(0)

    from processor import Dispatcher
    from transports.stratum_http import HttpServer
    from transports.stratum_tcp import TcpServer
    from transports.native import NativeServer

    from backends.irc import ServerProcessor
    backend_name = config.get('server', 'backend')
    if backend_name == "libbitcoin":
        # NativeServer cannot be used with libbitcoin
        native_port = None
        config.set('server', 'native_port', '')
    try:
        backend = __import__("backends." + backend_name,
                             fromlist=["BlockchainProcessor"])
    except ImportError:
        sys.stderr.write("Unknown backend '%s' specified\n" % backend_name)
        raise

    # Create hub
    dispatcher = Dispatcher()
    shared = dispatcher.shared

    # Create and register processors
    chain_proc = backend.BlockchainProcessor(config)
    dispatcher.register('blockchain', chain_proc)

    server_proc = ServerProcessor(config)
    dispatcher.register('server', server_proc)

    transports = []
    # Create various transports we need
    if native_port:
        server_banner = config.get('server','banner')
        native_server = NativeServer(shared, chain_proc, server_proc,
                                     server_banner, host, int(native_port))
        transports.append(native_server)

    if stratum_tcp_port:
        tcp_server = TcpServer(dispatcher, host, int(stratum_tcp_port))
        transports.append(tcp_server)

    if stratum_http_port:
        http_server = HttpServer(dispatcher, host, int(stratum_http_port))
        transports.append(http_server)

    for server in transports:
        server.start()

    print "Starting Electrum server on", host
    while not shared.stopped():
        time.sleep(1)
    print "Server stopped"

