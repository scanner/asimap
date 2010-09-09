#!/usr/bin/env python
#
# File: $Id: server.py 1931 2009-05-05 17:05:26Z scanner $
#
"""
This is the heart of the main server. This is what accepts incoming
network connections, authenticates users, spawns userserver's, and
relays IMAP messages between an IMAP client and a userserver.

The basic example for how to set up an asynchat server was cribbed from:

  http://parijatmishra.wordpress.com/2008/01/06/pythons-asynchat-module/

  Parijat Mishra's weblog.
"""

# system imports
#
import asyncore
import asynchat
import multiprocess
import logging
import socket
import os

# By default every file is its own logging module. Kind of simplistic
# but it works for now.
#
log      = logging.getLogger("asimap.%s" % __name__)

BACKLOG  = 5
SIZE     = 1024

# The Dict of active mail stores. The key the '<username>:<mailstore location>'
#
active_mailstores = { }

##################################################################
##################################################################
#
class MailStoreClient(asynchat.async_chat):
    """
    This is the client that talks to a separate process that controls
    all the accesses to a specific user's mail store.

    We override a lot of the methods of the async_chat class because
    we are not reading from a traditional socket but a multiprocess
    Connection.
    """

    ##################################################################
    #
    def __init__(self, location, user_name):
        """
        We need the location of the user's mail store and the user
        name of the user that store belongs to.

        In a subprocess we will switch to that user and open the
        mail store indicated by that location.

        Arguments:
        - `location`: The location (filename) of the mailstore
        - `user_name`: The user to switch to.
        """

        self.user_name = user_name
        self.location = location

        # The unique name for this MailStoreClient is the concatenation
        # of the username and the location of the mail store.
        #
        self.name = self.username + ":" + self.location

        # and this mail store goes in to our dict of active mailstores
        # so that it can be looked up when new connections come in
        #
        active_mailstores[self.name] = self

        # XXX Create pipe for talking to MailStore
        #
        conn_to_srvr, self.conn_to_clnt = multiprocessing.Pipe()

        # XXX Create UserStore process for user_name
        #
        self.user_store = multiprocessing.Process(target = mailstore.create,
                                                  args = (conn_to_srvr,
                                                          location, user_name))
        self.user_store.start()
        asynchat.async_chat.__init__(self, asyncore.file_dispatcher(self.conn_to_clnt))
        self.set_terminator(None)


    ##################################################################
    #
    def collect_incoming_data(self, data):
        """
        Called when we have received some data from the MailStore
        server to send back to an IMAP client.

        It will be an imap response, and the name/address of the IMAP
        client to send the response to.

        Arguments:
        - `data`: tuple of IMAP response, and IMAP client destination.
        """
        pass

    ##################################################################
    #
    def found_terminator(self):
        """
        Called after we have received all the messages necessary for
        one message to an IMAP client.
        """
        pass

    ##################################################################
    #
    def handle_read(self):
        """
        """
#############################################################################
#
class IMAPClient(asynchat.async_chat):
    LINE_TERMINATOR     = "\r\n"

    ############################################################################
    #
    def __init__(self, conn_sock, client_address, server):
        """
        As can be seen, the init method calls
        async_chat.set_terminator() method with a string argument. The
        string argument tells async_chat that a message or record is
        terminated when it encounters the string in the data. Now,
        loop() will wait on this client socket and call async_chat’s
        handle_read() method. async_chat’s handle_read() will read the
        data, look at it, and call the collect_incoming_data() method
        that you define.
        """
        asynchat.async_chat.__init__(self, conn_sock)
        self.server             = server
        self.client_address     = client_address
        self.ibuffer            = []

        self.set_terminator(self.LINE_TERMINATOR)

    ############################################################################
    #
    def collect_incoming_data(self, data):
        """
        Buffer data read from the connect for later processing.
        """
        log.debug("collect_incoming_data: [%s]" % data)
        self.ibuffer.append(data)

    ############################################################################
    #
    def found_terminator(self):
        """
        Now, in the handle_read() method, async_chat will look for the
        string set by set_terminator(). If it finds it, then it will
        call the found_terminator().

        When we find that we have a complete line (because it was
        terminated by “\r\n”) we just send the data back. After all,
        we are writing an echo server.
        """
        log.debug("found_terminator")

        if self.reading_string_literal:
            # If we were reading a string literal, then we switch back
            # to reading lines.
            #
            self.reading_string_literal = False
            self.set_terminator(self.LINE_TERMINATOR)
            return

        # We have just read a full line. This could be an IMAP command
        # unless the line ends in '{[0-9]+}' which means that the
        # line contains a string literal. We have to switch to reading
        # a string literal for the number of characters defined by
        # the integer inside of the '{}'
        #
        m = re_literal_start.search(self.ibuffer[-1])
        if m:
            # Set how many characters to read and tell the client
            # that we are ready to receive more data from them.
            #
            self.set_terminator(int(m.group(1)))
            self.reading_string_literal = True
            self.push("+ Ready for more input\r\n")
            return

        # Otherwise we have a full IMAP message from the client
        # we need to parse it.
        #
        try:
            imap_command_str = "".join(self.ibuffer)
            imap_command = IMAPClientCommand(imap_command_str)
        except IMAPParse.BadCommand, e:
            if imap_command.tag is not None:
                self.push("%s BAD %s\r\n" % (imap_command.tag, str(e)))
            else:
                self.push("* BAD %s\r\n" % str(e.value))
            return

        # We have a parsed IMAP command, deal with it depending on the state we
        # are in.. authenticated ore pre-authenticated.
        #

    ############################################################################
    #
    def send_data(self):
        data = "".join(self.ibuffer)
        log.debug("sending: [%s]" % data)
        self.push(data+self.LINE_TERMINATOR)
        self.ibuffer = []

#############################################################################
#
class IMAPServer(asyncore.dispatcher):
    """
    """

    def __init__(self, address, handlerClass = IMAPClient):
        """
        """
        self.address            = address
        self.handlerClass       = handlerClass

        asyncore.dispatcher.__init__(self)
        self.create_socket(self.address_family,
                           self.socket_type)

        if self.allow_reuse_address:
            self.set_resue_addr()

        self.server_bind()
        self.server_activate()

    ############################################################################
    #
    def server_bind(self):
        self.bind(self.address)
        log.debug("bind: address=%s:%s" % (self.address[0], self.address[1]))

    ############################################################################
    #
    def server_activate(self):
        self.listen(self.request_queue_size)
        log.debug("listen: backlog=%d" % self.request_queue_size)

    ############################################################################
    #
    def fileno(self):
        return self.socket.fileno()

    ############################################################################
    #
    def serve_forever(self):
        asyncore.loop()    # TODO: try to implement handle_request()

    ############################################################################
    #
    # Internal use
#     def handle_accept(self):
#         (conn_sock, client_address) = self.accept()
#         if self.verify_request(conn_sock, client_address):
#             self.process_request(conn_sock, client_address)

    ############################################################################
    #
    def verify_request(self, conn_sock, client_address):
        return True

    ############################################################################
    #
    def process_request(self, conn_sock, client_address):
        log.info("conn_made: client_address=%s:%s" % \
                     (client_address[0],
                      client_address[1]))
        self.handlerClass(conn_sock, client_address, self)

    ############################################################################
    #
    def handle_close(self):
        self.close()
