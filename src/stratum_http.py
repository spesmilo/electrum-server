#!/usr/bin/env python
# Copyright(C) 2012 thomasv@gitorious

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/agpl.html>.
"""
sessions are identified with cookies
 - each session has a buffer of responses to requests


from the processor point of view:
 - the user only defines process() ; the rest is session management.  thus sessions should not belong to processor

"""
import json
import logging
import os
import Queue
import SimpleXMLRPCServer
import socket
import SocketServer
import sys
import time
import threading
import traceback
import types

import jsonrpclib
from jsonrpclib import Fault
from jsonrpclib.jsonrpc import USE_UNIX_SOCKETS

try:
    import fcntl
except ImportError:
    # For Windows
    fcntl = None


from processor import Session
from utils import random_string, print_log


def get_version(request):
    # must be a dict
    if 'jsonrpc' in request.keys():
        return 2.0
    if 'id' in request.keys():
        return 1.0
    return None


def validate_request(request):
    if not isinstance(request, types.DictType):
        return Fault(-32600, 'Request must be {}, not %s.' % type(request))
    rpcid = request.get('id', None)
    version = get_version(request)
    if not version:
        return Fault(-32600, 'Request %s invalid.' % request, rpcid=rpcid)
    request.setdefault('params', [])
    method = request.get('method', None)
    params = request.get('params')
    param_types = (types.ListType, types.DictType, types.TupleType)
    if not method or type(method) not in types.StringTypes or type(params) not in param_types:
        return Fault(-32600, 'Invalid request parameters or method.', rpcid=rpcid)
    return True


class StratumJSONRPCDispatcher(SimpleXMLRPCServer.SimpleXMLRPCDispatcher):

    def __init__(self, encoding=None):
        # todo: use super
        SimpleXMLRPCServer.SimpleXMLRPCDispatcher.__init__(self, allow_none=True, encoding=encoding)

    def _marshaled_dispatch(self, session_id, data, dispatch_method=None):
        response = None
        try:
            request = jsonrpclib.loads(data)
        except Exception, e:
            fault = Fault(-32700, 'Request %s invalid. (%s)' % (data, e))
            response = fault.response()
            return response

        session = self.dispatcher.get_session_by_address(session_id)
        if not session:
            return 'Error: session not found'
        session.time = time.time()

        responses = []
        if not isinstance(request, types.ListType):
            request = [request]

        for req_entry in request:
            result = validate_request(req_entry)
            if type(result) is Fault:
                responses.append(result.response())
                continue

            self.dispatcher.do_dispatch(session, req_entry)

            if req_entry['method'] == 'server.stop':
                return json.dumps({'result': 'ok'})

        r = self.poll_session(session)
        for item in r:
            responses.append(json.dumps(item))

        if len(responses) > 1:
            response = '[%s]' % ','.join(responses)
        elif len(responses) == 1:
            response = responses[0]
        else:
            response = ''

        return response

    def create_session(self):
        session_id = random_string(20)
        session = HttpSession(self.dispatcher, session_id)
        return session_id

    def poll_session(self, session):
        q = session.pending_responses
        responses = []
        while not q.empty():
            r = q.get()
            responses.append(r)
        #print "poll: %d responses"%len(responses)
        return responses


class StratumJSONRPCRequestHandler(SimpleXMLRPCServer.SimpleXMLRPCRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Allow', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Cache-Control, Content-Language, Content-Type, Expires, Last-Modified, Pragma, Accept-Language, Accept, Origin')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        if not self.is_rpc_path_valid():
            self.report_404()
            return
        try:
            session_id = None
            c = self.headers.get('cookie')
            if c:
                if c[0:8] == 'SESSION=':
                    #print "found cookie", c[8:]
                    session_id = c[8:]

            if session_id is None:
                session_id = self.server.create_session()
                #print "setting cookie", session_id

            data = json.dumps([])
            response = self.server._marshaled_dispatch(session_id, data)
            self.send_response(200)
        except Exception, e:
            self.send_response(500)
            err_lines = traceback.format_exc().splitlines()
            trace_string = '%s | %s' % (err_lines[-3], err_lines[-1])
            fault = jsonrpclib.Fault(-32603, 'Server error: %s' % trace_string)
            response = fault.response()
            print "500", trace_string
        if response is None:
            response = ''

        if session_id:
            self.send_header("Set-Cookie", "SESSION=%s" % session_id)

        self.send_header("Content-type", "application/json-rpc")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)
        self.wfile.flush()
        self.shutdown_connection()

    def do_POST(self):
        if not self.is_rpc_path_valid():
            self.report_404()
            return
        try:
            max_chunk_size = 10*1024*1024
            size_remaining = int(self.headers["content-length"])
            L = []
            while size_remaining:
                chunk_size = min(size_remaining, max_chunk_size)
                L.append(self.rfile.read(chunk_size))
                size_remaining -= len(L[-1])
            data = ''.join(L)

            session_id = None
            c = self.headers.get('cookie')
            if c:
                if c[0:8] == 'SESSION=':
                    #print "found cookie", c[8:]
                    session_id = c[8:]

            if session_id is None:
                session_id = self.server.create_session()
                #print "setting cookie", session_id

            response = self.server._marshaled_dispatch(session_id, data)
            self.send_response(200)
        except Exception, e:
            self.send_response(500)
            err_lines = traceback.format_exc().splitlines()
            trace_string = '%s | %s' % (err_lines[-3], err_lines[-1])
            fault = jsonrpclib.Fault(-32603, 'Server error: %s' % trace_string)
            response = fault.response()
            print "500", trace_string
        if response is None:
            response = ''

        if session_id:
            self.send_header("Set-Cookie", "SESSION=%s" % session_id)

        self.send_header("Content-type", "application/json-rpc")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)
        self.wfile.flush()
        self.shutdown_connection()

    def shutdown_connection(self):
        self.connection.shutdown(1)


