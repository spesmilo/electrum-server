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

import socket
import sys
import threading
import time
import Queue
import select

import electrum
from electrum import Connection, Interface, SimpleConfig


from processor import Processor
import re

from utils import Hash, print_log
from version import VERSION
from utils import logger


class Network(threading.Thread):

    def wait_on_sockets(self):
        if not self.interfaces:
            time.sleep(0.1)
            return
        rin = [i for i in self.interfaces.values()]
        win = [i for i in self.interfaces.values() if i.unsent_requests]
        try:
            rout, wout, xout = select.select(rin, win, [], 0.1)
        except socket.error as (code, msg):
            if code == errno.EINTR:
                return
            raise
        assert not xout
        for interface in wout:
            interface.send_requests()
        for interface in rout:
            self.process_responses(interface)

    def process_responses(self, interface):
        responses = interface.get_responses()
        for request, response in responses:
            if request:
                method, params, message_id = request
            else:
                if not response:  # Closed remotely / misbehaving
                    self.connection_down(interface.server)
                    break
                # Rewrite response shape to match subscription request response
                method = response.get('method')
                params = response.get('params')
                if method == 'blockchain.headers.subscribe':
                    response['result'] = params[0]
                    response['params'] = []

            self.process_response(interface.server, method, params, response)
 

    def add_interface(self, server, socket):
        self.interfaces[server] = i = Interface(server, socket)
        return i


class P2PThread(Network):

    def __init__(self, processor, config):
        threading.Thread.__init__(self)
        self.daemon = True
        self.processor = processor

        self.config = SimpleConfig()
        self.peers_queue = Queue.Queue()
        self.interfaces_queue = Queue.Queue()
        self.headers_queue = Queue.Queue()

        self.interfaces = {}

        self.peers_header = {}
        self.peers_version = {}

        self.known_peers = set([])
        self.good_peers = {}

        options = dict(config.items('server'))
        self.stratum_tcp_port = options.get('stratum_tcp_port')
        self.stratum_tcp_ssl_port = options.get('stratum_tcp_ssl_port')
        self.report_stratum_tcp_port = options.get('report_stratum_tcp_port')
        self.report_stratum_tcp_ssl_port = options.get('report_stratum_tcp_ssl_port')
        self.host = options.get('host')
        self.report_host = options.get('report_host')
        if self.report_stratum_tcp_port:
            self.stratum_tcp_port = self.report_stratum_tcp_port
        if self.report_stratum_tcp_ssl_port:
            self.stratum_tcp_ssl_port = self.report_stratum_tcp_ssl_port
        if self.report_host:
            self.host = self.report_host

        self.server_name = self.host + ':' + self.stratum_tcp_ssl_port + ':s'
        print "server name", self.server_name

        # hardcoded peers
        hh = set(['erbium1.sytes.net:50002:s', 'ecdsa.net:110:s', 'ulrichard.ch:50002:s'])
        for x in hh:
            self.add_peer(x)

    def add_peer(self, server):
        if server in self.known_peers:
            return
        self.known_peers.add(server)
        self.peers_queue.put(server)

    def process_response(self, server, method, params, response):
        error = response.get('error')
        if error:
            return

        result = response.get('result')

        if method == 'server.new_peer':
            # result should be "ok"
            pass

        if method == 'server.version':
            self.peers_version[server] = result

        elif method == 'server.peers.subscribe':
            for ip, host, data in result:
                for item in data:
                    if item == 's':
                        port = '50002'
                        break
                    m = re.match("s(\d*)",item)
                    if m:
                        port = m.group(1)
                        break
                else:
                    #print "no SSL port", ip, host, ports
                    continue
                peer = host + ':' + port + ':s'
                self.add_peer(peer)

        elif method == 'blockchain.headers.subscribe':
            self.peers_header[server] = result

    def run(self):

        while not self.processor.shared.stopped():

            # open connections with servers
            try:
                server = self.peers_queue.get(False)
            except Queue.Empty:
                pass
            else:
                conn = Connection(server, self.interfaces_queue, self.config.path)

            # request headers from servers
            try:
                server, socket = self.interfaces_queue.get(False)
            except Queue.Empty:
                pass
            else:
                if socket:
                    i = self.add_interface(server, socket)
                    i.queue_request('server.new_peer', [self.server_name], 0)
                    i.queue_request('server.peers.subscribe', [], 1)
                    i.queue_request('blockchain.headers.subscribe', [], 2)
                    i.queue_request('server.version', [], 3)
                else:
                    #print "failed to establish connection", server
                    pass

            self.wait_on_sockets()
            # when I receive headers, validate them

        
    def get_peers(self):
        out = []
        for server, v in self.peers_header.items():
            host, port, x = server.split(':')
            v = self.peers_version.get(host)
            if not v:
                continue
            out.append(('', host, ['s' if port == '50002' else 's'+port, 'v'+v]))
        return out


class ServerProcessor(Processor):

    def __init__(self, config, shared):
        Processor.__init__(self)
        self.daemon = True
        self.config = config
        self.shared = shared
        if self.config.get('server', 'p2p') == 'yes':
            self.p2p = P2PThread(self, self.config)
            self.p2p.start()
        else:
            self.p2p = None

    def get_peer_headers(self):
        out = {}
        if self.p2p:
            for k, v in self.p2p.peers_header.items():
                out[k] = v.get('utxo_root')
        return out

    def get_peers(self):
        return self.p2p.get_peers() if self.p2p else []

    def process(self, request):
        method = request['method']
        params = request['params']
        result = None

        if method == 'server.banner':
            result = self.config.get('server', 'banner').replace('\\n', '\n')

        elif method == 'server.donation_address':
            result = self.config.get('server', 'donation_address')

        elif method == 'server.peers.subscribe':
            result = self.get_peers()

        elif method == 'server.version':
            result = VERSION

        elif method == 'server.new_peer':
            self.p2p.add_peer(params[0])
            result = "ok"

        else:
            raise BaseException("unknown method: %s"%repr(method))

        return result


from processor import Shared
import ConfigParser

if __name__ == "__main__":
    #electrum.util.set_verbosity(1)
    config = ConfigParser.ConfigParser()
    config.add_section('server')
    config.set('server', 'p2p', 'yes')
    config.set('server', 'host', 'ecdsa.net')

    shared = Shared(config)

    proc = ServerProcessor(config, shared)
    proc.start()

    while not shared.stopped():
        try:
            time.sleep(1)
        except:
            shared.stop()
    #proc.join()
