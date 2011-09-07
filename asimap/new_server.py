#!/usr/bin/env python
#
# File: $Id$
#
"""
This is the heart of the main server. This is what accepts incoming
network connections, authenticates users, spawns userserver's, and
relays IMAP messages between an IMAP client and a userserver.
"""

# system imports
#
# system imports
#
import asyncore
import asynchat
import logging
import socket
import ssl
import os
import re


# By default every file is its own logging module. Kind of simplistic
# but it works for now.
#
log      = logging.getLogger("asimap.%s" % __name__)

BACKLOG  = 5
RE_LITERAL_STRING_START = r'\{(\d+)\}$'

# This dict is all of the subprocesses that we have created. One for each
# authenticated user with at least one active connection. It stores the port on
# 'localhost' that the subprocess for that user is listening on and a handle to
# the subprocess.Popen object, and how many ServerIMAPClient's are using this
# subprocess as a list <int port>,<subprocess.Popen instance>, <int number of
# ServerIMAPClient's>)
#
per_user_subprocesses = { }

##################################################################
##################################################################
#
class IMAPSubprocessHandle(object):
    """
    This is a handle to a multiprocess.Popen instance, the localhost port that
    instance is listening on, and how many local clients are using a reference
    to this subprocess.

    When an IMAP client connects to the server and authenticates if there is no
    subprocess for the user that the IMAP client authenticated as we create an
    instance of this class (which in turn creates a subprocess.)

    This sets the count of clients referring to this object to 1.

    Every new IMAP client that comes along that authenticates as the same user
    for which a subprocess exists will increment this count.

    When the count reaches 0 (by various IMAP clients logging out) the
    subprocess will be shutdown.

    When a subprocess starts up it will listen on a port on 'localhost'. It
    will then tell us (the IMAPSubprocessHandle object) that port. This port is
    what is used by other parts of the server to talk to the subprocess.
    """

    ##################################################################
    #
    def __init__(self, user):
        """
        
        Arguments:
        - `user`: The user that has authenticated to us and thus represents
                  the unique identifier for the subprocess we handle. The user
                  is passed to the subprocess so that it can look up which unix
                  user to switch to for handling that user's mailbox.
        """
        self.user = user
        self.port = None
        self.subprocess = None
        self.count = 1

        # Yes, our server is single threaded.. still we should maintain safety
        # over data that is manipulated by several different 'threads of
        # execution' which each IMAP client talking to us basically represents.
        #
        # You have to get a lock before you can increment or decrement the
        # count flag.
        #
        self.lock = threading.RLock()

        # And now create our subprocess.
        


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
        self.options = options

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
        A client has connected to us. Create the IMAPClientHandler object to
        handle that client and let it deal with it.
        """

        pair = self.accept()
        if pair is not None:
            sock,addr = pair
            print "Incoming connection from %s" % repr(pair)
            handler = IMAPClientHandler(sock, self._options)

##################################################################
##################################################################
#
class IMAPClientHandler(asynchat.async_chat):
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

    LINE_TERMINATOR     = "\r\n"

    ##################################################################
    #
    def __init__(self, sock, options):
        """
        """
        asyncchat.async_chat.__init__(self, sock = sock)

        self.reading_string_literal = False
        self.ibuffer = []
        self.obuffer = ""
        self.set_terminator(self.LINE_TERMINATOR)

    ############################################################################
    #
    def collect_incoming_data(self, data):
        """
        Buffer data read from the connect for later processing.
        """
        log.debug("collect_incoming_data: [%s]" % data)
        self.ibuffer.append(data)

    ##################################################################
    #
    def found_terminator(self):
        """
        We have come across a message terminator from the IMAP client talking
        to us.

        This is invoked in two different states:

        1) we have hit LINE_TERMINATOR and we were waiting for it.

        2) we have are reading a literal string and we have read the requisite
           number of characters for a complete message.

        If (2) then we exit the state where we are reading a string literal and
        set the terminator back to LINE_TERMINATOR so that we can read the rest
        of the message from the IMAP client.

        Otherwise we see if the message so far ends in the regexp
        '{[0-9]+}'. If it does then that means we have encountered a literal
        string in the input from the IMAP client.

        If that is the case we switch our terminator to be the number of
        characters to read from the client to complete that literal string.

        If that is NOT the case then we have read a complete IMAP message from
        our client and we pass it off to an ServerIMAPClient object to deal
        with.
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
            # Set how many characters to read
            #
            self.set_terminator(int(m.group(1)))
            self.reading_string_literal = True

            # Tell the IMAP client that we are ready to receive more data from
            # them.
            #
            self.push("+ Ready for more input\r\n")
            return

        # Create an IMAPClient object to handle messages from the IMAP client
        # for authentication and such.
        #
        if self.imap_client is None:
            self.imap_client = ServerIMAPClient(self)

        msg = "".join(self.ibuffer)
        self.ibuffer = []
        ServerIMAPClient.message(msg)
        return

##################################################################
##################################################################
#
class ServerIMAPClient(asynchat.async_chat):
    """
    This is the server object that handles full messages from the IMAP client
    and either processes them and responds to the client (if the client has not
    authenticated to us) or passes the commands on to a subprocess that
    processes the commands.

    If there is no subprocess for this authenticated user we create one and
    listen to what port it is going to listen on on localhost and store that
    information for other clients to use.

    If there is a subprocess then any messages it sends to us we push on to the
    imap client through the client_connection object we were passed.

    XXX Do we want to use subprocess or multiprocessing?
        Subprocess is a lot simpler, but the biggest issue is that it needs to
        run a python script via its path and that python script will be run as
        root! So someone could gain root privelegs if they can replace that
        script (same of course is true for the actual asimapd
        server.. Multiprocessing solves this but we need to remember to close
        all open sockets at the beginning of our subprocess in multiprocessing
        which could be annoying (but should not be that hard?) and we get the
        neat multiprocessing message passing stuff.
    """

    ##################################################################
    #
    def __init__(self, client_connection):
        """
        
        Arguments:
        - `client_connection`: An async_chat object that is a connect to the
                               IMAP client. We can use its 'push()' method to
                               send messages to the IMAP client.
        """
        self.client_connection = client_connection
        self.authenticated = False
        self.ibuffer = []
