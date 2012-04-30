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

import jsonrpclib
from jsonrpclib import Fault
from jsonrpclib.jsonrpc import USE_UNIX_SOCKETS
import SimpleXMLRPCServer
import SocketServer
import socket
import logging
import os
import types
import traceback
import sys, threading

try:
    import fcntl
except ImportError:
    # For Windows
    fcntl = None

import json


"""
sessions are identified with cookies
 - each session has a buffer of responses to requests


from the processor point of view: 
 - the user only defines process() ; the rest is session management.  thus sessions should not belong to processor

"""


from processor import random_string


def get_version(request):
    # must be a dict
    if 'jsonrpc' in request.keys():
        return 2.0
    if 'id' in request.keys():
        return 1.0
    return None
    
def validate_request(request):
    if type(request) is not types.DictType:
        fault = Fault(
            -32600, 'Request must be {}, not %s.' % type(request)
        )
        return fault
    rpcid = request.get('id', None)
    version = get_version(request)
    if not version:
        fault = Fault(-32600, 'Request %s invalid.' % request, rpcid=rpcid)
        return fault        
    request.setdefault('params', [])
    method = request.get('method', None)
    params = request.get('params')
    param_types = (types.ListType, types.DictType, types.TupleType)
    if not method or type(method) not in types.StringTypes or \
        type(params) not in param_types:
        fault = Fault(
            -32600, 'Invalid request parameters or method.', rpcid=rpcid
        )
        return fault
    return True

class StratumJSONRPCDispatcher(SimpleXMLRPCServer.SimpleXMLRPCDispatcher):

    def __init__(self, encoding=None):
        SimpleXMLRPCServer.SimpleXMLRPCDispatcher.__init__(self,
                                        allow_none=True,
                                        encoding=encoding)

    def _marshaled_dispatch(self, session_id, data, dispatch_method = None):
        response = None
        try:
            request = jsonrpclib.loads(data)
        except Exception, e:
            fault = Fault(-32700, 'Request %s invalid. (%s)' % (data, e))
            response = fault.response()
            return response

        responses = []
        if type(request) is not types.ListType:
            request = [ request ]

        for req_entry in request:
            result = validate_request(req_entry)
            if type(result) is Fault:
                responses.append(result.response())
                continue

            session = self.sessions.get(session_id)
            self.dispatcher.process(session, req_entry)
                
            if req_entry['method'] == 'server.stop':
                return json.dumps({'result':'ok'})

        r = self.poll_session(session_id)
        for item in r:
            responses.append(json.dumps(item))
            
        if len(responses) > 1:
            response = '[%s]' % ','.join(responses)
        elif len(responses) == 1:
            response = responses[0]
        else:
            response = ''

        return response



class StratumJSONRPCRequestHandler(
        SimpleXMLRPCServer.SimpleXMLRPCRequestHandler):
    
    def do_GET(self):
        if not self.is_rpc_path_valid():
            self.report_404()
            return
        try:
            session_id = None
            c = self.headers.get('cookie')
            if c:
                if c[0:8]=='SESSION=':
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
        if response == None:
            response = ''

        if session_id:
            self.send_header("Set-Cookie", "SESSION=%s"%session_id)

        self.send_header("Content-type", "application/json-rpc")
        self.send_header("Content-length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)
        self.wfile.flush()
        self.connection.shutdown(1)


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
                if c[0:8]=='SESSION=':
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
        if response == None:
            response = ''

        if session_id:
            self.send_header("Set-Cookie", "SESSION=%s"%session_id)

        self.send_header("Content-type", "application/json-rpc")
        self.send_header("Content-length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)
        self.wfile.flush()
        self.connection.shutdown(1)


class StratumJSONRPCServer(SocketServer.TCPServer, StratumJSONRPCDispatcher):

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
        # if python 2.5 and lower
        if vi[0] < 3 and vi[1] < 6:
            SocketServer.TCPServer.__init__(self, addr, requestHandler)
        else:
            SocketServer.TCPServer.__init__(self, addr, requestHandler,
                bind_and_activate)
        if fcntl is not None and hasattr(fcntl, 'FD_CLOEXEC'):
            flags = fcntl.fcntl(self.fileno(), fcntl.F_GETFD)
            flags |= fcntl.FD_CLOEXEC
            fcntl.fcntl(self.fileno(), fcntl.F_SETFD, flags)

        self.sessions = {}


    def create_session(self):
        session_id = random_string(10)
        self.sessions[session_id] = HttpSession(session_id)
        return session_id

    def poll_session(self, session_id):
        q = self.sessions[session_id].pending_responses
        responses = []
        while not q.empty():
            r = q.get()
            responses.append(r)
        #print "poll: %d responses"%len(responses)
        return responses


from processor import Session
import Queue

class HttpSession(Session):

    def __init__(self, session_id):
        Session.__init__(self)
        self.pending_responses = Queue.Queue()
        self.address = session_id
        self.name = "HTTP session"

    def send_response(self, response):
        raw_response = json.dumps(response)
        self.pending_responses.put(response)

class HttpServer(threading.Thread):
    def __init__(self, dispatcher, host, port):
        self.shared = dispatcher.shared
        self.dispatcher = dispatcher.request_dispatcher
        threading.Thread.__init__(self)
        self.daemon = True
        self.host = host
        self.port = port
        self.lock = threading.Lock()

    def run(self):
        # see http://code.google.com/p/jsonrpclib/
        from SocketServer import ThreadingMixIn
        class StratumThreadedJSONRPCServer(ThreadingMixIn, StratumJSONRPCServer): pass

        self.server = StratumThreadedJSONRPCServer(( self.host, self.port))
        self.server.dispatcher = self.dispatcher
        self.server.register_function(None, 'server.stop')
        self.server.register_function(None, 'server.info')

        print "HTTP server started."
        self.server.serve_forever()

