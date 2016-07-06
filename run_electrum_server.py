#!/usr/bin/env python
# Copyright(C) 2011-2016 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import argparse
import ConfigParser
import logging
import socket
import sys
import time
import threading
import json
import os
import imp


if os.path.dirname(os.path.realpath(__file__)) == os.getcwd():
    imp.load_module('electrumserver', *imp.find_module('src'))

from electrumserver import storage, networks, utils
from electrumserver.processor import Dispatcher, print_log
from electrumserver.server_processor import ServerProcessor
from electrumserver.blockchain_processor import BlockchainProcessor
from electrumserver.stratum_tcp import TcpServer


logging.basicConfig()

if sys.maxsize <= 2**32:
    print "Warning: it looks like you are using a 32bit system. You may experience crashes caused by mmap"

if os.getuid() == 0:
    print "Do not run this program as root!"
    print "Run the install script to create a non-privileged user."
    sys.exit()

def attempt_read_config(config, filename):
    try:
        with open(filename, 'r') as f:
            config.readfp(f)
    except IOError:
        pass

def load_banner(config):
    try:
        with open(config.get('server', 'banner_file'), 'r') as f:
            config.set('server', 'banner', f.read())
    except IOError:
        pass

def setup_network_params(config):
    type = config.get('network', 'type')
    params = networks.params.get(type)
    utils.PUBKEY_ADDRESS = int(params.get('pubkey_address'))
    utils.SCRIPT_ADDRESS = int(params.get('script_address'))
    storage.GENESIS_HASH = params.get('genesis_hash')

    if config.has_option('network', 'pubkey_address'):
        utils.PUBKEY_ADDRESS = config.getint('network', 'pubkey_address')
    if config.has_option('network', 'script_address'):
        utils.SCRIPT_ADDRESS = config.getint('network', 'script_address')
    if config.has_option('network', 'genesis_hash'):
        storage.GENESIS_HASH = config.get('network', 'genesis_hash')

def create_config(filename=None):
    config = ConfigParser.ConfigParser()
    # set some defaults, which will be overwritten by the config file
    config.add_section('server')
    config.set('server', 'banner', 'Welcome to Electrum!')
    config.set('server', 'banner_file', '/etc/electrum.banner')
    config.set('server', 'host', 'localhost')
    config.set('server', 'electrum_rpc_port', '8000')
    config.set('server', 'report_host', '')
    config.set('server', 'stratum_tcp_port', '50001')
    config.set('server', 'stratum_tcp_ssl_port', '50002')
    config.set('server', 'report_stratum_tcp_port', '')
    config.set('server', 'report_stratum_tcp_ssl_port', '')
    config.set('server', 'ssl_certfile', '')
    config.set('server', 'ssl_keyfile', '')
    config.set('server', 'irc', 'no')
    config.set('server', 'irc_nick', '')
    config.set('server', 'coin', '')
    config.set('server', 'donation_address', '')
    config.set('server', 'max_subscriptions', '10000')

    config.add_section('leveldb')
    config.set('leveldb', 'path', '/dev/shm/electrum_db')
    config.set('leveldb', 'pruning_limit', '100')
    config.set('leveldb', 'reorg_limit', '100')
    config.set('leveldb', 'utxo_cache', str(64*1024*1024))
    config.set('leveldb', 'hist_cache', str(128*1024*1024))
    config.set('leveldb', 'addr_cache', str(16*1024*1024))
    config.set('leveldb', 'profiler', 'no')

    # set network parameters
    config.add_section('network')
    config.set('network', 'type', 'bitcoin_main')

    # try to find the config file in the default paths
    if not filename:
        for path in ('/etc/', ''):
            filename = path + 'electrum.conf'
            if os.path.isfile(filename):
                break

    if not os.path.isfile(filename):
        print 'could not find electrum configuration file "%s"' % filename
        sys.exit(1)

    attempt_read_config(config, filename)

    load_banner(config)

    return config


def run_rpc_command(params, electrum_rpc_port):
    cmd = params[0]
    import xmlrpclib
    server = xmlrpclib.ServerProxy('http://localhost:%d' % electrum_rpc_port)
    func = getattr(server, cmd)
    r = func(*params[1:])
    if cmd == 'sessions':
        now = time.time()
        print 'type           address         sub  version  time'
        for item in r:
            print '%4s   %21s   %3s  %7s  %.2f' % (item.get('name'),
                                                   item.get('address'),
                                                   item.get('subscriptions'),
                                                   item.get('version'),
                                                   (now - item.get('time')),
                                                   )
    elif cmd == 'debug':
        print r
    else:
        print json.dumps(r, indent=4, sort_keys=True)


def cmd_banner_update():
    load_banner(dispatcher.shared.config)
    return True

