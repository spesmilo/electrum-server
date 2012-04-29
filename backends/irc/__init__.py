import threading, socket, traceback, time, sys

def random_string(N):
    import random, string
    return ''.join(random.choice(string.ascii_uppercase + string.digits) for x in range(N))

from processor import Processor

class ServerProcessor(Processor):

    def __init__(self, config):
        Processor.__init__(self)
        self.daemon = True
        self.peers = {}
        self.banner = config.get('server','banner')
        self.host = config.get('server','host')
        self.password = config.get('server','password')

        self.native_port = config.get('server','native_port')
        self.stratum_tcp_port = config.get('server','stratum_tcp_port')
        self.stratum_http_port = config.get('server','stratum_http_port')

        self.irc = config.get('server', 'irc') == 'yes'
        self.nick = config.get('server', 'irc_nick') 
        if not self.nick: self.nick = random_string(10)


    def get_peers(self):
        return self.peers.values()


    def getname(self):
        s = ''
        if self.native_port:
            s+= 'n' + self.native_port + ' '
        if self.stratum_tcp_port:
            s += 't' + self.stratum_tcp_port + ' ' 
        if self.stratum_http_port:
            s += 'h' + self.stratum_http_port + ' '
        return s


    def run(self):
        if not self.irc: 
            return

        ircname = self.getname()

        while not self.shared.stopped():
            try:
                s = socket.socket()
                s.connect(('irc.freenode.net', 6667))
                s.send('USER electrum 0 * :' + self.host + ' ' + ircname + '\n')
                s.send('NICK E_' + self.nick + '\n')
                s.send('JOIN #electrum\n')
                sf = s.makefile('r', 0)
                t = 0
                while not self.shared.stopped():
                    line = sf.readline()
                    line = line.rstrip('\r\n')
                    line = line.split()
                    if line[0]=='PING': 
                        s.send('PONG '+line[1]+'\n')
                    elif '353' in line: # answer to /names
                        k = line.index('353')
                        for item in line[k+1:]:
                            if item[0:2] == 'E_':
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
                        self.push_response({'method':'server.peers', 'params':[self.get_peers()]})
                        s.send('NAMES #electrum\n')
                        t = time.time()
                        self.peers = {}
            except:
                traceback.print_exc(file=sys.stdout)
            finally:
                sf.close()
                s.close()



    def process(self, request):
        method = request['method']
        params = request['params']
        result = None

        if method == 'server.banner':
            result = self.banner.replace('\\n','\n')

        elif method == 'server.peers.subscribe':
            result = self.get_peers()

        elif method == 'server.version':
            pass

        elif method == 'server.stop':
            print "stopping..."
            try:
                password = request['params'][0]
            except:
                password = None
            if password == self.password:
                self.shared.stop()
                result = 'ok'
        else:
            print "unknown method", request

        if result!='':
            response = { 'id':request['id'], 'method':method, 'params':params, 'result':result }
            self.push_response(response)

