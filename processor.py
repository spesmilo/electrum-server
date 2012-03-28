import json
import socket
import threading
import time
import Queue as queue

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
        self.shared = None
        threading.Thread.__init__(self)
        self.daemon = True
        self.request_queue = queue.Queue()
        self.response_queue = queue.Queue()
        self.internal_ids = {}
        self.internal_id = 1
        self.lock = threading.Lock()
        self.sessions = []

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

            method = request['method']
            params = request.get('params',[])

            if method in [ 'numblocks.subscribe', 'address.subscribe', 'server.peers']:
                session.subscribe_to_service(method, params)

            # store session and id locally
            request['id'] = self.store_session_id(session, request['id'])
            self.process(request)

        self.stop()

    def stop(self):
        pass

    def process(self, request):
        print "New request", request
        # Do stuff...
        # response = request
        # When ready, you call
        # self.push_response(response)

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

    def stopped(self):
        with self.lock:
            return self._stopped

    def subscribe_to_service(self, method, params):
        with self.lock:
            self.subscriptions.append((method, params))
    

class Dispatcher(threading.Thread):

    def __init__(self, shared, processor):
        self.shared = shared
        self.processor = processor
        threading.Thread.__init__(self)

    def run(self):
        while not self.shared.stopped():
            response = self.processor.pop_response()
            #print "pop response", response
            internal_id = response.get('id')
            params = response.get('params',[])
            try:
                method = response['method']
            except:
                print "no method", response
                continue

            if internal_id:
                session, message_id = self.processor.get_session_id(internal_id)
                response['id'] = message_id
                session.send_response(response)

            else:
                for session in self.processor.sessions:
                    if not session.stopped():
                        if (method,params) in session.subscriptions:
                            session.send_response(response)

