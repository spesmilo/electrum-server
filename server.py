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

import ConfigParser
import logging
import socket
import sys
import time
import threading
import traceback

import json

logging.basicConfig()

if sys.maxsize <= 2**32:
    print "Warning: it looks like you are using a 32bit system. You may experience crashes caused by mmap"


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
    config.set('server', 'report_host', '')
    config.set('server', 'stratum_tcp_port', '50001')
    config.set('server', 'stratum_http_port', '8081')
    config.set('server', 'stratum_tcp_ssl_port', '')
    config.set('server', 'stratum_http_ssl_port', '')
    config.set('server', 'report_stratum_tcp_port', '')
    config.set('server', 'report_stratum_http_port', '')
    config.set('server', 'report_stratum_tcp_ssl_port', '')
    config.set('server', 'report_stratum_http_ssl_port', '')
    config.set('server', 'ssl_certfile', '')
    config.set('server', 'ssl_keyfile', '')
    config.set('server', 'password', '')
    config.set('server', 'irc', 'no')
    config.set('server', 'irc_nick', '')
    config.set('server', 'coin', '')
    config.set('server', 'datadir', '')

    # use leveldb as default
    config.set('server', 'backend', 'leveldb')
    config.add_section('leveldb')
    config.set('leveldb', 'path', '/dev/shm/electrum_db')
    config.set('leveldb', 'pruning_limit', '100')

    for path in ('/etc/', ''):
        filename = path + 'electrum.conf'
        attempt_read_config(config, filename)

    try:
        with open('/etc/electrum.banner', 'r') as f:
            config.set('server', 'banner', f.read())
    except IOError:
        pass

    return config


def run_rpc_command(command, stratum_tcp_port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, stratum_tcp_port))
    except:
        print "cannot connect to server."
        return

    method = 'server.' + command
    params = [password]
    if len(sys.argv) > 2:
        params.append(sys.argv[2])
    
    request = json.dumps({'id': 0, 'method': method, 'params': params})
    s.send(request + '\n')
    msg = ''
    while True:
        o = s.recv(1024)
        if not o: break
        msg += o
        if msg.find('\n') != -1:
            break
    s.close()
    r = json.loads(msg).get('result')

    if command == 'info':
        now = time.time()
        print 'type           address         sub  version  time'
        for item in r:
            print '%4s   %21s   %3s  %7s  %.2f' % (item.get('name'),
                                                   item.get('address'),
                                                   item.get('subscriptions'),
                                                   item.get('version'),
                                                   (now - item.get('time')),
                                                   )
    else:
        print r


if __name__ == '__main__':
    config = create_config()
    password = config.get('server', 'password')
    host = config.get('server', 'host')
    stratum_tcp_port = config.getint('server', 'stratum_tcp_port')
    stratum_http_port = config.getint('server', 'stratum_http_port')
    stratum_tcp_ssl_port = config.getint('server', 'stratum_tcp_ssl_port')
    stratum_http_ssl_port = config.getint('server', 'stratum_http_ssl_port')
    ssl_certfile = config.get('server', 'ssl_certfile')
    ssl_keyfile = config.get('server', 'ssl_keyfile')

    if stratum_tcp_ssl_port or stratum_http_ssl_port:
        assert ssl_certfile and ssl_keyfile

    if len(sys.argv) > 1:
        run_rpc_command(sys.argv[1], stratum_tcp_port)
        sys.exit(0)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, stratum_tcp_port))
        s.close()
        is_running = True
    except:
        is_running = False

    if is_running:
        print "server already running"
        sys.exit(1)


    from processor import Dispatcher, print_log
    from backends.irc import ServerProcessor

    backend_name = config.get('server', 'backend')
    if backend_name == 'libbitcoin':
        from backends.libbitcoin import BlockchainProcessor
    elif backend_name == 'leveldb':
        from backends.bitcoind import BlockchainProcessor
    else:
        print "Unknown backend '%s' specified\n" % backend_name
        sys.exit(1)

    for i in xrange(5):
        print ""
    print_log("Starting Electrum server on", host)

    # Create hub
    dispatcher = Dispatcher(config)
    shared = dispatcher.shared

    # Create and register processors
    chain_proc = BlockchainProcessor(config, shared)
    dispatcher.register('blockchain', chain_proc)

    server_proc = ServerProcessor(config)
    dispatcher.register('server', server_proc)

    transports = []
    # Create various transports we need
    if stratum_tcp_port:
        from transports.stratum_tcp import TcpServer
        tcp_server = TcpServer(dispatcher, host, stratum_tcp_port, False, None, None)
        transports.append(tcp_server)

    if stratum_tcp_ssl_port:
        from transports.stratum_tcp import TcpServer
        tcp_server = TcpServer(dispatcher, host, stratum_tcp_ssl_port, True, ssl_certfile, ssl_keyfile)
        transports.append(tcp_server)

    if stratum_http_port:
        from transports.stratum_http import HttpServer
        http_server = HttpServer(dispatcher, host, stratum_http_port, False, None, None)
        transports.append(http_server)

    if stratum_http_ssl_port:
        from transports.stratum_http import HttpServer
        http_server = HttpServer(dispatcher, host, stratum_http_ssl_port, True, ssl_certfile, ssl_keyfile)
        transports.append(http_server)

    for server in transports:
        server.start()

    while not shared.stopped():
        try:
            time.sleep(1)
        except:
            shared.stop()

    server_proc.join()
    chain_proc.join()
    print_log("Electrum Server stopped")
