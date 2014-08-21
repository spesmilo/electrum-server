import socket
import time
import re
import threading

from utils import logger
from utils import Hash, print_log
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

    def run(self):
        while self.processor.shared.paused():
            time.sleep(1)

        ircname = self.getname()
        print_log("joining IRC")

        while not self.processor.shared.stopped():
            try:
                s = socket.socket()
                s.connect(('irc.freenode.net', 6667))
                s.settimeout(1)
            except:
                s.close()
                print_log("IRC: reconnect in 10 s")
                time.sleep(10)
                continue

            self.message = ''
            out_msg = []

            try:
                s.send('USER electrum 0 * :' + self.host + ' ' + ircname + '\n')
                s.send('NICK ' + self.nick + '\n')
                if self.password:
                    s.send('NICKSERV IDENTIFY ' + self.password + '\n')
                s.send('JOIN #electrum\n')

                t = time.time()
                while not self.processor.shared.stopped():
                    try:
                        data = s.recv(2048)
                    except socket.timeout, e:
                        if time.time() - t > 120:
                            out_msg.append('PING')
                        if out_msg:
                            m = out_msg.pop(0)
                            #print_log("-->", m)
                            s.send(m + '\n')
                        continue
                    except:
                        print_log("irc: socket error")
                        time.sleep(1)
                        break

                    self.message += data
                    t = time.time()

                    while self.message.find('\n') != -1:
                        pos = self.message.find('\n')
                        line = self.message[0:pos]
                        self.message = self.message[pos+1:]
                        line = line.strip('\r')
                        if not line:
                            continue
                        # print_log("<--", line)
                        line = line.split()
                        if line[0] == 'PING':
                            out_msg.append('PONG ' + line[1])

                        elif line[1] == 'JOIN' and line[2] == '#electrum':
                            m = re.match(":(E_.*)!",line[0])
                            if m:
                                out_msg.append('WHO %s' % m.group(1))

                        elif line[1] == 'QUIT':
                            m = re.match(":(E_.*)!",line[0])
                            if m:
                                self.queue.put(('quit', [m.group(1)]))

                        elif line[1] == '353':  # answer to /names
                            for item in line[2:]:
                                if item.startswith("E_"):
                                    out_msg.append('WHO %s' % item)
                                elif item.startswith(":E_"):
                                    out_msg.append('WHO %s' % item[1:])

                        elif line[1] == '352':  # answer to /who
                            try:
                                ip = socket.gethostbyname(line[5])
                            except:
                                print_log("gethostbyname error", line[5])
                                continue
                            name = line[7]
                            host = line[10]
                            ports = line[11:]
                            self.queue.put(('join', [name, ip, host, ports]))

                        elif line[1] == 'KICK':
                            m = re.match(":(E_.*)!",line[3])
                            if m:
                                self.queue.put(('quit', [m.group(1)]))
                            try:
                                print_log("KICK", line[3] + line[4])
                            except:
                                print_log("KICK", "error")

                    #if time.time() - t > 5*60:
                    #    #self.processor.push_response({'method': 'server.peers', 'params': [self.get_peers()]})
                    #    s.send('NAMES #electrum\n')
                    #    t = time.time()
                    #    self.peers = {}
            except:
                logger.error('irc', exc_info=True)
                time.sleep(1)
            finally:
                s.close()

        print_log("quitting IRC")



