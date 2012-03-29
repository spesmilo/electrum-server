####################################################################

import threading, socket, traceback, time

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
        self.nick = config.get('server','ircname')
        self.irc = config.get('server','irc') == 'yes'

    def get_peers(self):
        return self.peers.values()

    def run(self):
        if not self.irc: 
            return
        NICK = 'E_'+random_string(10)
        while not self.shared.stopped():
            try:
                s = socket.socket()
                s.connect(('irc.freenode.net', 6667))
                s.send('USER electrum 0 * :'+self.host+' '+self.nick+'\n')
                s.send('NICK '+NICK+'\n')
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
                        self.peers[name] = (ip,host)
                    if time.time() - t > 5*60:
                        self.push_response({'method':'server.peers', 'result':[self.get_peers()]})
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

        result = ''
        if method == 'server.banner':
            result = self.banner.replace('\\n','\n')
        elif method == 'server.peers.subscribe':
            result = self.get_peers()
        else:
            print "unknown method", request

        if result!='':
            response = { 'id':request['id'], 'method':method, 'params':request['params'], 'result':result }
            self.push_response(response)



