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
import asyncore
import asynchat
import logging
import shlex
import socket
import ssl
import os
import pwd
import re
import traceback
import subprocess

# asimap imports
#
import asimap.parse
import asimap.user_server

from asimap.client import PreAuthenticated
from asimap.auth import AUTH_SYSTEMS

# By default every file is its own logging module. Kind of simplistic
# but it works for now.
#
log      = logging.getLogger("%s" % __name__)

BACKLOG  = 5
RE_LITERAL_STRING_START = re.compile(r'\{(\d+)\}$')

# This dict is all of the subprocesses that we have created. One for each
# authenticated user with at least one active connection.
#
# The key is the username. The value is an IMAPSubprocessHandle.
#
#
user_imap_subprocesses = { }

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
        self.log = logging.getLogger("%s.IMAPSubprocessHandle" % __name__)
        self.user = user
        self.port = None
        self.subprocess = None
        self.rc = None

    ##################################################################
    #
    def start(self):
        """
        Start our subprocess. This assumes that we have no subprocess
        already. If we do then we will be basically creating an orphan process.
        """
        cmd = shlex.split("%s --debug" % asimap.user_server.USER_SERVER_PROGRAM)
        self.log.debug("Starting user server, cmd: %s, as user: '%s', in "
                       "directory '%s'" % (repr(cmd),self.user.local_username,
                                           self.user.maildir))
        self.subprocess = subprocess.Popen(cmd,
                                           preexec_fn = self.setuid_to_user,
                                           close_fds = True,
                                           cwd = self.user.maildir,
                                           stdout = subprocess.PIPE)

        # We expect the subprocess to send back to us over its stdout a single
        # line which has the port it is listening on.
        #
        # XXX This is bad in that our server will block while waiting for this
        #     I wonder if we can use a new asynchat client to handle this
        #     communication.
        #
        try:
            self.port = int(self.subprocess.stdout.read().strip())
        except ValueError, e:
            self.log.error("Unable to read port definition from subprocess: "
                           "%s" % str(e))
            # XXX Uh.. what do we do here? basically the subprocess start
            # failed and we need to tell our caller so they can deal with it.
            #
            raise
        return

    ##################################################################
    #
    def setuid_to_user(self):
        """
        This is called as the pre-exec function for subprocess.Popen. It is
        what changes the user from root (presumably) to the actual owner of the
        mailbox we are going to be serving up.

        We also close stdin, because the subprocess will not be reading
        anything so we want to be tidy.
        """
        os.close(0)

        # XXX If we are using the 'test_auth' system then we do not actually
        #     try to setuid (it would fail anyways.)
        #
        if self.user.auth_system != AUTH_SYSTEMS["test_auth"]:
            os.setuid(pwd.getpwnam(self.user.local_username)[2])
        return

    ##################################################################
    #
    def is_alive(self):
        """
        Calls the subprocess 'poll' method on our popen object to see if it is
        still around. This needs to be called before any attempt to establish
        communication with the subprocess. If it returns false then 'start()'
        must be called before attempting to talk to the subprocess.
        """
        if self.subprocess is None:
            return False
        
        self.rc = self.subprocess.poll()
        if self.rc is None:
            return True

        self.subprocess = None
        if self.rc != 0:
            self.log.error("Subprocess had non-zero return code: %d" % rc) 
        return False
    
    
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
        self.log = logging.getLogger("%s.IMAPServer" % __name__)
        
        asyncore.dispatcher.__init__(self)

        self.options = options
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind((options.interface, options.port))
        self.listen(BACKLOG)
        self.log.info("IMAP Server listening on %s:%d" % \
                          (options.interface,options.port))
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
            handler = IMAPClientHandler(sock, self.options)

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
        self.log = logging.getLogger("%s.IMAPClientHandler" % __name__)

        asynchat.async_chat.__init__(self, sock = sock)

        self.reading_string_literal = False
        self.ibuffer = []
        self.set_terminator(self.LINE_TERMINATOR)
        self.msg_processor = ServerIMAPMessageProcessor(self)

    ############################################################################
    #
    def collect_incoming_data(self, data):
        """
        Buffer data read from the connect for later processing.
        """
        self.log.debug("collect_incoming_data: [%s]" % data)
        self.ibuffer.append(data)
        return
    
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
        m = RE_LITERAL_STRING_START.search(self.ibuffer[-1])
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

        # Pass the full IMAP message on to the server IMAP message processor to
        # deal with.
        #
        msg = "".join(self.ibuffer)
        self.ibuffer = []
        if self.msg_processor is None:
            self.log.error("We have no message processor to send a message to.")
        else:
            self.msg_processor.message(msg)
        return

    ##################################################################
    #
    def handle_close(self):
        """
        What to do when the IMAP client we are talking to closes their
        connection to us.

        Basically tell the ServerIMAPMessageProcessor that its services will no
        longer be needed and various bits of cleanup.
        """
        self.log.info("Client disconnected")
        if self.msg_processor is not None:
            self.msg_processor.client_disconnected()
            self.msg_processor = None
        return
    

