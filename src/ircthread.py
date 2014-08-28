import re
import time
import socket
import threading
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
        self.stratum_tcp_port = config.get('server', 'stratum_tcp_port')
        self.stratum_http_port = config.get('server', 'stratum_http_port')
        self.stratum_tcp_ssl_port = config.get('server', 'stratum_tcp_ssl_port')
        self.stratum_http_ssl_port = config.get('server', 'stratum_http_ssl_port')
        self.report_stratum_tcp_port = config.get('server', 'report_stratum_tcp_port')
        self.report_stratum_http_port = config.get('server', 'report_stratum_http_port')
        self.report_stratum_tcp_ssl_port = config.get('server', 'report_stratum_tcp_ssl_port')
        self.report_stratum_http_ssl_port = config.get('server', 'report_stratum_http_ssl_port')
        self.host = config.get('server', 'host')
        self.report_host = config.get('server', 'report_host')
        self.nick = config.get('server', 'irc_nick')
        if self.report_stratum_tcp_port:
            self.stratum_tcp_port = self.report_stratum_tcp_port
        if self.report_stratum_http_port:
            self.stratum_http_port = self.report_stratum_http_port
        if self.report_stratum_tcp_ssl_port:
            self.stratum_tcp_ssl_port = self.report_stratum_tcp_ssl_port
        if self.report_stratum_http_ssl_port:
            self.stratum_http_ssl_port = self.report_stratum_http_ssl_port
        if self.report_host:
            self.host = self.report_host
        if not self.nick:
            self.nick = Hash(self.host)[:5].encode("hex")
        self.pruning = True
        self.pruning_limit = config.get('leveldb', 'pruning_limit')
        self.nick = 'E_' + self.nick
        self.password = None

    def getname(self):
        s = 'v' + VERSION + ' '
        if self.pruning:
            s += 'p' + self.pruning_limit + ' '

        def add_port(letter, number):
            DEFAULT_PORTS = {'t':'50001', 's':'50002', 'h':'8081', 'g':'8082'}
            if not number: return ''
            if DEFAULT_PORTS[letter] == number:
                return letter + ' '
            else:
                return letter + number + ' '

        s += add_port('t',self.stratum_tcp_port)
        s += add_port('h',self.stratum_http_port)
        s += add_port('s',self.stratum_tcp_ssl_port)
        s += add_port('g',self.stratum_http_ssl_port)
        return s

    def start(self, queue):
        self.queue = queue
        threading.Thread.start(self)
 
    def on_connect(self, connection, event):
        connection.join("#electrum")

    def on_join(self, connection, event):
        m = re.match("(E_.*)!", event.source)
        if m:
            connection.who(m.group(1))

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
            logger.error("gethostbyname error" + line[1])
            return
        nick = event.arguments[4]
        host = line[1]
        ports = line[2:]
        self.queue.put(('join', [nick, ip, host, ports]))

    def on_name(self, connection, event):
        for s in event.arguments[2].split():
            if s.startswith("E_"):
                connection.who(s)

    def run(self):
        while self.processor.shared.paused():
            time.sleep(1)

        self.ircname = self.host + ' ' + self.getname()
        logger.info("joining IRC")

        while not self.processor.shared.stopped():
            client = irc.client.IRC()
            try:
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
