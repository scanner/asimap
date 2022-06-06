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
import asynchat
import asyncore
import errno
import logging
import mailbox
import os
import os.path
import random
import socket
import sys
import time
import traceback

# asimap imports
#
import asimap
import asimap.mbox
import asimap.message_cache
import asimap.parse
from asimap.client import Authenticated
from asimap.db import Database
from asimap.exceptions import MailboxInconsistency, MailboxLock
from asimap.trace import trace

# By default every file is its own logging module. Kind of simplistic
# but it works for now.
#
log = logging.getLogger(f"asimap.{__name__}")

BACKLOG = 5


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

    LINE_TERMINATOR = "\n"

    ##################################################################
    #
    def __init__(self, sock, rem_address, port, server, options):
        """ """
        asynchat.async_chat.__init__(self, sock=sock)

        self.log = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.reading_message = False
        self.ibuffer = []
        self.set_terminator(self.LINE_TERMINATOR)

        # A reference to our entry in the server.handlers dict so we can remove
        # it when our connection to the main server is shutdown.
        #
        self.port = port
        self.rem_addr = rem_address
        self.trace("CONNECT", {})

        # A handle on the server process and its database connection.
        #
        self.server = server
        self.options = options
        self.cmd_processor = Authenticated(self, self.server)
        return

    ####################################################################
    #
    def trace(self, msg_type, msg):
        """
        We like to include the 'identity' of the IMAP Client handler in
        our trace messages so we can tie to gether which messages come
        from which connection. To make this easier we provide our own
        trace method that fills in various parts of the message being
        logged automatically.

        Keyword Arguments:
        msg_type -- 'SEND','RECEIVE','EXCEPTION','CONNECT','REMOTE_CLOSE'
        msg -- a dict that contains the rest of the message to trace log
        """
        msg["connection"] = "{}:{}".format(self.rem_addr, self.port)
        msg["msg_type"] = msg_type
        trace(msg)

    ####################################################################
    #
    def push(self, data):
        """
        We have our own version of push that logs sent messages to our
        trace file if we have one.

        Keyword Arguments:
        data -- (str) data that is being sent to the client and that we
                      need to log.
        """
        self.trace("SEND", {"data": data})

        # XXX asyncore.dispatcher which asynchat.async_chat is a
        #     subclass of is an old-style class and thus we can not
        #     use 'super()' (all the more reason to move off of this
        #     and use something more modern.)
        #
        asynchat.async_chat.push(self, data)

    ##################################################################
    #
    def log_string(self):
        """
        format the username/remote address/port as a string
        """
        return f"from {self.rem_addr}:{self.port}"

    ##################################################################
    #
    def log_info(self, message, type="info"):
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

    ##################################################################
    #
    def handle_error(self):
        """
        Override the aysnc_chat's error handler so that we can log the
        message more directly in such a way that errorstack will get a
        full stack trace.
        """
        t, v, tb = sys.exc_info()
        # sometimes a user repr method will crash.
        try:
            self_repr = repr(self)
        except Exception:
            self_repr = "<__repr__(self) failed for object at %0x>" % id(self)

        tb_string = "".join(traceback.format_tb(tb))
        self.trace("EXCEPTION", {"data": [str(t), str(v), tb_string]})
        log.error(
            f"uncaptured python exception, closing channel {self_repr} "
            f"({t}:{v})",
            exc_info=(t, v, tb),
        )
        self.close()

    ###########################################################################
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
        if not self.reading_message:
            # We have hit our line terminator.. we should have an ascii
            # representation of an int in our buffer.. read that to determine
            # how many characters the actual IMAP message we need to read is.
            #
            try:
                msg_length = int("".join(self.ibuffer).strip())
                self.ibuffer = []
                self.reading_message = True
                self.set_terminator(msg_length)
            except ValueError:
                self.log.error(
                    "found_terminator(): %s expected an int, got: "
                    "'%s'" % (self.log_string(), "".join(self.ibuffer))
                )
            return

        # If we were reading a full IMAP message, then we switch back to
        # reading lines.
        #
        imap_msg = "".join(self.ibuffer)
        self.trace("RECEIVED", {"data": imap_msg})
        self.ibuffer = []
        self.reading_message = False
        self.set_terminator(self.LINE_TERMINATOR)

        # Parse the IMAP message. If we can not parse it hand back a 'BAD'
        # response to the IMAP client.
        #
        # We special case if the client is idling. In this state we look for
        # ONLY a 'DONE' non-tagged message and when we get that we call the
        # 'do_done()' method on the client command processor.
        #
        if self.cmd_processor.idling:
            if imap_msg.lower().strip() != "done":
                self.push("* BAD Expected 'DONE' not: %s\r\n" % imap_msg)
            else:
                self.cmd_processor.do_done(None)
            return

        try:
            imap_cmd = asimap.parse.IMAPClientCommand(imap_msg)
            imap_cmd.parse()

        except asimap.parse.BadCommand as e:
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

        # Pass the command on to the command processor to handle.
        #
        self.cmd_processor.command(imap_cmd)

        # If our state is "logged_out" after processing the command then the
        # client has logged out of the authenticated state. We need to close
        # our connection to the main server process.
        #
        if self.cmd_processor.state == "logged_out":
            self.log.info(
                "Client %s has logged out of the subprocess" % self.log_string()
            )
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
        msg = "main server closed its connection with us. " "{}:{}".format(
            self.rem_addr, self.port
        )
        self.log.info(msg)
        self.trace("REMOTE_CLOSE", {"msg": msg})
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
        if self.port in self.server.clients:
            del self.server.clients[self.port]
        for mbox in self.server.active_mailboxes.values():
            mbox.unselected(self.cmd_processor)

        # If the user server has no more clients then start the idle timeout
        # clock
        #
        if len(self.server.clients) == 0:
            self.log.debug(
                "cleanup(): Server has no clients, starting timeout clock"
            )
            self.expiry = time.time() + 1800

        return


