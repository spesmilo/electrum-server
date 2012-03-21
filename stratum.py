import json
import socket
import threading
import time
import Queue as queue

class Processor(threading.Thread):

    def __init__(self):
        self.shared = None
        self.lock = threading.Lock()
        self.sessions = []
        threading.Thread.__init__(self)
        self.daemon = True

    def add_session(self, session):
        with self.lock:
            self.sessions.append(session)

    def run(self):
        if self.shared is None:
            raise TypeError("self.shared not set in Processor")
        while not self.shared.stopped():
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
                    self.process(session)

    def process(self, session):
        request = session.pop_request()
        print "New request", request
        # Execute and when ready, you call
        # session.push_response(response)

class Session:

    def __init__(self, connection, address):
        self._connection = connection
        self.address = address
        self._stopped = False
        self.lock = threading.Lock()

        self.request_queue = queue.Queue()
        self.response_queue = queue.Queue()

    def stop(self):
        self._connection.close()
        print "Terminating connection:", self.address[0]
        with self.lock:
            self._stopped = True

    def stopped(self):
        with self.lock:
            return self._stopped

    def connection(self):
        if self.stopped():
            raise Exception("Session was stopped")
        else:
            return self._connection

    def push_request(self, item):
        self.request_queue.put(item)

    def pop_request(self):
        return self.request_queue.get()

    def push_response(self):
        self.response_queue.put(item)

    def pop_response(self):
        return self.response_queue.get()

class TcpClientResponder(threading.Thread):

    def __init__(self, shared, session):
        self.shared = shared
        self.session = session
        threading.Thread.__init__(self)

    def run(self):
        while not self.shared.stopped() or self.session.stopped():
            response = self.session.pop_response()
            raw_response = json.dumps(response)
            # Possible race condition here by having session
            # close connection?
            # I assume Python connections are thread safe interfaces
            connection = self.session.connection()
            try:
                connection.send(raw_response + "\n")
            except:
                self.session.stop()

class TcpClientRequestor(threading.Thread):

    def __init__(self, shared, session):
        self.shared = shared
        self.message = ""
        self.session = session
        threading.Thread.__init__(self)

    def run(self):
        while not self.shared.stopped():
            if not self.update():
                self.session.stop()
                return

    def update(self):
        data = self.receive()
        if data is None:
            # close_session
            self.stop()
            return False

        self.message += data
        if not self.parse():
            return False
        return True

    def receive(self):
        try:
            return self.session.connection().recv(1024)
        except socket.error:
            return None

    def parse(self):
        while True:
            raw_buffer = self.message.find('\n')
            if raw_buffer == -1:
                return True

            command = self.message[0:raw_buffer].strip()
            self.message = self.message[raw_buffer + 1:]
            if command == 'quit': 
                return False

            try:
                command = json.loads(command)
            except:
                print "json error", repr(command)
                continue

            try:
                # Try to load vital fields, and return an error if
                # unsuccessful.
                message_id = command['id']
                method = command['method']
            except KeyError:
                # This should return an error JSON in response.
                print "syntax error", repr(command), self.session.address[0]
            else:
                self.session.push_request(command)

class TcpServer(threading.Thread):

    def __init__(self, shared, processor):
        self.shared = shared
        self.processor = processor
        self.clients = []
        threading.Thread.__init__(self)
        self.daemon = True

    def run(self):
        print "TCP server started."
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("localhost", 50001))
        sock.listen(1)
        while not self.shared.stopped():
            session = Session(*sock.accept())
            client_req = TcpClientRequestor(self.shared, session)
            client_req.start()
            client_res = TcpClientResponder(self.shared, session)
            client_res.start()
            self.processor.add_session(session)

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

class Stratum:

    def start(self, processor):
        shared = Shared()
        # Bind shared to processor since constructor is user defined
        processor.shared = shared
        processor.start()
        # Create various transports we need
        transports = TcpServer(shared, processor),
        for server in transports:
            server.start()
        while not shared.stopped():
            if raw_input() == "quit":
                shared.stop()
            time.sleep(1)

if __name__ == "__main__":
    processor = Processor()
    app = Stratum()
    app.start(processor)

