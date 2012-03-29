import thread, threading, time, socket, traceback, ast, sys



def random_string(N):
    import random, string
    return ''.join(random.choice(string.ascii_uppercase + string.digits) for x in range(N))

def timestr():
    return time.strftime("[%d/%m/%Y-%H:%M:%S]")


class NativeServer(threading.Thread):

    def __init__(self, shared, abe, irc, banner, host, port):
        threading.Thread.__init__(self)
        self.banner = banner
        self.abe = abe
        self.store = abe.store
        self.irc = irc
        self.sessions = {}
        self.host = host
        self.port = port
        self.daemon = True
        self.shared = shared


    def modified_addresses(self,a_session):
        import copy
        session = copy.deepcopy(a_session)
        addresses = session['addresses']
        session['last_time'] = time.time()
        ret = {}
        k = 0
        for addr in addresses.keys():
            status = self.store.get_status( addr )
            msg_id, last_status = addresses[addr]
            if last_status != status:
                addresses[addr] = msg_id, status
                ret[addr] = status

        return ret, addresses


    def poll_session(self, session_id): 
        session = self.sessions.get(session_id)
        if session is None:
            print time.asctime(), "session not found", session_id
            return -1, {}
        else:
            self.sessions[session_id]['last_time'] = time.time()
            ret, addresses = self.modified_addresses(session)
            if ret: self.sessions[session_id]['addresses'] = addresses
            return repr( (self.abe.block_number,ret))


    def add_address_to_session(self, session_id, address):
        status = self.store.get_status(address)
        self.sessions[session_id]['addresses'][address] = ("", status)
        self.sessions[session_id]['last_time'] = time.time()
        return status


    def new_session(self, version, addresses):
        session_id = random_string(10)
        self.sessions[session_id] = { 'addresses':{}, 'version':version }
        for a in addresses:
            self.sessions[session_id]['addresses'][a] = ('','')
        out = repr( (session_id, self.banner.replace('\\n','\n') ) )
        self.sessions[session_id]['last_time'] = time.time()
        return out


    def update_session(self, session_id,addresses):
        """deprecated in 0.42, wad replaced by add_address_to_session"""
        self.sessions[session_id]['addresses'] = {}
        for a in addresses:
            self.sessions[session_id]['addresses'][a] = ('','')
        self.sessions[session_id]['last_time'] = time.time()
        return 'ok'



    def native_client_thread(self, ipaddr,conn):
        try:
            ipaddr = ipaddr[0]
            msg = ''
            while 1:
                d = conn.recv(1024)
                msg += d
                if not d: 
                    break
                if '#' in msg:
                    msg = msg.split('#', 1)[0]
                    break
            try:
                cmd, data = ast.literal_eval(msg)
            except:
                print "syntax error", repr(msg), ipaddr
                conn.close()
                return

            out = self.do_command(cmd, data, ipaddr)
            if out:
                #print ipaddr, cmd, len(out)
                try:
                    conn.send(out)
                except:
                    print "error, could not send"

        finally:
            conn.close()



    def do_command(self, cmd, data, ipaddr):

        if cmd=='b':
            out = "%d"%self.abe.block_number

        elif cmd in ['session','new_session']:
            try:
                if cmd == 'session':
                    addresses = ast.literal_eval(data)
                    version = "old"
                else:
                    version, addresses = ast.literal_eval(data)
                    if version[0]=="0": version = "v" + version
            except:
                print "error", data
                return None
            print timestr(), "new session", ipaddr, addresses[0] if addresses else addresses, len(addresses), version
            out = self.new_session(version, addresses)

        elif cmd=='address.subscribe':
            try:
                session_id, addr = ast.literal_eval(data)
            except:
                traceback.print_exc(file=sys.stdout)
                print data
                return None
            out = self.add_address_to_session(session_id,addr)

        elif cmd=='update_session':
            try:
                session_id, addresses = ast.literal_eval(data)
            except:
                traceback.print_exc(file=sys.stdout)
                return None
            print timestr(), "update session", ipaddr, addresses[0] if addresses else addresses, len(addresses)
            out = self.update_session(session_id,addresses)
            
        elif cmd=='poll': 
            out = self.poll_session(data)

        elif cmd == 'h': 
            address = data
            out = repr( self.store.get_history( address ) )

        elif cmd =='tx':
            out = self.store.send_tx(data)
            print timestr(), "sent tx:", ipaddr, out

        elif cmd == 'peers':
            out = repr(self.irc.get_peers())

        else:
            out = None

        return out


    def clean_session_thread(self):
        while not self.shared.stopped():
            time.sleep(30)
            t = time.time()
            for k,s in self.sessions.items():
                if s.get('type') == 'persistent': continue
                t0 = s['last_time']
                if t - t0 > 5*60:
                    self.sessions.pop(k)
                    print "lost session", k
            
    def run(self):
        thread.start_new_thread(self.clean_session_thread, ())

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port)) 
        s.listen(1)
        while not self.shared.stopped():
            conn, addr = s.accept()
            try:
                thread.start_new_thread(self.native_client_thread, (addr, conn,))
            except:
                # can't start new thread if there is no memory..
                traceback.print_exc(file=sys.stdout)

