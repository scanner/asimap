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
import sys
import socket
import asyncore
import asynchat
import logging
import os
import pwd
import sqlite3
import mailbox

# asimap imports
#
import asimap
import asimap.parse

from asimap.client import Authenticated
from asimap.db import Database

# By default every file is its own logging module. Kind of simplistic
# but it works for now.
#
log      = logging.getLogger("asimap.%s" % __name__)

BACKLOG  = 5

####################################################################
#
def set_user_server_program(prg):
    """
    Sets the 'USER_SERVER_PROGRAM' attribute on this module (so other modules
    will known how to launch the user server.)

    Arguments:
    - `prg`: An absolute path to the user server program.
    """
    module = sys.modules[__name__]
    setattr(module, "USER_SERVER_PROGRAM", prg)
    return

##################################################################
##################################################################
#
class IMAPUserClientHandler(asynchat.async_chat):
    """
    This class receives messages from the main server process.

    These messages are recevied by the main server process from an IMAP client
    and it has sent them on to us to process.

    All of the messages we receive will be for an IMAP client that has
    successfully authenticated with the main server.

    The messages will be in the form of a decimal ascii integer followed by a
    new line that represents the length of the entire IMAP message we are being
    sent.

    After that will be the IMAP message (of the pre-indicated length.)

    To send messages back to the IMAP client we follow the same protocol.
    """

    LINE_TERMINATOR     = "\n"

    ##################################################################
    #
    def __init__(self, sock, port, server, options):
        """
        """
        asynchat.async_chat.__init__(self, sock = sock)

        self.log = logging.getLogger("%s.IMAPUserClientHandler" % __name__)
        self.reading_message = False
        self.ibuffer = []
        self.set_terminator(self.LINE_TERMINATOR)

        # A reference to our entry in the server.handlers dict so we can remove
        # it when our connection to the main server is shutdown.
        #
        self.port = port
        
        # A handle on the server process and its database connection.
        #
        self.server = server
        self.options = options
        self.cmd_processor = Authenticated(self, self.server)
        return

    ##################################################################
    #
    def log_info(self, message, type = "info"):
        """
        Replace the log_info method with one that uses our stderr logger
        instead of trying to write to stdout.

        Arguments:
        - `message`: The message to log
        - `type`: Type of message to log.. maps to 'info','error',etc on the
                  logger object.
        """
        if type not in self.ignore_log_types:
            if type == "info":
                self.log.info(message)
            elif type == "error":
                self.log.error(message)
            elif type == "warning":
                self.log.warning(message)
            elif type == "debug":
                self.log.debug(message)
            else:
                self.log.info(message)
    
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

        1) we have hit LINE_TERMINATOR and we were waiting for it.  At this
           point the buffer should contain an integer as an ascii string. This
           integer is the length of the actual message.

        2) We are reading the message itself.. we read the appropriate number
           of bytes from the channel.

        If (2) then we exit the state where we are reading the IMAP message
        from the channel and set the terminator back to LINE_TERMINATOR so that
        we can read the rest of the message from the IMAP client.
        """
        self.log.debug("found_terminator")

        if not self.reading_message:
            # We have hit our line terminator.. we should have an ascii
            # representation of an int in our buffer.. read that to determine
            # how many characters the actual IMAP message we need to read is.
            #
            try:
                msg_length = int("".join(self.ibuffer).strip())
                self.ibuffer = []
                self.log.debug("Read IMAP message length indicator: %d" % \
                                   msg_length)
                self.reading_message = True
                self.set_terminator(msg_length)
            except ValueError,e:
                self.log.error("found_terminator(): expected an int, got: "
                               "'%s'" % "".join(self.ibuffer))
            return

        # If we were reading a full IMAP message, then we switch back to
        # reading lines.
        #
        imap_msg = "".join(self.ibuffer)
        self.ibuffer = []
        self.reading_message = False
        self.set_terminator(self.LINE_TERMINATOR)

        self.log.debug("Got complete IMAP message: %s" % imap_msg)

        # Parse the IMAP message. If we can not parse it hand back a 'BAD'
        # response to the IMAP client.
        #
        try:
            imap_cmd = asimap.parse.IMAPClientCommand(imap_msg)
            imap_cmd.parse()

        except asimap.parse.BadCommand, e:
            # The command we got from the client was bad...  If we at least
            # managed to parse the TAG out of the command the client sent us we
            # use that when sending our response to the client so it knows what
            # message we had problems with.
            #
            if imap_cmd.tag is not None:
                self.push("%s BAD %s\r\n" % (imap_cmd.tag, str(e)))
            else:
                self.push("* BAD %s\r\n" % str(e))
            return

        # Message parsed successfully. Hand it off to the message processor to
        # respond to.
        #
        self.cmd_processor.command(imap_cmd)

        # If our state is "logged_out" after processing the command then the
        # client has logged out of the authenticated state. We need to close
        # our connection to the main server process.
        #
        if self.cmd_processor.state == "logged_out":
            self.log.info("Client has logged out of the subprocess")
            self.cleanup()
            if self.socket is not None:
                self.close()
        return

    ##################################################################
    #
    def handle_close(self):
        """
        Huh. The main server process severed its connection with us. That is a
        bit strange, but, I guess it crashed or something.
        """
        self.log.info("main server closed its connection with us.")
        self.cleanup()
        if self.socket is not None:
            self.close()
        return

    ##################################################################
    #
    def cleanup(self):
        """
        This cleans up various references and resources held open by this
        client.

        The code was collected here because it is called when a client logs out
        or when the main server closes the connection to us.
        """
        # Be sure to remove our entry from the server.clients dict. Also go
        # through all of the active mailboxes and make sure the client
        # unselects any if it had selections on them.
        #
        del self.server.clients[self.port]
        for mbox in self.server.active_mailboxes.itervalues():
            mbox.unselected(self.cmd_processor)
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
    def __init__(self, options, maildir):
        """
        Setup our dispatcher.. listen on a port we are supposed to accept
        connections on. When something connects to it create an
        IMAPClientHandler and pass it the socket.

        Arguments:
        - `options` : The options set on the command line
        - `maildir` : The directory our mailspool and database are in
        """
        self.options = options

        asyncore.dispatcher.__init__(self)
        self.log = logging.getLogger("%s.%s" % (__name__,self.__class__.__name__))

        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind(("127.0.0.1", 0))
        self.address = self.socket.getsockname()
        self.listen(BACKLOG)
        self.maildir = maildir
        self.mailbox = mailbox.MH(self.maildir, create = True)
        
        # A global counter for the next available uid_vv is stored in the user
        # server object. Mailboxes will get this value and increment it when
        # they need a new uid_vv.
        #
        self.uid_vv = 0

        # A handle to the sqlite3 database where we store our persistent
        # information.
        #
        self.db = Database(maildir)

        # A dict of the active mailboxes. An active mailbox is one that has an
        # instance of an asimap.mbox.Mailbox class.
        #
        # We keep active mailboxes around when IMAP clients are poking them in
        # some way. Active mailboxes are gotten rid of after a certain amount
        # of time during which no client pokes it.
        #
        # The key is the mailbox name.
        #
        self.active_mailboxes = { }

        # A dict of the active IMAP clients that are talking to us.
        #
        # The key is the port number of the attached client.
        #
        self.clients = { }

        # When we have any connected clients this time gets set to
        # None. Otherwise use it to determine when we have hung around long
        # enough with no connected clients and decide to exit.
        #
        self.time_since_no_clients = time.time()

        # and finally restore any pesistent state stored in the db for the user
        # server.
        #
        self._restore_from_db()
        return

    ##################################################################
    #
    def _restore_from_db(self):
        """
        Restores any user server persistent state we may have in the db.
        If there is none saved yet then we save a bunch of default values.
        """
        c = self.db.cursor()
        c.execute("select uid_vv from user_server order by id desc limit 1")
        results = c.fetchone()
        if results is None:
            c.execute("insert into user_server (uid_vv) values (?)",
                      str(self.uid_vv))
            c.close()
            self.db.commit()
        else:
            self.uid_vv = int(results[0])
            c.close()
        return

    ##################################################################
    #
    def get_next_uid_vv(self):
        """
        Return the next uid_vv. Also update the underlying database
        so that its uid_vv state remains up to date.
        """
        self.uid_vv += 1
        c = self.db.conn.cursor()
        c.execute("update user_server set uid_vv = ?", (str(self.uid_vv),))
        c.close()
        self.db.commit()
        return self.uid_vv
    
    ##################################################################
    #
    def log_info(self, message, type = "info"):
        """
        Replace the log_info method with one that uses our stderr logger
        instead of trying to write to stdout.

        Arguments:
        - `message`:
        - `type`:
        """
        if type not in self.ignore_log_types:
            if type == "info":
                self.log.info(message)
            elif type == "error":
                self.log.error(message)
            elif type == "warning":
                self.log.warning(message)
            elif type == "debug":
                self.log.debug(message)
            else:
                self.log.info(message)
        return

    ##################################################################
    #
    def find_all_folders(self):
        """
        This goes through all of the folders and makes sure we have
        db records for all of them.
        """
        pass

    ##################################################################
    #
    def check_all_folders(self, ):
        """
        This goes through all of the folders and makes sure we have
        db records for all of them.

        It then sees if any of the mtimes we have on disk disagree with the
        mtimes we have in the database.

        If they do we then do a resync of that folder.

        If the folder is an active folder it may cause messages to be generated
        and sent to clients that are watching it in some way.
        """
        pass
    
    
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
            self.log.info("Incoming connection from %s" % repr(pair))
            handler = IMAPUserClientHandler(sock, addr[1], self, self.options)
            self.clients[addr[1]] = handler
        
