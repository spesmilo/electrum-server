import socket
import sys
import threading
import time
import traceback

from processor import Processor
from utils import Hash, print_log
from version import VERSION


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
        self.peers = {}
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
        self.prepend = 'E_'
        if config.get('server', 'coin') == 'litecoin':
            self.prepend = 'EL_'
        self.pruning = config.get('server', 'backend') == 'leveldb'
        if self.pruning:
            self.pruning_limit = config.get('leveldb', 'pruning_limit')
        self.nick = self.prepend + self.nick

    def get_peers(self):
        return self.peers.values()

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

    def run(self):
        ircname = self.getname()

        while not self.processor.shared.stopped():
            try:
                s = socket.socket()
                s.connect(('irc.freenode.net', 6667))
                s.settimeout(300)
            except:
                s.close()
                time.sleep(10)
                continue

            self.message = ''
            try:
                s.send('USER electrum 0 * :' + self.host + ' ' + ircname + '\n')
                s.send('NICK ' + self.nick + '\n')
                s.send('JOIN #electrum\n')
                t = 0
                while not self.processor.shared.stopped():
                    try:
                        data = s.recv(2048)
                    except:
                        print_log( "irc: socket error" )
                        time.sleep(1)
                        break

                    self.message += data

                    while self.message.find('\n') != -1:
                        pos = self.message.find('\n')
                        line = self.message[0:pos]
                        self.message = self.message[pos+1:]
                        line = line.strip('\r')
                        if not line:
                            continue
                        line = line.split()
                        if line[0] == 'PING':
                            s.send('PONG ' + line[1] + '\n')
                        elif '353' in line:  # answer to /names
                            k = line.index('353')
                            for item in line[k+1:]:
                                if item.startswith(self.prepend):
                                    s.send('WHO %s\n' % item)
                        elif '352' in line:  # answer to /who
                            # warning: this is a horrible hack which apparently works
                            k = line.index('352')
                            try:
                                ip = socket.gethostbyname(line[k+4])
                            except:
                                print_log("gethostbyname error", line[k+4])
                                continue
                            name = line[k+6]
                            host = line[k+9]
                            ports = line[k+10:]
                            self.peers[name] = (ip, host, ports)

                    if time.time() - t > 5*60:
                        #self.processor.push_response({'method': 'server.peers', 'params': [self.get_peers()]})
                        s.send('NAMES #electrum\n')
                        t = time.time()
                        self.peers = {}
            except:
                traceback.print_exc(file=sys.stdout)
                time.sleep(1)
            finally:
                s.close()

        print_log("quitting IRC")


class ServerProcessor(Processor):

    def __init__(self, config):
        Processor.__init__(self)
        self.daemon = True
        self.banner = config.get('server', 'banner')
        self.password = config.get('server', 'password')

        if config.get('server', 'irc') == 'yes':
            self.irc = IrcThread(self, config)
        else:
            self.irc = None

    def get_peers(self):
        if self.irc:
            return self.irc.get_peers()
        else:
            return []

    def run(self):
        if self.irc:
            self.irc.start()
        Processor.run(self)

    def process(self, session, request):
        method = request['method']
        params = request['params']
        result = None

        if method in ['server.stop', 'server.info', 'server.debug']:
            try:
                password = request['params'][0]
            except:
                password = None

            if password != self.password:
                self.push_response(session, 
                                   {'id': request['id'],
                                    'result': None,
                                    'error': 'incorrect password'})
                return

        if method == 'server.banner':
            result = self.banner.replace('\\n', '\n')

        elif method == 'server.peers.subscribe':
            result = self.get_peers()

        elif method == 'server.version':
            result = VERSION

        elif method == 'server.stop':
            self.shared.stop()
            result = 'stopping, please wait until all threads terminate.'

        elif method == 'server.info':
            result = map(lambda s: {"time": s.time,
                                    "name": s.name,
                                    "address": s.address,
                                    "version": s.version,
                                    "subscriptions": len(s.subscriptions)},
                         self.dispatcher.request_dispatcher.get_sessions())

        elif method == 'server.debug':
            try:
                s = request['params'][1]
            except:
                s = None

            if s:
                from guppy import hpy
                h = hpy()
                bp = self.dispatcher.request_dispatcher.processors['blockchain']
                try:
                    result = str(eval(s))
                except:
                    result = "error"
        else:
            print_log("unknown method", method)

        if result != '':
            self.push_response(session, {'id': request['id'], 'result': result})
