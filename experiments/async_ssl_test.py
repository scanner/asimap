#!/usr/bin/env python
#
# File: $Id$
#
"""
Test ssl with ayncore and asynchat
"""

import asynchat
import asyncore
import select
import socket
import ssl

# system imports
#
import sys


class EchoServer(asyncore.dispatcher):
    def __init__(self, certfile, keyfile=None):
        self.certfile = certfile
        self.keyfile = keyfile
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind(("0.0.0.0", 993))
        self.port = 993
        self.listen(5)

    def handle_accept(self):
        sock_obj, addr = self.accept()
        sys.stderr.write(" server:  new connection from %s:%s\n" % addr)
        try:
            ConnectionHandler(sock_obj, self.certfile, self.keyfile)
        except ssl.SSLError as e:
            sys.stderr.write(" server: connection failed: %s" % str(e))

    def handle_error(self):
        raise


class ConnectionHandler(asynchat.async_chat):
    def __init__(self, conn, certfile, keyfile):
        asynchat.async_chat.__init__(self, conn)
        self.socket = ssl.wrap_socket(
            conn,
            server_side=True,
            certfile=certfile,
            keyfile=keyfile,
            do_handshake_on_connect=False,
        )
        while True:
            try:
                self.socket.do_handshake()
                break
            except ssl.SSLError as err:
                if err.args[0] == ssl.SSL_ERROR_WANT_READ:
                    select.select([self.socket], [], [])
                elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
                    select.select([], [self.socket], [])
                else:
                    raise

        self.ibuffer = []
        self.set_terminator("\r\n")
        msg = "* OK [IMAP4rev1 IDLE ID UNSELECT UIDPLUS] IMAP4rev1 Service Ready\r\n"
        print("Writing: '%s'" % msg)
        self.push(msg)
        return

    ############################################################################
    #
    def collect_incoming_data(self, data):
        """
        Buffer data read from the connect for later processing.
        """
        self.ibuffer.append(data)
        return

    ##################################################################
    #
    def found_terminator(self):
        msg = "".join(self.ibuffer)
        self.ibuffer = []
        print("Msg: %s" % msg)
        tag, cmd = msg.split(" ")
        if cmd.lower() == "capability":
            out_msg = "%s CAPABILITY IMAP4rev1 IDLE ID UNSELECT UIDPLUS" % tag
        else:
            out_msg = "%s OK" % tag
        print("Writing: '%s'" % out_msg)
        self.push(out_msg + "\r\n")
        return

    def readable(self):
        if isinstance(self.socket, ssl.SSLSocket):
            while self.socket.pending() > 0:
                self.handle_read_event()
        return True

    # def handle_read(self):
    #     data = self.recv(1024)
    #     self.send(data.lower())

    def handle_close(self):
        self.close()
        sys.stderr.write(" server:  closed connection %s\n" % self.socket)

    def handle_error(self):
        raise


#############################################################################
#
def main():
    """ """
    if len(sys.argv) == 3:
        print("Using certfile %s, keyfile: %s" % (sys.argv[1], sys.argv[2]))
        s = EchoServer(certfile=sys.argv[1], keyfile=sys.argv[2])
    elif len(sys.argv) == 2:
        print("Using certfile %s" % sys.argv[1])
        s = EchoServer(certfile=sys.argv[1])

    print("Starting...")
    asyncore.loop()
    return


############################################################################
############################################################################
#
# Here is where it all starts
#
if __name__ == "__main__":
    main()
#
#
############################################################################
############################################################################
