class EchoServer(asyncore.dispatcher):
    class ConnectionHandler(asyncore.dispatcher_with_send):
        def __init__(self, conn, certfile):
            asyncore.dispatcher_with_send.__init__(self, conn)
            self.socket = ssl.wrap_socket(
                conn,
                server_side=True,
                certfile=certfile,
                do_handshake_on_connect=True,
            )

        def readable(self):
            if isinstance(self.socket, ssl.SSLSocket):
                while self.socket.pending() > 0:
                    self.handle_read_event()
            return True

        def handle_read(self):
            data = self.recv(1024)
            self.send(data.lower())

        def handle_close(self):
            self.close()
            if test_support.verbose:
                sys.stdout.write(
                    " server:  closed connection %s\n" % self.socket
                )

        def handle_error(self):
            raise

    def __init__(self, certfile):
        self.certfile = certfile
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.port = test_support.bind_port(self.socket)
        self.listen(5)

    def handle_accept(self):
        sock_obj, addr = self.accept()
        if test_support.verbose:
            sys.stdout.write(" server:  new connection from %s:%s\n" % addr)
        self.ConnectionHandler(sock_obj, self.certfile)

    def handle_error(self):
        raise


import asynchat
import errno
import socket
import ssl


class async_chat_ssl(asynchat.async_chat):
    """Asynchronous connection with SSL support."""

    def connect(self, host, use_ssl=False):
        self.use_ssl = use_ssl
        if use_ssl:
            self.send = self._ssl_send
            self.recv = self._ssl_recv
        asynchat.async_chat.connect(self, host)

    def handle_connect(self):
        """Initializes SSL support after the connection has been made."""
        if self.use_ssl:
            self.ssl = ssl.wrap_socket(self.socket)
            self.set_socket(self.ssl)

    def _ssl_send(self, data):
        """Replacement for self.send() during SSL connections."""
        try:
            result = self.write(data)
            return result
        except ssl.SSLError as why:
            if why[0] in (asyncore.EWOULDBLOCK, errno.ESRCH):
                return 0
            else:
                raise ssl.SSLError(why)
            return 0

    def _ssl_recv(self, buffer_size):
        """Replacement for self.recv() during SSL connections."""
        try:
            data = self.read(buffer_size)
            if not data:
                self.handle_close()
                return ""
            return data
        except ssl.SSLError as why:
            if why[0] in (
                asyncore.ECONNRESET,
                asyncore.ENOTCONN,
                asyncore.ESHUTDOWN,
            ):
                self.handle_close()
                return ""
            elif why[0] == errno.ENOENT:
                # Required in order to keep it non-blocking
                return ""
            else:
                raise
