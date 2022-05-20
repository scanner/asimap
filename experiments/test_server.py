#!/usr/bin/env python
#
# File: $Id$
#
"""
A test asynccore based IMAP main server.
"""

# system imports
#
import asyncore
import asynchat
import logging
import socket
import ssl
import os

# By default every file is its own logging module. Kind of simplistic
# but it works for now.
#
log      = logging.getLogger("asimap.%s" % __name__)

BACKLOG  = 5
SIZE     = 4096

##################################################################
##################################################################
#
class IMAPServer(asyncore.dispatcher):
    """
    The IMAPServer dispatcher. This really just listens for connections and
    when we accept one we hand it off to an IMAPClientHandler to deal with.
    """

    ##################################################################
    #
    def __init__(self, options):
        """
        Setup our dispatcher.. listen on the port we are supposed to accept
        connections on. When something connects to it create an
        IMAPClientHandler and pass it the socket.

        Arguments:
        - `options` : The options set on the command line
        """
        self._options = options

        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind((options.interface, options.port))
        self.listen(BACKLOG)
        return

    ##################################################################
    #
    def handle_accept(self):
        """
        """

        pair = self.accept()
        if pair is not None:
            sock,addr = pair
            print "Incoming connection from %s" % repr(pair)
            handler = IMAPClient(sock, self._options)

##################################################################
##################################################################
#
class IMAPClientHandler(asyncore.dispatcher_with_send):
    """
    A handler for a connection with an IMAP Client.

    If the IMAP Client has not been authenticated and associated with a
    subprocess then we parse a limited set IMAP messages from this client --
    enough to require it to authenticate.

    If we have an associated subprocess then we gather up the data for a
    message and pass it on as a single message to the subprocess letting it
    parse the message and give us a response to send back to the IMAP client
    that is connected to us.
    """

    ##################################################################
    #
    def __init__(self, ):
        """
        """
