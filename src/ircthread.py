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

import re
import time
import socket
import ssl
import threading
import Queue
import irc.client
from utils import logger
from utils import Hash
from version import VERSION

out_msg = []

class IrcThread(threading.Thread):

    def __init__(self, processor, config):
        threading.Thread.__init__(self)
        self.processor = processor
        self.daemon = True
        options = dict(config.items('server'))
        self.stratum_tcp_port = options.get('stratum_tcp_port')
        self.stratum_tcp_ssl_port = options.get('stratum_tcp_ssl_port')
        self.report_stratum_tcp_port = options.get('report_stratum_tcp_port')
        self.report_stratum_tcp_ssl_port = options.get('report_stratum_tcp_ssl_port')
        self.irc_bind_ip = options.get('irc_bind_ip')
        self.host = options.get('host')
        self.report_host = options.get('report_host')
        self.nick = options.get('irc_nick')
        if self.report_stratum_tcp_port:
            self.stratum_tcp_port = self.report_stratum_tcp_port
        if self.report_stratum_tcp_ssl_port:
            self.stratum_tcp_ssl_port = self.report_stratum_tcp_ssl_port
        if self.report_host:
            self.host = self.report_host
        if not self.nick:
            self.nick = Hash(self.host)[:5].encode("hex")
        self.pruning = True
        self.pruning_limit = config.get('leveldb', 'pruning_limit')
        self.nick = 'E_' + self.nick
        self.password = None
        self.who_queue = Queue.Queue()

    def getname(self):
        s = 'v' + VERSION + ' '
        if self.pruning:
            s += 'p' + self.pruning_limit + ' '

        def add_port(letter, number):
            DEFAULT_PORTS = {'t':'50001', 's':'50002'}
            if not number: return ''
            if DEFAULT_PORTS[letter] == number:
                return letter + ' '
            else:
                return letter + number + ' '

        s += add_port('t',self.stratum_tcp_port)
        s += add_port('s',self.stratum_tcp_ssl_port)
        return s

    def start(self, queue):
        self.queue = queue
        threading.Thread.start(self)

    def on_connect(self, connection, event):
        connection.join("#electrum")

    def on_join(self, connection, event):
        m = re.match("(E_.*)!", event.source)
        if m:
            self.who_queue.put((connection, m.group(1)))

    def on_quit(self, connection, event):
        m = re.match("(E_.*)!", event.source)
        if m:
            self.queue.put(('quit', [m.group(1)]))

    def on_kick(self, connection, event):
        m = re.match("(E_.*)", event.arguments[0])
        if m:
            self.queue.put(('quit', [m.group(1)]))

    def on_disconnect(self, connection, event):
        logger.error("irc: disconnected")
        raise BaseException("disconnected")

    def on_who(self, connection, event):
        line = str(event.arguments[6]).split()
        try:
            ip = socket.gethostbyname(line[1])
        except:
            # no IPv4 address could be resolved. Could be .onion or IPv6.
            ip = line[1]
        nick = event.arguments[4]
        host = line[1]
        ports = line[2:]
        self.queue.put(('join', [nick, ip, host, ports]))

    def on_name(self, connection, event):
        for s in event.arguments[2].split():
            if s.startswith("E_"):
                self.who_queue.put((connection, s))

    def who_thread(self):
        while not self.processor.shared.stopped():
            try:
                connection, s = self.who_queue.get(timeout=1)
            except Queue.Empty:
                continue
            #logger.info("who: "+ s)
            connection.who(s)
            time.sleep(1)

    def run(self):

        while self.processor.shared.paused():
            time.sleep(1)

        self.ircname = self.host + ' ' + self.getname()
        # avoid UnicodeDecodeError using LenientDecodingLineBuffer
        irc.client.ServerConnection.buffer_class = irc.buffer.LenientDecodingLineBuffer
        logger.info("joining IRC")

        t = threading.Thread(target=self.who_thread)
        t.start()

        while not self.processor.shared.stopped():
            client = irc.client.Reactor()
            try:
                #bind_address = (self.irc_bind_ip, 0) if self.irc_bind_ip else None
                #ssl_factory = irc.connection.Factory(wrapper=ssl.wrap_socket, bind_address=bind_address)
                #c = client.server().connect('irc.freenode.net', 6697, self.nick, self.password, ircname=self.ircname, connect_factory=ssl_factory)
                c = client.server().connect('irc.freenode.net', 6667, self.nick, self.password, ircname=self.ircname) 
            except irc.client.ServerConnectionError:
                logger.error('irc', exc_info=True)
                time.sleep(10)
                continue

            c.add_global_handler("welcome", self.on_connect)
            c.add_global_handler("join", self.on_join)
            c.add_global_handler("quit", self.on_quit)
            c.add_global_handler("kick", self.on_kick)
            c.add_global_handler("whoreply", self.on_who)
            c.add_global_handler("namreply", self.on_name)
            c.add_global_handler("disconnect", self.on_disconnect)
            c.set_keepalive(60)

            self.connection = c
            try:
                client.process_forever()
            except BaseException as e:
                logger.error('irc', exc_info=True)
                time.sleep(10)
                continue

        logger.info("quitting IRC")
