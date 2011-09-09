#!/usr/bin/env python
#
# File: $Id$
#
"""
The heart of the asimap server process to handle a single user's
mailbox for multiple IMAP clients.

We get all of our data relayed to us from the main asimapd server via
connections on localhost.
"""

# system imports
#
import asyncore
import asynchat
import logging
import os
import pwd

# By default every file is its own logging module. Kind of simplistic
# but it works for now.
#
log      = logging.getLogger("asimap.%s" % __name__)

BACKLOG  = 5

##################################################################
##################################################################
#
class IMAPUserClientHandler(asynchat.async_chat):
    """
    A handler for a connection with an IMAP client (all of the data is
    relayed through the asimapd.)

    This deals with the part of the IMAP client protocol for when the
    client is authenticated.

    We will be receiving IMAP messages from clients relayed through
    the asimapd. This means the protocol for reading these messages is
    a little bit different.

    We no longer need to watch and parse continuation messages. The
    asimapd server will collect a full message and pass it on to
    us. We first get a length terminated by a newline, and then that
    many characters (for the rest of the message.)
    """

    LINE_TERMINATOR     = "\n"

    ##################################################################
    #
    def __init__(self, sock, options):
        """
        """
        asynchat.async_chat.__init__(self, sock = sock)

        self.reading_message = False
        self.ibuffer = []
        self.set_terminator(self.LINE_TERMINATOR)

    ############################################################################
    #
    def collect_incoming_data(self, data):
        """
        Buffer data read from the connect for later processing.
        """
        log.debug("collect_incoming_data: [%s]" % data)
        self.ibuffer.append(data)
        return

    ##################################################################
    #
    def found_terminator(self):
        """
        We have come across a message terminator from the IMAP client talking
        to us.

        This is invoked in two different states:

        1) we have hit LINE_TERMINATOR and we were waiting for it.  At this
           point the buffer should contain an integer as an ascii string. This
           integer is the length of the actual message.

        2) We are reading the message itself.. we read the appropriate number
           of bytes from the channel.

        If (2) then we exit the state where we are reading the IMAP message
        from the channel and set the terminator back to LINE_TERMINATOR so that
        we can read the rest of the message from the IMAP client.
        """
        log.debug("found_terminator")

        if not self.reading_message:
            # We have hit our line terminator.. we should have an ascii
            # representation of an int in our buffer.. read that to determine
            # how many characters the actual IMAP message we need to read is.
            #
            msg_length = int("".join(self.ibuffer).strip())
            self.ibuffer = []
            log.debug("Read IMAP message length indicator: %d" % msg_length)
            self.set_terminator(msg_length)
            return

        # If we were reading a full IMAP message, then we switch back to
        # reading lines.
        #
        imap_msg = "".join(self.ibuffer)
        self.ibuffer = []
        self.reading_message = False
        self.set_terminator(self.LINE_TERMINATOR)

        log.debug("Got complete IMAP message: %s" % imap_msg)
        # ServerIMAPClient.message(imap_msg)
        return

##################################################################
##################################################################
#
class IMAPUserServer(asyncore.dispatcher):
    """
    Listen on a port on localhost for connections from the
    asimapd. When we get one create an IMAPUserClientHandler object that
    gets the new connection (and handles all further IMAP related
    communications with the client.)
    """

    ##################################################################
    #
    def __init__(self, options):
        """
        Setup our dispatcher.. listen on a port we are supposed to accept
        connections on. When something connects to it create an
        IMAPClientHandler and pass it the socket.

        Arguments:
        - `options` : The options set on the command line
        """
        
        self.options = options

        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind(("127.0.0.1", 0))
        self.listen(BACKLOG)
        return

    ##################################################################
    #
    def handle_accept(self):
        """
        A client has connected to us. Create the IMAPClientHandler object to
        handle that client and let it deal with it.
        """

        pair = self.accept()
        if pair is not None:
            sock,addr = pair
            print "Incoming connection from %s" % repr(pair)
            handler = IMAPUserClientHandler(sock, self._options)
        
