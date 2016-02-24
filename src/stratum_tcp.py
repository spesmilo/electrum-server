#!/usr/bin/env python
# Copyright(C) 2011-2016 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import json
import Queue as queue
import socket
import select
import threading
import time
import sys

from processor import Session, Dispatcher
from utils import print_log, logger


READ_ONLY = select.POLLIN | select.POLLPRI | select.POLLHUP | select.POLLERR
READ_WRITE = READ_ONLY | select.POLLOUT
WRITE_ONLY = select.POLLOUT
TIMEOUT = 100

import ssl

class TcpSession(Session):

    def __init__(self, dispatcher, connection, address, use_ssl, ssl_certfile, ssl_keyfile):
        Session.__init__(self, dispatcher)
        self.use_ssl = use_ssl
        self.raw_connection = connection
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
        self.dispatcher.add_session(self)
        self.response_queue = queue.Queue()
        self.message = ''
        self.retry_msg = ''
        self.handshake = not self.use_ssl
        self.mode = None

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
            pass
        self._connection.close()

    def send_response(self, response):
        try:
            msg = json.dumps(response) + '\n'
        except BaseException as e:
            logger.error('send_response:' + str(e))
            return
        self.response_queue.put(msg)

    def parse_message(self):
        message = self.message
        self.time = time.time()
        raw_buffer = message.find('\n')
        if raw_buffer == -1:
            return False
        raw_command = message[0:raw_buffer].strip()
        self.message = message[raw_buffer + 1:]
        return raw_command





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

        self.fd_to_session = {}
        self.buffer_size = 4096





    def handle_command(self, raw_command, session):
        try:
            command = json.loads(raw_command)
        except:
            session.send_response({"error": "bad JSON"})
            return True
        try:
            # Try to load vital fields, and return an error if
            # unsuccessful.
            message_id = command['id']
            method = command['method']
        except:
            # Return an error JSON in response.
            session.send_response({"error": "syntax error", "request": raw_command})
        else:
            #print_log("new request", command)
            self.dispatcher.push_request(session, command)



    def run(self):

        for res in socket.getaddrinfo(self.host, self.port, socket.AF_UNSPEC, socket.SOCK_STREAM):
            af, socktype, proto, cannonname, sa = res
            try:
                sock = socket.socket(af, socktype, proto)
                sock.setblocking(0)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except socket.error:
                sock = None
                continue
            try:
                sock.bind(sa)
                sock.listen(5)
            except socket.error:
                sock.close()
                sock = None
                continue
            break
        host = sa[0]
        if af == socket.AF_INET6:
            host = "[%s]" % host
        if sock is None:
            print_log( "could not open " + ("SSL" if self.use_ssl else "TCP") + " socket on %s:%d" % (host, self.port))
            return
        print_log( ("SSL" if self.use_ssl else "TCP") + " server started on %s:%d" % (host, self.port))

        sock_fd = sock.fileno()
        poller = select.poll()
        poller.register(sock)

        def stop_session(fd):
            try:
                # unregister before we close s 
                poller.unregister(fd)
                session = self.fd_to_session.pop(fd)
                # this will close the socket
                session.stop()
            except BaseException as e:
                logger.error('stop_session error:' + str(e))

        def check_do_handshake(session):
            if session.handshake:
                return
            try:
                session._connection.do_handshake()
            except ssl.SSLError as err:
                if err.args[0] == ssl.SSL_ERROR_WANT_READ:
                    return
                elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
                    poller.modify(session.raw_connection, READ_WRITE)
                    return
                else:
                    raise BaseException(str(err))
            poller.modify(session.raw_connection, READ_ONLY)
            session.handshake = True

        redo = []

        while not self.shared.stopped():

            if self.shared.paused():
                sessions = self.fd_to_session.keys()
                if sessions:
                    logger.info("closing %d sessions"%len(sessions))
                for fd in sessions:
                    stop_session(fd)
                time.sleep(1)
                continue

            if redo:
                events = redo
                redo = []
            else:
                now = time.time()
                for fd, session in self.fd_to_session.items():
                    # Anti-DOS: wait 0.01 second between requests
                    if now - session.time > 0.01 and session.message:
                        cmd = session.parse_message()
                        if not cmd:
                            break
                        if cmd == 'quit':
                            data = False
                            break
                        session.time = now
                        self.handle_command(cmd, session)

                    # Anti-DOS: Stop reading if the session does not read responses 
                    if session.response_queue.empty():
                        mode = READ_ONLY
                    elif session.response_queue.qsize() < 200:
                        mode = READ_WRITE
                    else:
                        mode = WRITE_ONLY
                    if mode != session.mode:
                        poller.modify(session.raw_connection, mode)
                        session.mode = mode

                    # Collect garbage
                    if now - session.time > session.timeout:
                        stop_session(fd)

                events = poller.poll(TIMEOUT)

            for fd, flag in events:
                # open new session
                if fd == sock_fd:
                    if flag & (select.POLLIN | select.POLLPRI):
                        try:
                            connection, address = sock.accept()
                            session = TcpSession(self.dispatcher, connection, address, 
                                                 use_ssl=self.use_ssl, ssl_certfile=self.ssl_certfile, ssl_keyfile=self.ssl_keyfile)
                        except BaseException as e:
                            logger.error("cannot start TCP session" + str(e) + ' ' + repr(address))
                            connection.close()
                            continue
                        connection = session._connection
                        connection.setblocking(False)
                        self.fd_to_session[connection.fileno()] = session
                        poller.register(connection, READ_ONLY)
                    continue
                # existing session
                session = self.fd_to_session[fd]
                s = session._connection
                # non-blocking handshake
                try:
                    check_do_handshake(session)
                except BaseException as e:
                    #logger.error('handshake failure:' + str(e) + ' ' + repr(session.address))
                    stop_session(fd)
                    continue
                # anti DOS
                now = time.time()
                if now - session.time < 0.01:
                    continue
                # Read input messages.
                if flag & (select.POLLIN | select.POLLPRI):
                    try:
                        data = s.recv(self.buffer_size)
                    except ssl.SSLError as x:
                        if x.args[0] == ssl.SSL_ERROR_WANT_READ: 
                            pass
                        elif x.args[0] == ssl.SSL_ERROR_SSL: 
                            pass
                        else:
                            logger.error('SSL recv error:'+ repr(x))
                        continue 
                    except socket.error as x:
                        if x.args[0] != 104:
                            logger.error('recv error: ' + repr(x) +' %d'%fd)
                        stop_session(fd)
                        continue
                    except ValueError as e:
                        logger.error('recv error: ' + str(e) +' %d'%fd)
                        stop_session(fd)
                        continue
                    if data:
                        session.message += data
                        if len(data) == self.buffer_size:
                            redo.append((fd, flag))
                        
                    if not data:
                        stop_session(fd)
                        continue

                elif flag & select.POLLHUP:
                    print_log('client hung up', session.address)
                    stop_session(fd)

                elif flag & select.POLLOUT:
                    # Socket is ready to send data, if there is any to send.
                    if session.retry_msg:
                        next_msg = session.retry_msg
                    else:
                        try:
                            next_msg = session.response_queue.get_nowait()
                        except queue.Empty:
                            continue
                    try:
                        sent = s.send(next_msg)
                    except socket.error as x:
                        logger.error("send error:" + str(x))
                        stop_session(fd)
                        continue
                    session.retry_msg = next_msg[sent:]

                elif flag & select.POLLERR:
                    print_log('handling exceptional condition for', session.address)
                    stop_session(fd)

                elif flag & select.POLLNVAL:
                    print_log('invalid request', session.address)
                    stop_session(fd)


        print_log('TCP thread terminating', self.shared.stopped())
