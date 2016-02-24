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


from processor import Processor
from utils import Hash, print_log
from version import VERSION
from utils import logger
from ircthread import IrcThread



class ServerProcessor(Processor):

    def __init__(self, config, shared):
        Processor.__init__(self)
        self.daemon = True
        self.config = config
        self.shared = shared
        self.irc_queue = Queue.Queue()
        self.peers = {}

        if self.config.get('server', 'irc') == 'yes':
            self.irc = IrcThread(self, self.config)
            self.irc.start(self.irc_queue)
            t = threading.Thread(target=self.read_irc_results)
            t.daemon = True
            t.start()
        else:
            self.irc = None


    def read_irc_results(self):
        while True:
            try:
                event, params = self.irc_queue.get(timeout=1)
            except Queue.Empty:
                continue
            #logger.info(event + ' ' + repr(params))
            if event == 'join':
                nick, ip, host, ports = params
                self.peers[nick] = (ip, host, ports)
            if event == 'quit':
                nick = params[0]
                if nick in self.peers:
                    del self.peers[nick]


    def get_peers(self):
        return self.peers.values()


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

        else:
            raise BaseException("unknown method: %s"%repr(method))

        return result
