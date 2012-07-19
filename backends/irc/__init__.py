import threading, socket, traceback, time, sys

def random_string(N):
    import random, string
    return ''.join(random.choice(string.ascii_uppercase + string.digits) for x in range(N))

from processor import Processor
from version import VERSION

class IrcThread(threading.Thread):

    def __init__(self, processor, config):
        threading.Thread.__init__(self)
        self.processor = processor
        self.daemon = True
        self.stratum_tcp_port = config.get('server','stratum_tcp_port')
        self.stratum_http_port = config.get('server','stratum_http_port')
        self.peers = {}
        self.host = config.get('server','host')
        self.nick = config.get('server', 'irc_nick')
        if not self.nick: self.nick = random_string(10)

    def get_peers(self):
        return self.peers.values()


    def getname(self):
        s = 'v' + VERSION + ' '
        if self.stratum_tcp_port:
            s += 't' + self.stratum_tcp_port + ' ' 
        if self.stratum_http_port:
            s += 'h' + self.stratum_http_port + ' '
        return s


    def run(self):
        ircname = self.getname()

        while not self.processor.shared.stopped():
            try:
                s = socket.socket()
                s.connect(('irc.freenode.net', 6667))
                s.send('USER electrum 0 * :' + self.host + ' ' + ircname + '\n')
                s.send('NICK EL_' + self.nick + '\n')
                s.send('JOIN #electrum\n')
                sf = s.makefile('r', 0)
                t = 0
                while not self.processor.shared.stopped():
                    line = sf.readline()
                    line = line.rstrip('\r\n')
                    line = line.split()
                    if not line: continue
                    if line[0]=='PING': 
                        s.send('PONG '+line[1]+'\n')
                    elif '353' in line: # answer to /names
                        k = line.index('353')
                        for item in line[k+1:]:
                            if item[0:2] == 'EL_':
                                s.send('WHO %s\n'%item)
                    elif '352' in line: # answer to /who
                        # warning: this is a horrible hack which apparently works
                        k = line.index('352')
                        ip = line[k+4]
                        ip = socket.gethostbyname(ip)
                        name = line[k+6]
                        host = line[k+9]
                        ports  = line[k+10:]
                        self.peers[name] = (ip, host, ports)
                    if time.time() - t > 5*60:
                        self.processor.push_response({'method':'server.peers', 'params':[self.get_peers()]})
                        s.send('NAMES #electrum\n')
                        t = time.time()
                        self.peers = {}
            except:
                traceback.print_exc(file=sys.stdout)
            finally:
                sf.close()
                s.close()

        print "quitting IRC"



class ServerProcessor(Processor):

    def __init__(self, config):
        Processor.__init__(self)
        self.daemon = True
        self.banner = config.get('server','banner')
        self.password = config.get('server','password')

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

    def process(self, request):
        method = request['method']
        params = request['params']
        result = None

        if method in ['server.stop', 'server.info']:
            try:
                password = request['params'][0]
            except:
                password = None

            if password != self.password:
                response = { 'id':request['id'], 'result':None,  'error':'incorrect password'}
                self.push_response(response)
                return

        if method == 'server.banner':
            result = self.banner.replace('\\n','\n')

        elif method == 'server.peers.subscribe':
            result = self.get_peers()

        elif method == 'server.version':
            result = VERSION

        elif method == 'server.stop':
            self.shared.stop()
            result = 'ok'

        elif method == 'server.info':
            result = map(lambda s: { "time":s.time, 
                                     "name":s.name,
                                     "address":s.address, 
                                     "version":s.version, 
                                     "subscriptions":len(s.subscriptions)}, 
                         self.dispatcher.request_dispatcher.get_sessions())

        elif method == 'server.cache':
            p = self.dispatcher.request_dispatcher.processors['blockchain']
            result = len(repr(p.store.tx_cache))

        elif method == 'server.load':
            p = self.dispatcher.request_dispatcher.processors['blockchain']
            result = p.queue.qsize()

        else:
            print "unknown method", request

        if result!='':
            response = { 'id':request['id'], 'result':result }
            self.push_response(response)