class SSLRequestHandler(StratumJSONRPCRequestHandler):
    def setup(self):
        self.connection = self.request
        self.rfile = socket._fileobject(self.request, "rb", self.rbufsize)
        self.wfile = socket._fileobject(self.request, "wb", self.wbufsize)

    def shutdown_connection(self):
        self.connection.shutdown()


class SSLTCPServer(SocketServer.TCPServer):
    def __init__(self, server_address, certfile, keyfile, RequestHandlerClass, bind_and_activate=True):
        from OpenSSL import SSL
        SocketServer.BaseServer.__init__(self, server_address, RequestHandlerClass)
        ctx = SSL.Context(SSL.SSLv23_METHOD)
        ctx.use_privatekey_file(keyfile)
        ctx.use_certificate_file(certfile)
        self.socket = SSL.Connection(ctx, socket.socket(self.address_family, self.socket_type))
        if bind_and_activate:
            self.server_bind()
            self.server_activate()

    def shutdown_request(self, request):
        #request.shutdown()
        pass


class StratumHTTPServer(SocketServer.TCPServer, StratumJSONRPCDispatcher):

    allow_reuse_address = True

    def __init__(self, addr, requestHandler=StratumJSONRPCRequestHandler,
                 logRequests=False, encoding=None, bind_and_activate=True,
                 address_family=socket.AF_INET):
        self.logRequests = logRequests
        StratumJSONRPCDispatcher.__init__(self, encoding)
        # TCPServer.__init__ has an extra parameter on 2.6+, so
        # check Python version and decide on how to call it
        vi = sys.version_info
        self.address_family = address_family
        if USE_UNIX_SOCKETS and address_family == socket.AF_UNIX:
            # Unix sockets can't be bound if they already exist in the
            # filesystem. The convention of e.g. X11 is to unlink
            # before binding again.
            if os.path.exists(addr):
                try:
                    os.unlink(addr)
                except OSError:
                    logging.warning("Could not unlink socket %s", addr)

        SocketServer.TCPServer.__init__(self, addr, requestHandler, bind_and_activate)

        if fcntl is not None and hasattr(fcntl, 'FD_CLOEXEC'):
            flags = fcntl.fcntl(self.fileno(), fcntl.F_GETFD)
            flags |= fcntl.FD_CLOEXEC
            fcntl.fcntl(self.fileno(), fcntl.F_SETFD, flags)


class StratumHTTPSSLServer(SSLTCPServer, StratumJSONRPCDispatcher):

    allow_reuse_address = True

    def __init__(self, addr, certfile, keyfile,
                 requestHandler=SSLRequestHandler,
                 logRequests=False, encoding=None, bind_and_activate=True,
                 address_family=socket.AF_INET):

        self.logRequests = logRequests
        StratumJSONRPCDispatcher.__init__(self, encoding)
        # TCPServer.__init__ has an extra parameter on 2.6+, so
        # check Python version and decide on how to call it
        vi = sys.version_info
        self.address_family = address_family
        if USE_UNIX_SOCKETS and address_family == socket.AF_UNIX:
            # Unix sockets can't be bound if they already exist in the
            # filesystem. The convention of e.g. X11 is to unlink
            # before binding again.
            if os.path.exists(addr):
                try:
                    os.unlink(addr)
                except OSError:
                    logging.warning("Could not unlink socket %s", addr)

        SSLTCPServer.__init__(self, addr, certfile, keyfile, requestHandler, bind_and_activate)

        if fcntl is not None and hasattr(fcntl, 'FD_CLOEXEC'):
            flags = fcntl.fcntl(self.fileno(), fcntl.F_GETFD)
            flags |= fcntl.FD_CLOEXEC
            fcntl.fcntl(self.fileno(), fcntl.F_SETFD, flags)


class HttpSession(Session):

    def __init__(self, dispatcher, session_id):
        Session.__init__(self, dispatcher)
        self.pending_responses = Queue.Queue()
        self.address = session_id
        self.name = "HTTP"
        self.timeout = 60
        self.dispatcher.add_session(self)

    def send_response(self, response):
        raw_response = json.dumps(response)
        self.pending_responses.put(response)



class HttpServer(threading.Thread):
    def __init__(self, dispatcher, host, port, use_ssl, certfile, keyfile):
        self.shared = dispatcher.shared
        self.dispatcher = dispatcher.request_dispatcher
        threading.Thread.__init__(self)
        self.daemon = True
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.certfile = certfile
        self.keyfile = keyfile
        self.lock = threading.Lock()

    def run(self):
        # see http://code.google.com/p/jsonrpclib/
        from SocketServer import ThreadingMixIn
        if self.use_ssl:
            class StratumThreadedServer(ThreadingMixIn, StratumHTTPSSLServer):
                pass
            self.server = StratumThreadedServer((self.host, self.port), self.certfile, self.keyfile)
            print_log("HTTPS server started.")
        else:
            class StratumThreadedServer(ThreadingMixIn, StratumHTTPServer):
                pass
            self.server = StratumThreadedServer((self.host, self.port))
            print_log("HTTP server started.")

        self.server.dispatcher = self.dispatcher
        self.server.register_function(None, 'server.stop')
        self.server.register_function(None, 'server.info')

        self.server.serve_forever()