##################################################################
##################################################################
#
class ServerIMAPMessageProcessor(asynchat.async_chat):
    """
    This is the server object that handles full messages from the IMAP client
    and either processes them and responds to the client (if the client is not
    authenticated) or passes the commands on to a subprocess that processes the
    commands. Messages from the subprocess are then relayed back to the IMAP
    client via the client_connection object.

    An instance of this is created for every IMAP client that connects to the
    server.

    If there is no subprocess for this authenticated user we create one and
    connect to it via a localhost TCP connection.
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
        self.log = logging.getLogger("%s.ServerIMAPMessageProcessor" % __name__)

        asynchat.async_chat.__init__(self)

        self.client_connection = client_connection
        self.client_handler = PreAuthenticated(self.client_connection,
                                               AUTH_SYSTEMS["test_auth"])
        self.authenticated = False
        self.ibuffer = []
        self.subprocess = None
        self.reading_message = False
        self.set_terminator("\n")

    ##################################################################
    #
    def message(self, msg):
        """
        Handle an IMAP message from a client.

        If the client is NOT authenticated then we parse this message and hand
        it to a local IMAP message processor to deal with.

        If the client IS authenticated then we send it on to the subprocess
        that is dealing with the user's actual mail spool.

        Arguments: - `msg`: A full IMAP message from an IMAP client
        """

        # If the IMAP client is authenticated then we can just push the IMAP
        # messages off to the subprocess to handle.
        #
        # XXX What are the failure modes here that we need to worry about?  Can
        #     we push messages before the connection has actually been
        #     established?
        #
        if self.authenticated:
            self.push("%d\n" % len(msg))
            self.push(msg)
            return

        # The user has not authenticated we need to locally parse the message
        # and deal with all of the IMAP protocol interactions required for a
        # user to authenticate...
        #
        try:
            imap_cmd = asimap.parse.IMAPClientCommand(msg)
            imap_cmd.parse()

        except asimap.parse.BadCommand, e:
            # The command we got from the client was bad...  If we at least
            # managed to parse the TAG out of the command the client sent us we
            # use that when sending our response to the client so it knows what
            # message we had problems with.
            #
            if imap_cmd.tag is not None:
                msg = "%s BAD %s\r\n" % (imap_cmd.tag, str(e))
            else:
                msg = "* BAD %s\r\n" % str(e)
            self.client_connection.push(msg)
            return

        # This hands the IMAP command to be processed by the client handler
        # (dealing with everything before the client is in the authenticated
        # state.)
        #
        try:
            self.client_handler.command(imap_cmd)
        except Exception, e:
            # We catch all exceptions because we do not want the server
            # unceremoniously exiting. 
            #
            # XXX However an exception making it to this level probably means
            #     we should disconnect the client?
            #
            tb = traceback.format_exc()
            self.log.error("Exception handling IMAP command %s(%s): %s\n%s" % \
                               (imap_cmd.command, imap_cmd.tag, str(e),tb))

        # After processing that command see if we are in the authenticated or
        # logged out state and take the appropriate action.
        #
        if self.client_handler.state == "authenticated":
            # The client has authenticated to us.. connect a subprocess
            # that will handle the client's messages from now until it
            # logs out.
            #
            try:
                self.get_and_connect_subprocess(self.client_handler.user)
            except Exception, e:
                # If we fail to launch our subprocess then tell the client we
                # had an internal error and log it for us to figure out what
                # went wrong.
                #
                tb = traceback.format_exc()
                self.log.error("Exception starting subprocess: %s\n%s" % \
                               (str(e),tb))
                msg = "* BAD Internal error launching user mail spool\r\n"
                self.client_connection.push(msg)
                self.client_connection.close()
                if self.socket is not None:
                    self.close()
                return
        elif self.client_handler.state == "logged_out":
            # The client has logged out. We need to close our connection to the
            # subprocess if we have it, and close our connection to the
            # client. Doing these two things should cause this object to be
            # removed from the asyncore dispatcher loop.
            #
            if self.socket is not None:
                self.close()
            self.client_connection.close()
        return
    
    ##################################################################
    #
    def collect_incoming_data(self, data):
        """
        Buffer data read from the connect for later processing.
        """
        self.log.debug("collect_incoming_data: [%s]" % data)
        self.ibuffer.append(data)
        return
    
    ##################################################################
    #
    def found_terminator(self):
        """
        The subprocess will send us messages to send to the IMAP client. Like
        the messages we send to the subprocess we will be getting fully formed
        IMAP protocol messages that are prefixed by a length and a newline.

        So we have two states:

        1) we are waiting for a newline so we can know how many characters long
           the IMAP message is.

        2) we have read the whole IMAP message from the subprocess.
        """
        if not self.reading_message:
            # We have hit our line terminator.. we should have an ascii
            # representation of an int in our buffer.. read that to determine
            # how many characters the actual IMAP message we need to read is.
            #
            msg_length = int("".join(self.ibuffer).strip())
            self.ibuffer = []
            self.log.debug("Read IMAP message length indicator: %d" % \
                           msg_length)
            self.set_terminator(msg_length)
            return

        # If we were reading a full IMAP message then this means we have
        # received the entire message and we need to switch the line terminator
        # back to '\n' reading lines.
        #
        imap_msg = "".join(self.ibuffer)
        self.ibuffer = []
        self.reading_message = False
        self.set_terminator("\n")

        self.log.debug("Got complete IMAP message: %s" % imap_msg)

        # Send the message on to the client. We check to make sure this exists
        # in case the client suddenly disconnects from us.
        #
        if self.client_connection is not None:
            self.client_connection.push(imap_msg)
        else:
            # XXX need better debugging so we have some idea of what client
            #     we were expecting to be able to talk to.
            #
            self.log.warn("Unable to send message to client, "
                          "client_connection was None")
        return

    ##################################################################
    #
    def get_and_connect_subprocess(self, user):
        """
        At this point the IMAP client has authenticated to us and we know what
        user they authenticated as. We need to see if there is an existing
        subprocess handler for this user.

        If there is none then we create one.
        """
        if user.imap_username in user_imap_subprocesses:
            self.subprocess = user_imap_subprocesses[user.imap_username]
        else:
            self.subprocess = IMAPSubprocessHandle(user)
            user_imap_subprocesses[user.imap_username] = self.subprocess
            
        if not self.subprocess.is_alive():
            self.subprocess.start()

        # And initiate a connection to the subprocess.
        #
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.log.debug('connecting to localhost:%d' % self.subprocess.port)
        self.connect(('127.0.0.1', self.subprocess.port))
        return

    ##################################################################
    #
    def handle_connect(self):
        """
        We have established a connection to the subprocess for this user.
        Yay.
        """
        self.log.debug("handle_connect()")
        return

    ##################################################################
    #
    def handle_close(self):
        """
        This gets called when the subprocess handling this user's mailspool has
        disconnected from us.

        This can happen for two reasons:

        1) The IMAP client has gone through the process of de-authenticating
           and they are no longer authenticated. The subprocess signals the
           success of this IMAP protocol transaction by closing its TCP
           connection to us.

        2) However, this is also what will happen if the subprocess crashes for
           some reason. Crashes happen. In this case we need to send some
           signal back to the IMAP client so that it does not sit there
           thinking it can send messages to us expecting us to be in some IMAP
           state that we are not in. So, in this case we need to close our
           connection to the IMAP client as if we had indeed crashed.

           THe question is: how do we know that this is the case, not (1)?
           Although this is not perfect what we do is see if the subprocess is
           still alive, and if it is not, see if its exit code is non-zero.

           If it is not alive and its exit code is non-zero then we know it
           crashed.
        """

        self.client_handler.state = "non_authenticated"

        # See if the subprocess is alive.. if it is not then it ungraciously
        # went away and we need to tell the IMAP client to go away too.
        #
        if self.subprocess.is_alive == False:
            self.log.warn("Our subprocess for user '%s' went away " \
                          "unexpectedly with the exit code: %d" % \
                          (self.client_handler.user,self.subprocess.rc))

            # Since we lost the connection to our subprocess close the
            # connection to the IMAP client too.
            #
            self.client_connection.close()
        self.client_handler.user = None
        return

    ##################################################################
    #
    def client_disconnected(self):
        """
        This is called when the IMAP client has disconnected from us.

        We close our connection to the subprocess and do various cleanups.
        """
        self.log.debug("client_disconnected()")
        self.client_connection = None
        self.client_handler.state = "non_authenticated"
        self.client_handler.user = None
        self.subprocess = None

        # If we have a connection to the subprocess then close it too.
        #
        if self.socket is not None:
            self.close()
        return
    
