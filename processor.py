import json
import Queue as queue
import socket
import threading
import time
import traceback
import sys

from utils import random_string, timestr, print_log


class Shared:

    def __init__(self, config):
        self.lock = threading.Lock()
        self._stopped = False
        self.config = config

    def stop(self):
        print_log("Stopping Stratum")
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
        self.queue = queue.Queue()

    def process(self, session, request):
        pass

    def add_request(self, session, request):
        self.queue.put((session, request))

    def push_response(self, session, response):
        #print "response", response
        self.dispatcher.request_dispatcher.push_response(session, response)

    def run(self):
        while not self.shared.stopped():
            request, session = self.queue.get(10000000000)
            try:
                self.process(request, session)
            except:
                traceback.print_exc(file=sys.stdout)

        print_log("processor terminating")


class Dispatcher:

    def __init__(self, config):
        self.shared = Shared(config)
        self.request_dispatcher = RequestDispatcher(self.shared)
        self.request_dispatcher.start()
        self.response_dispatcher = \
            ResponseDispatcher(self.shared, self.request_dispatcher)
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
        self.lock = threading.Lock()
        self.idlock = threading.Lock()
        self.sessions = []
        self.processors = {}

    def push_response(self, session, item):
        self.response_queue.put((session, item))

    def pop_response(self):
        return self.response_queue.get()

    def push_request(self, session, item):
        self.request_queue.put((session, item))

    def pop_request(self):
        return self.request_queue.get()

    def run(self):
        if self.shared is None:
            raise TypeError("self.shared not set in Processor")
        while not self.shared.stopped():
            session, request = self.pop_request()
            try:
                self.do_dispatch(session, request)
            except:
                traceback.print_exc(file=sys.stdout)

        self.stop()

    def stop(self):
        pass

    def do_dispatch(self, session, request):
        """ dispatch request to the relevant processor """

        method = request['method']
        params = request.get('params', [])
        suffix = method.split('.')[-1]

        if session is not None:
            if suffix == 'subscribe':
                session.subscribe_to_service(method, params)

        prefix = request['method'].split('.')[0]
        try:
            p = self.processors[prefix]
        except:
            print_log("error: no processor for", prefix)
            return

        p.add_request(session, request)

        if method in ['server.version']:
            session.version = params[0]
            try:
                session.protocol_version = float(params[1])
            except:
                pass


    def get_sessions(self):
        with self.lock:
            r = self.sessions[:]
        return r

    def add_session(self, session):
        with self.lock:
            self.sessions.append(session)

    def collect_garbage(self):
        # Deep copy entire sessions list and blank it
        # This is done to minimize lock contention
        with self.lock:
            sessions = self.sessions[:]

        active_sessions = []

        now = time.time()
        for session in sessions:
            if (now - session.time) > 1000:
                session.stop()

        bp = self.processors['blockchain']

        for session in sessions:
            if not session.stopped():
                # If session is still alive then re-add it back
                # to our internal register
                active_sessions.append(session)
            else:
                session.stop_subscriptions(bp)

        with self.lock:
            self.sessions = active_sessions[:]



class Session:

    def __init__(self):
        self._stopped = False
        self.lock = threading.Lock()
        self.subscriptions = []
        self.address = ''
        self.name = ''
        self.version = 'unknown'
        self.protocol_version = 0.
        self.time = time.time()
        threading.Timer(2, self.info).start()


    # Debugging method. Doesn't need to be threadsafe.
    def info(self):
        for sub in self.subscriptions:
            #print sub
            method = sub[0]
            if method == 'blockchain.address.subscribe':
                addr = sub[1]
                break
        else:
            addr = None

        if self.subscriptions:
            print_log("%4s" % self.name,
                      "%15s" % self.address,
                      "%35s" % addr,
                      "%3d" % len(self.subscriptions),
                      self.version)

    def stopped(self):
        with self.lock:
            return self._stopped


    def subscribe_to_service(self, method, params):
        with self.lock:
            if (method, params) not in self.subscriptions:
                self.subscriptions.append((method,params))


    def stop_subscriptions(self, bp):
        with self.lock:
            s = self.subscriptions[:]

        for method, params in s:
            with bp.watch_lock:
                if method == 'blockchain.numblocks.subscribe':
                    if self in bp.watch_blocks:
                        bp.watch_blocks.remove(self)
                elif method == 'blockchain.headers.subscribe':
                    if self in bp.watch_headers:
                        bp.watch_headers.remove(self)
                elif method == "blockchain.address.subscribe":
                    addr = params[0]
                    l = bp.watched_addresses.get(addr)
                    if not l:
                        continue
                    if self in l:
                        l.remove(self)
                    if l == []:
                        bp.watched_addresses.pop(addr)

        with self.lock:
            self.subscriptions = []


class ResponseDispatcher(threading.Thread):

    def __init__(self, shared, request_dispatcher):
        self.shared = shared
        self.request_dispatcher = request_dispatcher
        threading.Thread.__init__(self)
        self.daemon = True

    def run(self):
        while not self.shared.stopped():
            session, response = self.request_dispatcher.pop_response()
            session.send_response(response)
