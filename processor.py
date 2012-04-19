import json
import socket
import threading
import time
import traceback, sys
import Queue as queue

def random_string(N):
    import random, string
    return ''.join(random.choice(string.ascii_uppercase + string.digits) for x in range(N))

def timestr():
    return time.strftime("[%d/%m/%Y-%H:%M:%S]")


class Shared:

    def __init__(self):
        self.lock = threading.Lock()
        self._stopped = False

    def stop(self):
        print "Stopping Stratum"
        with self.lock:
            self._stopped = True

    def stopped(self):
        with self.lock:
            return self._stopped


class Processor(threading.Thread):

    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.dispatcher = None

    def process(self, request):
        pass

    def push_response(self, response):
        print "response", response
        #self.dispatcher.request_dispatcher.push_response(response)



class Dispatcher:

    def __init__(self):
        self.shared = Shared()
        self.request_dispatcher = RequestDispatcher(self.shared)
        self.request_dispatcher.start()
        self.response_dispatcher = ResponseDispatcher(self.shared, self.request_dispatcher)
        self.response_dispatcher.start()

    def register(self, prefix, processor):
        processor.dispatcher = self
        processor.shared = self.shared
        processor.start()
        self.request_dispatcher.processors[prefix] = processor



class RequestDispatcher(threading.Thread):

    def __init__(self, shared):
        self.shared = shared
        threading.Thread.__init__(self)
        self.daemon = True
        self.request_queue = queue.Queue()
        self.response_queue = queue.Queue()
        self.internal_ids = {}
        self.internal_id = 1
        self.lock = threading.Lock()
        self.sessions = []
        self.processors = {}

    def push_response(self, item):
        self.response_queue.put(item)

    def pop_response(self):
        return self.response_queue.get()

    def push_request(self, session, item):
        self.request_queue.put((session,item))

    def pop_request(self):
        return self.request_queue.get()

    def get_session_id(self, internal_id):
        with self.lock:
            return self.internal_ids.pop(internal_id)

    def store_session_id(self, session, msgid):
        with self.lock:
            self.internal_ids[self.internal_id] = session, msgid
            r = self.internal_id
            self.internal_id += 1
            return r

    def run(self):
        if self.shared is None:
            raise TypeError("self.shared not set in Processor")
        while not self.shared.stopped():
            session, request = self.pop_request()
            self.process(session, request)

        self.stop()

    def stop(self):
        pass

    def process(self, session, request):
        method = request['method']
        params = request.get('params',[])

        suffix = method.split('.')[-1]
        if suffix == 'subscribe':
            session.subscribe_to_service(method, params)

        # store session and id locally
        request['id'] = self.store_session_id(session, request['id'])

        # dispatch request to the relevant module..
        prefix = request['method'].split('.')[0]
        try:
            p = self.processors[prefix]
        except:
            print "error: no processor for", prefix
            return
        try:
            p.process(request)
        except:
            traceback.print_exc(file=sys.stdout)

        if method in ['server.version']:
            session.version = params[0]


    def add_session(self, session):
        with self.lock:
            self.sessions.append(session)

    def collect_garbage(self):
        # Deep copy entire sessions list and blank it
        # This is done to minimise lock contention
        with self.lock:
            sessions = self.sessions[:]
            self.sessions = []
        for session in sessions:
            if not session.stopped():
                # If session is still alive then re-add it back
                # to our internal register
                self.add_session(session)


class Session:

    def __init__(self):
        self._stopped = False
        self.lock = threading.Lock()
        self.subscriptions = []
        self.address = ''
        self.name = ''
        threading.Timer(2, self.info).start()

    def info(self):
        for s in self.subscriptions:
            m, p = s
            if m == 'blockchain.address.subscribe':
                addr = p[0]
                break
        else:
            addr = None
        print timestr(), self.name, self.address, addr, len(self.subscriptions), self.version

    def stopped(self):
        with self.lock:
            return self._stopped

    def subscribe_to_service(self, method, params):
        with self.lock:
            self.subscriptions.append((method, params))
    

class ResponseDispatcher(threading.Thread):

    def __init__(self, shared, processor):
        self.shared = shared
        self.processor = processor
        threading.Thread.__init__(self)
        self.daemon = True

    def run(self):
        while not self.shared.stopped():
            response = self.processor.pop_response()
            #print "pop response", response
            internal_id = response.get('id')
            params = response.get('params', [])

            # This is wrong. "params" and "method" should never
            # be in a response.
            if internal_id is None:
                method = response.get('method')
                if method is None:
                    print "no method", response
                    continue
                for session in self.processor.sessions:
                    if not session.stopped():
                        if (method,params) in session.subscriptions:
                            session.send_response(response)
                continue

            session, message_id = self.processor.get_session_id(internal_id)
            response['id'] = message_id
            session.send_response(response)