def cmd_getinfo():
    return {
        'blocks': chain_proc.storage.height,
        'peers': len(server_proc.peers),
        'sessions': len(dispatcher.request_dispatcher.get_sessions()),
        'watched': len(chain_proc.watched_addresses),
        'cached': len(chain_proc.history_cache),
        }

def cmd_sessions():
    return map(lambda s: {"time": s.time,
                          "name": s.name,
                          "address": s.address,
                          "version": s.version,
                          "subscriptions": len(s.subscriptions)},
               dispatcher.request_dispatcher.get_sessions())

def cmd_numsessions():
    return len(dispatcher.request_dispatcher.get_sessions())

def cmd_peers():
    return server_proc.peers.keys()

def cmd_numpeers():
    return len(server_proc.peers)


hp = None
def cmd_guppy():
    from guppy import hpy
    global hp
    hp = hpy()

def cmd_debug(s):
    import traceback
    import gc
    if s:
        try:
            result = str(eval(s))
        except:
            err_lines = traceback.format_exc().splitlines()
            result = '%s | %s' % (err_lines[-3], err_lines[-1])
        return result


def get_port(config, name):
    try:
        return config.getint('server', name)
    except:
        return None


# share these as global, for 'debug' command
shared = None
chain_proc = None
server_proc = None
dispatcher = None
transports = []
tcp_server = None
ssl_server = None

def start_server(config):
    global shared, chain_proc, server_proc, dispatcher
    global tcp_server, ssl_server

    utils.init_logger()
    host = config.get('server', 'host')
    stratum_tcp_port = get_port(config, 'stratum_tcp_port')
    stratum_tcp_ssl_port = get_port(config, 'stratum_tcp_ssl_port')
    ssl_certfile = config.get('server', 'ssl_certfile')
    ssl_keyfile = config.get('server', 'ssl_keyfile')

    setup_network_params(config)

    if ssl_certfile is '' or ssl_keyfile is '':
        stratum_tcp_ssl_port = None

    print_log("Starting Electrum server on", host)

    # Create hub
    dispatcher = Dispatcher(config)
    shared = dispatcher.shared

    # handle termination signals
    import signal
    def handler(signum = None, frame = None):
        print_log('Signal handler called with signal', signum)
        shared.stop()
    for sig in [signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT]:
        signal.signal(sig, handler)

    # Create and register processors
    chain_proc = BlockchainProcessor(config, shared)
    dispatcher.register('blockchain', chain_proc)

    server_proc = ServerProcessor(config, shared)
    dispatcher.register('server', server_proc)

    # Create various transports we need
    if stratum_tcp_port:
        tcp_server = TcpServer(dispatcher, host, stratum_tcp_port, False, None, None)
        transports.append(tcp_server)

    if stratum_tcp_ssl_port:
        ssl_server = TcpServer(dispatcher, host, stratum_tcp_ssl_port, True, ssl_certfile, ssl_keyfile)
        transports.append(ssl_server)

    for server in transports:
        server.start()


def stop_server():
    shared.stop()
    server_proc.join()
    chain_proc.join()
    print_log("Electrum Server stopped")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--conf', metavar='path', default=None, help='specify a configuration file')
    parser.add_argument('command', nargs='*', default=[], help='send a command to the server')
    args = parser.parse_args()
    config = create_config(args.conf)

    electrum_rpc_port = get_port(config, 'electrum_rpc_port')

    if len(args.command) >= 1:
        try:
            run_rpc_command(args.command, electrum_rpc_port)
        except socket.error:
            print "server not running"
            sys.exit(1)
        sys.exit(0)

    try:
        run_rpc_command(['getpid'], electrum_rpc_port)
        is_running = True
    except socket.error:
        is_running = False

    if is_running:
        print "server already running"
        sys.exit(1)

    start_server(config)

    from SimpleXMLRPCServer import SimpleXMLRPCServer
    server = SimpleXMLRPCServer(('localhost', electrum_rpc_port), allow_none=True, logRequests=False)
    server.register_function(lambda: os.getpid(), 'getpid')
    server.register_function(shared.stop, 'stop')
    server.register_function(cmd_getinfo, 'getinfo')
    server.register_function(cmd_sessions, 'sessions')
    server.register_function(cmd_numsessions, 'numsessions')
    server.register_function(cmd_peers, 'peers')
    server.register_function(cmd_numpeers, 'numpeers')
    server.register_function(cmd_debug, 'debug')
    server.register_function(cmd_guppy, 'guppy')
    server.register_function(cmd_banner_update, 'banner_update')
    server.socket.settimeout(1)
 
    while not shared.stopped():
        try:
            server.handle_request()
        except socket.timeout:
            continue
        except:
            stop_server()
