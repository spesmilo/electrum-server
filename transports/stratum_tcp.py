import json
import Queue as queue
import socket
import threading
import time
import traceback, sys

from processor import Session, Dispatcher
from utils import print_log


class TcpSession(Session):

    def __init__(self, dispatcher, connection, address, use_ssl, ssl_certfile, ssl_keyfile):
        Session.__init__(self, dispatcher)
        self.use_ssl = use_ssl
        if use_ssl:
            import ssl
            self._connection = ssl.wrap_socket(
                connection,
                server_side=True,
                certfile=ssl_certfile,
                keyfile=ssl_keyfile,
                ssl_version=ssl.PROTOCOL_SSLv23,
                do_handshake_on_connect=False)
        else:
            self._connection = connection

        self.address = address[0] + ":%d"%address[1]
        self.name = "TCP " if not use_ssl else "SSL "
        self.timeout = 1000
        self.response_queue = queue.Queue()
        self.dispatcher.add_session(self)

    def do_handshake(self):
        if self.use_ssl:
            self._connection.do_handshake()

    def connection(self):
        if self.stopped():
            raise Exception("Session was stopped")
        else:
            return self._connection

    def shutdown(self):
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except:
            # print_log("problem shutting down", self.address)
            # traceback.print_exc(file=sys.stdout)
            pass

        self._connection.close()

    def send_response(self, response):
        self.response_queue.put(response)


class TcpClientResponder(threading.Thread):

    def __init__(self, session):
        self.session = session
        threading.Thread.__init__(self)

    def run(self):
        while not self.session.stopped():
            try:
                response = self.session.response_queue.get(timeout=10)
            except queue.Empty:
                continue
            data = json.dumps(response) + "\n"
            try:
                while data:
                    l = self.session.connection().send(data)
                    data = data[l:]
            except:
                self.session.stop()



class TcpClientRequestor(threading.Thread):

    def __init__(self, dispatcher, session):
        self.shared = dispatcher.shared
        self.dispatcher = dispatcher
        self.message = ""
        self.session = session
        threading.Thread.__init__(self)

    def run(self):
        try:
            self.session.do_handshake()
        except:
            self.session.stop()
            return

        while not self.shared.stopped():

            data = self.receive()
            if not data:
                self.session.stop()
                break

            self.message += data
            self.session.time = time.time()

            while self.parse():
                pass


    def receive(self):
        try:
            return self.session.connection().recv(2048)
        except:
            return ''

    def parse(self):
        raw_buffer = self.message.find('\n')
        if raw_buffer == -1:
            return False

        raw_command = self.message[0:raw_buffer].strip()
        self.message = self.message[raw_buffer + 1:]
        if raw_command == 'quit':
            self.session.stop()
            return False

        try:
            command = json.loads(raw_command)
        except:
            self.dispatcher.push_response(self.session, {"error": "bad JSON", "request": raw_command})
            return True

        try:
            # Try to load vital fields, and return an error if
            # unsuccessful.
            message_id = command['id']
            method = command['method']
        except KeyError:
            # Return an error JSON in response.
            self.dispatcher.push_response(self.session, {"error": "syntax error", "request": raw_command})
        else:
            self.dispatcher.push_request(self.session, command)
            # sleep a bit to prevent a single session from DOSing the queue
            time.sleep(0.01)

        return True


class TcpServer(threading.Thread):

    def __init__(self, dispatcher, host, port, use_ssl, ssl_certfile, ssl_keyfile):
        self.shared = dispatcher.shared
        self.dispatcher = dispatcher.request_dispatcher
        threading.Thread.__init__(self)
        self.daemon = True
        self.host = host
        self.port = port
        self.lock = threading.Lock()
        self.use_ssl = use_ssl
        self.ssl_keyfile = ssl_keyfile
        self.ssl_certfile = ssl_certfile

    def run(self):
        print_log( ("SSL" if self.use_ssl else "TCP") + " server started on port %d"%self.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(5)

        while not self.shared.stopped():

            #if self.use_ssl: print_log("SSL: socket listening")
            try:
                connection, address = sock.accept()
            except:
                traceback.print_exc(file=sys.stdout)
                time.sleep(0.1)
                continue

            #if self.use_ssl: print_log("SSL: new session", address)
            try:
                session = TcpSession(self.dispatcher, connection, address, use_ssl=self.use_ssl, ssl_certfile=self.ssl_certfile, ssl_keyfile=self.ssl_keyfile)
            except BaseException, e:
                error = str(e)
                print_log("cannot start TCP session", error, address)
                connection.close()
                time.sleep(0.1)
                continue

            client_req = TcpClientRequestor(self.dispatcher, session)
            client_req.start()
            responder = TcpClientResponder(session)
            responder.start()