##################################################################
##################################################################
#
class IMAPUserServer(asyncore.dispatcher):
    """
    Listen on a port on localhost for connections from the asimapd
    main server that gets connections from actual IMAP clients. When
    we get one create an IMAPUserClientHandler object that gets the
    new connection (and handles all further IMAP related
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
        self.log = logging.getLogger(
            "%s.%s" % (__name__, self.__class__.__name__)
        )

        # Do NOT create our socket if we are running in standalone mode
        #
        if not self.options.standalone_mode:
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
            self.set_reuse_addr()
            self.bind(("127.0.0.1", 0))
            self.address = self.socket.getsockname()
            self.listen(BACKLOG)

        self.maildir = maildir
        self.mailbox = mailbox.MH(self.maildir, create=True)

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
        self.active_mailboxes = {}

        # A dict of the active IMAP clients that are talking to us.
        #
        # The key is the port number of the attached client.
        #
        self.clients = {}

        # There is a single message cache per user server instance.
        #
        self.msg_cache = asimap.message_cache.MessageCache()

        # When we have any connected clients self.expiry gets set to
        # None. Otherwise use it to determine when we have hung around long
        # enough with no connected clients and decide to exit.
        #
        self.expiry = time.time() + 1800

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
            c.execute(
                "insert into user_server (uid_vv) values (?)", str(self.uid_vv)
            )
            c.close()
            self.db.commit()
        else:
            self.uid_vv = int(results[0])
            c.close()
        return

    ##################################################################
    #
    def has_queued_commands(self):
        """
        Returns True if any active mailbox has queued commands.
        """
        return any(
            active_mbox.has_queued_commands()
            for active_mbox in list(self.active_mailboxes.values())
        )

    ##################################################################
    #
    def process_queued_commands(self):
        """
        See if any active mailboxes have queued commands that still need to be
        processed and let them run if they do.
        """
        # Some IMAP clients can be nasty with the number of commands they have
        # running in parallel. Also you may have gobs of IMAP clients. In these
        # cases we want to make sure we still pull messages off of the network
        # socket without blocking for too long (5 seconds to actually read a
        # message is probably too long.)
        #
        # When we are processing queued messages they are popped off the front
        # of the command queue, and then if they need to be continued, pushed
        # on to the back of the command queue.
        #
        # What we do is generate a randomized list of mailboxes that have
        # queued commands. We then run through that list of mailboxes once,
        # doing one queued command from each mailbox or until a maximum of <n>
        # (5?) seconds has passed.
        #
        # Since the list of mailboxes is randomized each time we are called and
        # each time we run a queued command we stick it back on to the _end_ of
        # the command queue this should generally work through the queued
        # commands in a fair fashion.
        #
        mboxes = [
            x
            for x in list(self.active_mailboxes.values())
            if x.has_queued_commands()
        ]
        if not mboxes:
            return

        start_time = time.time()

        num_queued_cmds = sum(len(x.command_queue) for x in mboxes)
        self.log.debug(
            "process_queued_command: ** START Number of queued "
            "commands: %d" % num_queued_cmds
        )

        random.shuffle(mboxes)

        for mbox in mboxes:
            if len(mbox.command_queue) == 0:
                continue
            self.log.debug(
                "process_queued_command:    mbox: %s - commands in "
                "queue: %d" % (mbox.name, len(mbox.command_queue))
            )
            client, imap_cmd = mbox.command_queue.pop(0)

            # If the client's network connectionis closed then we do not
            # bother processing this command.. there is no one to receive
            # the results.
            #
            if not client.client.connected:
                continue

            try:
                self.log.debug(
                    "process_queued_command mbox: %s, client: "
                    "%s, cmd: %s" % (mbox.name, client.name, str(imap_cmd))
                )
                client.command(imap_cmd)
            except Exception:
                # We catch all exceptions and log them similar to what
                # asynchat does when processing a message.
                #
                nil, t, v, tbinfo = asyncore.compact_traceback()
                self.log.error(
                    "Exception handling queued command: "
                    "%s:%s %s" % (t, v, tbinfo)
                )

            # If we have been processing queued commands for more than n (5?)
            # seconds that is enough. Return to let the main loop read more
            # stuff from clients.
            #
            now = time.time()
            if now - start_time > 5:
                self.log.debug(
                    "process_queued_command: ** Running for %f "
                    "seconds. Will run more commands later" % (now - start_time)
                )
                return

        now = time.time()
        self.log.debug(
            "process_queued_command: ** Finished all queued "
            "commands. Took %f seconds" % (now - start_time)
        )

        return

    ##################################################################
    #
    def get_next_uid_vv(self):
        """
        Return the next uid_vv. Also update the underlying database
        so that its uid_vv state remains up to date.
        """
        self.uid_vv += 1
        c = self.db.cursor()
        c.execute("update user_server set uid_vv = ?", (str(self.uid_vv),))
        c.close()
        self.db.commit()
        return self.uid_vv

    ##################################################################
    #
    def log_info(self, message, type="info"):
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
    def get_mailbox(self, name, expiry=900):
        """
        A factory of sorts.. if we have an active mailbox with the given name
        return it.

        If we do not instantiate an instance of that mailbox and add it to our
        list of active mailboxes.

        Arguments:
        - `name`: The name of the mailbox our caller wants.
        - `expiry`: If we have to instantiate a mailbox give it this expiry
          time. Used so that boxes that are just being updated rarely expire
          and do not take up excess memory in the server. Defaults to 15
          minutes.
        """
        # The INBOX is case-insensitive but it is stored in our file system in
        # a case sensitive lower case fashion..
        #
        if name.lower() == "inbox":
            name = "inbox"
        if name in self.active_mailboxes:
            return self.active_mailboxes[name]

        # otherwise.. make an instance of this mailbox.
        #
        mbox = asimap.mbox.Mailbox(name, self, expiry=expiry)
        self.active_mailboxes[name] = mbox
        return mbox

    ##################################################################
    #
    def check_all_active_folders(self):
        """
        Like 'check_all_folders' except this only checks folders that are
        active and have clients in IDLE listening to them.
        """
        for name, mbox in self.active_mailboxes.items():
            if any(x.idling for x in mbox.clients.values()):
                try:
                    self.log.debug("check_all_active: checking '%s'" % name)
                    mbox.resync()
                except (MailboxLock, MailboxInconsistency) as e:
                    # If hit one of these exceptions they are usually
                    # transient.  we will skip it. The command processor in
                    # client.py knows how to handle these better.
                    #
                    self.log.warn(
                        "check-all-active: skipping '%s' due to: "
                        "%s" % (name, str(e))
                    )
        return

    ##################################################################
    #
    def expire_inactive_folders(self):
        """
        Go through the list of active mailboxes and if any of them are around
        past their expiry time, expire time.
        """
        # And finally check all active mailboxes to see if they have no clients
        # and are beyond their expiry time.
        #
        expired = []
        for mbox_name, mbox in self.active_mailboxes.items():
            if (
                len(mbox.clients) == 0
                and mbox.expiry is not None
                and mbox.expiry < time.time()
            ):
                expired.append(mbox_name)

        for mbox_name in expired:
            self.active_mailboxes[mbox_name].commit_to_db()
            del self.active_mailboxes[mbox_name]
            self.msg_cache.clear_mbox(mbox_name)
        if len(expired) > 0:
            self.log.debug(
                "expire_inactive_folders: Expired %d folders" % len(expired)
            )
        return

    ##################################################################
    #
    def find_all_folders(self):
        """
        compare the list of folders on disk with the list of known folders in
        our database.

        For every folder found on disk that does not exist in the database
        create an entry for it.
        """
        start_time = time.time()
        self.log.debug("find_all_folders: STARTING")
        extant_mboxes = {}
        mboxes_to_create = []
        c = self.db.cursor()
        c.execute("select name, mtime from mailboxes order by name")
        for row in c:
            name, mtime = row
            extant_mboxes[name] = mtime
        c.close()

        # The user_server's CWD is the root of our mailboxes.
        #
        for root, dirs, files in os.walk(".", followlinks=True):
            for d in dirs:
                dirname = os.path.normpath(os.path.join(root, d))
                if dirname not in extant_mboxes:
                    mboxes_to_create.append(dirname)

        # Now 'mboxes_to_create' is a list of full mailbox names that were in
        # the file system but not in the database. Instantiate these (with the
        # create flag set so that we will not get any nasty surpises about
        # missing .mh_sequence files)
        #
        for mbox_name in mboxes_to_create:
            self.log.debug("Creating mailbox '%s'" % mbox_name)
            self.get_mailbox(mbox_name, expiry=0)
        self.log.debug(
            "find_all_folders: FINISHED. Took %f seconds"
            % (time.time() - start_time)
        )

    ##################################################################
    #
    def check_all_folders(self, force=False):
        """
        This goes through all of the folders and sees if any of the mtimes we
        have on disk disagree with the mtimes we have in the database.

        If they do we then do a resync of that folder.

        If the folder is an active folder it may cause messages to be generated
        and sent to clients that are watching it in some way.

        The folder's \\Marked and \\Unmarked attributes maybe set in
        the process of this run.

        - `force` : If True this will force a full resync on all
                    mailbox regardless of their mtimes.
        """
        start_time = time.time()
        self.log.debug("check_all_folders begun")

        # Get all of the folders and mtimes we know about from the sqlite db at
        # the beginning. This takes more memory (not _that_ much really in the
        # grand scheme of things) but it gives the answers in one go-round to
        # the db and we get to deal with the data in an easier format.
        #
        mboxes = []
        c = self.db.cursor()
        c.execute(
            "select name, mtime from mailboxes where attributes "
            "not like '%%ignored%%' order by name"
        )
        for row in c:
            mboxes.append(row)
        c.close()

        # Now go through all of the extant mailboxes and see
        # if their mtimes have changed warranting us to force them to resync.
        #
        # XXX We should probably skip folders that are active and have been
        #     resync'd in the last 30 seconds because those are already checked
        #     by another process.
        #
        for mbox_name, mtime in mboxes:
            # If this mailbox is active and has a client idling on it OR if it
            # has queued commands then we can skip doing a resync here. It has
            # been handled already. It is especially important not to do
            # random resyncs on folders that are in the middle of processing a
            # queued command. It might cause messages to be generated and reset
            # various bits of state that are important to the queued command.
            #
            if mbox_name in self.active_mailboxes and (
                any(
                    x.idling
                    for x in self.active_mailboxes[mbox_name].clients.values()
                )
                or len(self.active_mailboxes[mbox_name].command_queue) > 0
            ):
                continue

            path = os.path.join(self.mailbox._path, mbox_name)
            seq_path = os.path.join(path, ".mh_sequences")
            try:
                fmtime = asimap.mbox.Mailbox.get_actual_mtime(
                    self.mailbox, mbox_name
                )
                if (fmtime > mtime) or force:
                    # The mtime differs.. force the mailbox to resync.
                    #
                    self.log.debug(
                        "check_all_folders: doing resync on '%s' "
                        "stored mtime: %d, actual mtime: %d"
                        % (mbox_name, mtime, fmtime)
                    )
                    m = self.get_mailbox(mbox_name, 30)
                    if (m.mtime >= fmtime) and not force:
                        # Looking at the actual mailbox its mtime is NOT
                        # earlier than the mtime of the actual folder so we can
                        # skip this resync. But commit the mailbox data to the
                        # db so that the actual mtime value is stored.
                        #
                        m.commit_to_db()
                    else:
                        # Yup, we need to resync this folder.
                        m.resync(force=force)
            except (MailboxLock, MailboxInconsistency) as e:
                # If hit one of these exceptions they are usually
                # transient.  we will skip it. The command processor in
                # client.py knows how to handle these better.
                #
                self.log.warn(
                    "check_all_folders: skipping '%s' due to: "
                    "%s" % (mbox_name, str(e))
                )
            except (OSError, IOError) as e:
                if e.errno == errno.ENOENT:
                    self.log.error(
                        "check_all_folders: One of %s or %s does "
                        "not exist for mtime check" % (path, seq_path)
                    )

        self.log.debug(
            "check_all_folders finished, Took %f seconds"
            % (time.time() - start_time)
        )
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
            sock, addr = pair
            self.log.info("Incoming connection from %s:%d" % addr)
            self.expiry = None
            handler = IMAPUserClientHandler(
                sock, addr[0], addr[1], self, self.options
            )
            self.clients[addr[1]] = handler
