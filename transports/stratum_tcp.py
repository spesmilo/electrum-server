import json
import socket
import threading
import time
import Queue as queue

from processor import Session, Dispatcher, timestr

class TcpSession(Session):

    def __init__(self, connection, address, use_ssl, ssl_certfile, ssl_keyfile):
        Session.__init__(self)
        print connection, address, use_ssl
        if use_ssl:
            import ssl
            self._connection = ssl.wrap_socket(
                connection,
                server_side=True,
                certfile=ssl_certfile,
                keyfile=ssl_keyfile,
                ssl_version=ssl.PROTOCOL_TLSv1)
        else:
            self._connection = connection

        self.address = address[0]
        self.name = "TCP"

    def connection(self):
        if self.stopped():
            raise Exception("Session was stopped")
        else:
            return self._connection

    def stop(self):
        self._connection.close()
        #print "Terminating connection:", self.address
        with self.lock:
            self._stopped = True

    def send_response(self, response):
        data = json.dumps(response) + "\n"
        # Possible race condition here by having session
        # close connection?
        # I assume Python connections are thread safe interfaces
        try:
            connection = self.connection()
            while data:
                l = connection.send(data)
                data = data[l:]
        except:
            self.stop()



class TcpClientRequestor(threading.Thread):

    def __init__(self, dispatcher, session):
        self.shared = dispatcher.shared
        self.dispatcher = dispatcher
        self.message = ""
        self.session = session
        threading.Thread.__init__(self)

    def run(self):
        while not self.shared.stopped():
            if not self.update():
                break

            self.session.time = time.time()

            while self.parse():
                pass

    def update(self):
        data = self.receive()
        if not data:
            # close_session
            self.session.stop()
            return False

        self.message += data
        return True

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
            self.dispatcher.push_response({"error": "bad JSON", "request": raw_command})
            return True

        try:
            # Try to load vital fields, and return an error if
            # unsuccessful.
            message_id = command['id']
            method = command['method']
        except KeyError:
            # Return an error JSON in response.
            self.dispatcher.push_response({"error": "syntax error", "request": raw_command})
        else:
            self.dispatcher.push_request(self.session,command)

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
        print "TCP server started.", self.use_ssl
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(1)
        while not self.shared.stopped():
            session = TcpSession(*sock.accept(), use_ssl=self.use_ssl, ssl_certfile=self.ssl_certfile, ssl_keyfile=self.ssl_keyfile)
            self.dispatcher.add_session(session)
            self.dispatcher.collect_garbage()
            client_req = TcpClientRequestor(self.dispatcher, session)
            client_req.start()

