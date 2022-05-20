#!/usr/bin/env python
#
# File: $Id$
#
"""
a test echo server that uses SSL.
"""

# system imports
#
import ssl
import asyncore
import socket

class EchoServer(asyncore.dispatcher):

    def __init__(self, certfile):
        self.certfile = certfile
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.port = test_support.bind_port(self.socket)
        self.listen(5)

    def handle_accept(self):
        sock_obj, addr = self.accept()
        if test_support.verbose:
            sys.stdout.write(" server:  new connection from %s:%s\n" %addr)
        self.ConnectionHandler(sock_obj, self.certfile)

    def handle_error(self):
        raise

class ConnectionHandler(asyncore.dispatcher_with_send):

    def __init__(self, conn, certfile):
        asyncore.dispatcher_with_send.__init__(self, conn)
        self.socket = ssl.wrap_socket(conn, server_side=True,
                                      certfile=certfile,
                                      do_handshake_on_connect=False)
        self._ssl_accepting = True

    def readable(self):
        if isinstance(self.socket, ssl.SSLSocket):
            while self.socket.pending() > 0:
                self.handle_read_event()
        return True

    def _do_ssl_handshake(self):
        try:
            self.socket.do_handshake()
        except ssl.SSLError, err:
            if err.args[0] in (ssl.SSL_ERROR_WANT_READ,
                               ssl.SSL_ERROR_WANT_WRITE):
                return
            elif err.args[0] == ssl.SSL_ERROR_EOF:
                return self.handle_close()
            raise
        except socket.error, err:
            if err.args[0] == errno.ECONNABORTED:
                return self.handle_close()
        else:
            self._ssl_accepting = False

    def handle_read(self):
        if self._ssl_accepting:
            self._do_ssl_handshake()
        else:
            data = self.recv(1024)
            if data and data.strip() != 'over':
                self.send(data.lower())

    def handle_close(self):
        self.close()
        if test_support.verbose:
            sys.stdout.write(" server:  closed connection %s\n" % self.socket)

    def handle_error(self):
        raise
