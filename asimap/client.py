#!/usr/bin/env python
#
# File: $Id$
#
"""
Here we have the classes that represent the server side state for a
single connected IMAP client.
"""

# system imports
#
import sys
import logging
import os.path
import time

# asimapd imports
#
import asimap.mbox
import asimap.auth
from asimap.exceptions import No, Bad, MailboxInconsistency, MailboxLock

# Local constants
#
#CAPABILITIES = ('IMAP4rev1', 'IDLE', 'NAMESPACE', 'ID', 'LITERAL+', 'UIDPLUS')
CAPABILITIES = ('IMAP4rev1', 'IDLE', 'ID', 'UNSELECT')
SERVER_ID = { 'name'        : 'asimapd',
              'version'     : '0.2',
              'vendor'      : 'Apricot Systematic',
              'support-url' : 'https://github.com/scanner/asimap/issues',
              'command'     : sys.argv[0],
              'os'          : sys.platform,
              }

# States that our IMAP client handler can be in. These reflect the valid states
# from rfc2060.
#
STATES = ("not_authenticated","authenticated","selected","logged_out")

##################################################################
##################################################################
#
class BaseClientHandler(object):
    """
    Both the pre-authenticated and authenticated client handlers operate in the
    same manner. So we provide a base class that they both extend to have
    common functionality in one place.
    """

    ##################################################################
    #
    def __init__(self, client):
        """
        Arguments:
        - `client`: An asynchat.async_chat object that is connected to the IMAP
                    client we are handling. This lets us send messages to that
                    IMAP client.
        """
        self.log = logging.getLogger("%s.%s" % (__name__, self.__class__.__name__))
        self.client = client
        self.state = None
        self.name = "BaseClientHandler"

        # Idling is like a sub-state. When we are idling we expect a 'DONE'
        # completion from the IMAP client before it sends us any other
        # message. However during this time the server may still send async
        # messages to the client.
        #
        self.idling = False

        # This is used to keep track of the tag.. useful for when finishing an
        # DONE command when idling.
        #
        self.tag = None

        # If there are pending expunges that we need to send to the client
        # during its next command (that we can send pending expunges during)
        # they are stored here.
        #
        self.pending_expunges = []

        return

    ##################################################################
    #
    def command(self, imap_command):
        """
        Process an IMAP command we received from the client.

        We use introspection to find out what IMAP commands this handler
        actually supports.

        Arguments:
        - `imap_command`: An instance parse.IMAPClientCommand
        """
        # Since the imap command was properly parsed we know it is a valid
        # command. If it is one we support there will be a method
        # on this object of the format "do_%s" that will actually do the
        # command.
        #
        # If no such method exists then this is not a supported command.
        #
        self.tag = imap_command.tag
        if not hasattr(self, 'do_%s' % imap_command.command):
            self.client.push("%s BAD Sorry, %s is not a supported "
                             "command\r\n" % (imap_command.tag,
                                              imap_command.command))
            return

        # Okay. The command was a known command. Process it. Each 'do_' method
        # will send any messages back to the client specific to that command
        # except the "OK" response and any exceptional errors which are handled
        # by this method.
        #
        start_time = time.time()
        try:
            result = getattr(self, 'do_%s' % imap_command.command)(imap_command)
        except No, e:
            result = "%s NO %s\r\n" % (imap_command.tag, str(e))
            self.client.push(result)
            self.log.debug(result)
            return
        except Bad, e:
            result = "%s BAD %s\r\n" % (imap_command.tag, str(e))
            self.client.push(result)
            self.log.debug(result)
            return
        except MailboxLock, e:
            self.log.warn("Unable to get lock on mailbox '%s', putting on to command queue" % e.mbox.name)
            imap_command.needs_continuation = True
            e.mbox.command_queue.append(self, imap_command)
            return
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception, e:
            result = "%s BAD Unhandled exception: %s\r\n" % (imap_command.tag,
                                                             str(e))
            self.client.push(result)
            self.log.debug(result)
            raise

        # If there was no result from running this command then everything went
        # okay and we send back a final 'OK' to the client for processing this
        # command.
        #
        if result is None:
            result = "%s OK %s completed\r\n" % (imap_command.tag,
                                                 imap_command.command.upper())
            self.client.push(result)
            self.log.debug(result)
        elif result is False:
            # Some commands do NOT send an OK response immediately.. aka the
            # IDLE command and commands that are being processed in multiple
            # runs (see 'command_queue' on the mailbox). If result is false
            # then we just return. We do not send a message back to our client.
            #
            return
        else:
            # The command has some specific response it wants to send back as
            # part of the tagged OK response.
            #
            result = "%s OK %s %s completed\r\n" % \
                     (imap_command.tag, result,
                      imap_command.command.upper())
            self.client.push(result)
            self.log.debug(result)
        return

    ##################################################################
    #
    def send_pending_expunges(self):
        """
        Deal with pending expunges that have built up for this client.  This
        can only be called during a command, but not during FETCH, STORE, or
        SEARCH commands.

        Also we will not call this during things like 'select' or 'close'
        because they are no longer listening to the mailbox (but they will
        empty the list of pending expunges.
        """
        for p in self.pending_expunges:
            self.client.push(p)
        self.pending_expunges = []

    ##################################################################
    #
    def unceremonious_bye(self, msg):
        """
        Sometimes we hit a state where we can not easily recover while a client
        is connected. Frequently for clients that are in 'select' on a
        mailbox. In those cases we punt by forcibly disconnecting the client.

        With this we can usually restart whatever had problems (like a resync)
        and come out with things being proper.

        This method handles the basics of disconnecting a client.

        Arguments:
        - `msg`: The message to send to the client in the BYE.
        """
        self.client.push("* BYE %s\r\n" % msg)
        self.client.close()
        return

    ## The following commands are supported in any state.
    ##

    ##################################################################
    #
    def do_done(self, imap_command):
        """
        We have gotten a DONE. This is only called when we are idling.

        Arguments:
        - `imap_command`: This is ignored.
        """
        self.idling = False
        self.send_pending_expunges()
        self.client.push("%s OK IDLE terminated\r\n" % self.tag)
        return

    #########################################################################
    #
    def do_capability(self, imap_command):
        """
        Return the capabilities of this server.

        Arguments:
        - `imap_command`: The full IMAP command object.
        """
        self.send_pending_expunges()
        self.client.push("* CAPABILITY %s\r\n" % ' '.join(CAPABILITIES))
        return None

    #########################################################################
    #
    def do_namespace(self, imap_command):
        """
        We currently only support a single personal name space. No leading
        prefix is used on personal mailboxes and '/' is the hierarchy delimiter.

        Arguments:
        - `imap_command`: The full IMAP command object.
        """
        self.send_pending_expunges()
        self.client.push('* NAMESPACE (("" "/")) NIL NIL\r\n')
        return None

    #########################################################################
    #
    def do_id(self, imap_command):
        """
        Construct an ID response... uh.. lookup the rfc that defines this.

        Arguments:
        - `imap_command`: The full IMAP command object.
        """
        self.send_pending_expunges()
        self.client_id = imap_command.id_dict
        self.log.info("Client at %s:%d identified itself with: %s" % \
                          (self.client.rem_addr, self.client.port,
                           ", ".join("%s: '%s'" % x for x in self.client_id.iteritems())))
        res = []
        for k,v in SERVER_ID.iteritems():
            res.extend(['"%s"' % k,'"%s"' % v])
        self.client.push("* ID (%s)\r\n" % ' '.join(res))
        return None

    #########################################################################
    #
    def do_idle(self, imap_command):
        """
        The idle command causes the server to wait until the client sends
        us a 'DONE' continuation. During that time the client can not send
        any commands to the server. However, the client can still get
        asynchronous messages from the server.

        Arguments:
        - `imap_command`: The full IMAP command object.
        """
        # Because this is a blocking command the main server read-loop
        # for this connection is not going to hit the read() again
        # until this thread exits. In here we send a "+\r\n" to the client
        # indicating that we are now waiting for its continuation. We
        # then block reading on the connection. When we get a line
        # of input, if it is "DONE" then we complete this command
        # If it is any other input we raise a bad syntax error.
        #
        self.client.push("+ idling\r\n")
        self.send_pending_expunges()
        self.idling = True
        return False

    #########################################################################
    #
    def do_logout(self, imap_command):
        """
        This just sets our state to 'logged out'. Our caller will take the
        appropriate actions to finishing a client's log out request.

        Arguments:
        - `imap_command`: The full IMAP command object.
        """
        self.pending_expunges = []
        self.client.push("* BYE Logging out of asimap server. Good bye.\r\n")
        self.state = "logged_out"
        return None


##################################################################
##################################################################
#
class PreAuthenticated(BaseClientHandler):
    """
    This handles the server-side state for an IMAP client when they
    are in the states before they have successfully authenticated to
    the IMAP server.

    NOTE: This is the class used by the main server to handle an IMAP
          client as it authenticates. It does not handle any IMAP
          commands after the client enters the authenticated
          state. All of those are handled by the subprocess instation
          of the Authenticated class
    """

    ##################################################################
    #
    def __init__(self, client, auth_system):
        """
        Arguments:
        - `client`: An asynchat.async_chat object that is connected to the IMAP
                    client we are handling. This lets us send messages to that
                    IMAP client.
        - `auth_system`: The auth system we use to authenticate the IMAP client.
        """
        BaseClientHandler.__init__(self, client)
        self.name = "PreAuthenticated"
        self.log = logging.getLogger("%s.PreAuthenticated" % __name__)
        self.auth_system = auth_system
        self.user = None
        return

    ## The following commands are supported in the non-authenticated state.
    ##

    ##################################################################
    #
    def do_authenticated(self):
        """
        We do not support any authentication mechanisms at this time.. just
        password authentication via the 'login' IMAP client command.

        Arguments:
        - `imap_command`: The full IMAP command object.
        """
        self.send_pending_expunges()
        if self.state == "authenticated":
            raise Bad("client already is in the authenticated state")
        raise No("unsupported authentication mechanism")

    ##################################################################
    #
    def do_login(self, imap_command):
        """
        Process a LOGIN command with a username and password from the IMAP
        client.

        Arguments:
        - `imap_command`: The full IMAP command object.
        """

        # XXX This should poke the authentication mechanism we were passed
        #     to see if the user authenticated properly, and if they did
        #     determine what the path to the user's mailspool is.
        #
        #     But for our first test we are going to accept a test user
        #     and password.
        #
        self.send_pending_expunges()
        if self.state == "authenticated":
            raise Bad("client already is in the authenticated state")

        try:
            self.user = self.auth_system.authenticate(imap_command.user_name,
                                                      imap_command.password)

            # Even if the user authenticates properly, we can not allow them to
            # login if they have no maildir.
            #
            if not (os.path.exists(self.user.maildir) and \
                    os.path.isdir(self.user.maildir)):
                raise No("You have no mailbox directory setup")

            self.user.auth_system = self.auth_system
            self.state = "authenticated"
            self.log.info("%s logged in from %s:%d" % (str(self.user),
                                                       self.client.rem_addr,
                                                       self.client.port))
        except asimap.auth.AuthenticationException, e:
            raise No(str(e))
        return None

##################################################################
##################################################################
#
class Authenticated(BaseClientHandler):
    """
    The 'authenticated' client IMAP command handler. Basically this handles all
    of the IMAP messages from the IMAP client when they have authenticated and
    we are running in the user_server subprocess.

    This is basically the main command dispatcher for pretty much everything
    that the IMAP client is going to do.
    """

    ##################################################################
    #
    def __init__(self, client, user_server):
        """

        Arguments:
        - `client`: An asynchat.async_chat object that is connected to the IMAP
                    client we are handling. This lets us send messages to that
                    IMAP client.
        - `user_server`: A handle on the user server object (which holds the
                         handle to our sqlite3 db, etc.
        """
        BaseClientHandler.__init__(self, client)
        self.log = logging.getLogger("%s.%s.%d" % (__name__, self.__class__.__name__, client.port))
        self.server = user_server
        self.port = client.port # Used for debug messages
        self.name = "Client:%d" % client.port
        self.db = user_server.db
        self.mbox = None
        self.state = "authenticated"
        self.examine = False # If a mailbox is selected in 'examine' mode

        return

    ##################################################################
    #
    def process_or_queue(self, imap_cmd, queue = True):
        """
        When we have a mailbox selected we may be in a state where we can not
        process the command we have been handed. This happens when we have a
        non-zero command queue on the mailbox. In these cases the imap command
        is NOT processed and is instead added to the end of the mailbox's
        command queue for later processing.

        If that is the case we will return False.

        Otherwise we return True letting our caller know they can just continue
        with processing this command.

        Arguments:
        - `imap_cmd`: IMAP command about to be processed.
        - `queue`: If this is True then we _queue this command_ we are handed
          for later processing. It is the case that some commands can be
          immediately processed even if we are in the middle of processing
          another command and our caller knows this and tells us not to queue
          the command.
        """
        # If this imap command has 'needs_continuation' set then we are going
        # to assume that is the continuation of the command currently being
        # processed.
        #
        # If this imap coammnd DOES NOT have 'needs_continuation' set AND the
        # mailbox has a non-zero command_queue then we push this command on to
        # the end of the queue and return.
        #
        if imap_cmd.needs_continuation:
            return True
        if self.mbox is None:
            return True
        if len(self.mbox.command_queue) == 0:
            return True
        if queue:
            self.mbox.command_queue.append((self, imap_cmd))
        return False

    ##################################################################
    #
    def notifies(self):
        """
        Handles the common case of sending pending expunges and a resync where
        we only notify this client of exists/recent.
        """
        if self.state == "selected" and self.mbox is not None:
            self.mbox.resync(only_notify = self)
        self.send_pending_expunges()
        return

    #########################################################################
    #
    def do_noop(self, imap_command):
        """
        Do nothing.. but send any pending messages and do a resync.. but when
        doing a resync only send the exists/recent to us (the mailbox might
        have shrunk and if I am to understand the RFC correctly I can not send
        out exists/recents that shrink the size of a mailbox.)

        Arguments:
        - `imap_command`: The full IMAP command object.
        """

        # If we have a mailbox and we have commands in the command queue of
        # that mailbox then we can not do the notifies or expunges.
        #
        if self.mbox and not self.mbox.has_queued_commands(self):
            try:
                self.notifies()
            except MailboxLock:
                pass
        return None

    #########################################################################
    #
    def do_authenticate(self, cmd):
        self.notifies()
        raise Bad("client already is in the authenticated state")

    #########################################################################
    #
    def do_login(self, cmd):
        try:
            self.notifies()
        except MailboxLock:
            pass
        raise Bad("client already is in the authenticated state")

    ##################################################################
    #
    def do_select(self, cmd, examine = False):
        """
        Select a folder, enter in to 'selected' mode.

        Arguments:
        - `cmd`: The IMAP command we are executing
        - `examine`: Opens the folder in read only mode if True
        """
        self.log.debug("do_select(): mailbox: '%s', examine: %s" % \
                           (cmd.mailbox_name, examine))

        # NOTE: If this client currently has messages being processed in the
        # command queue for this mailbox then they are all tossed when they
        # pre-emptively select another mailbox (this could cause the client
        # some heartburn as commands they issues will never get their final
        # message.. but that is their problem.)
        #
        # Selecting a mailbox, even if the attempt fails, automatically
        # deselects any already selected mailbox.
        #
        self.pending_expunges = []
        if self.state == "selected":
            self.state = "authenticated"
            if self.mbox:
                self.mbox.unselected(self)
                self.mbox = None

        # Note the 'selected()' method may fail with an exception and
        # we should not set our state or the mailbox we have selected
        # until 'selected()' returns without a failure.
        #
        mbox = self.server.get_mailbox(cmd.mailbox_name)
        mbox.selected(self)
        self.mbox = mbox
        self.state = "selected"
        self.examine = examine
        if self.examine:
            return "[READ-ONLY]"
        return "[READ-WRITE]"

    ##################################################################
    #
    def do_unselect(self, cmd):
        """
        Unselect a mailbox. Similar to close, except it does not do an expunge.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        # NOTE: If this client currently has messages being processed in the
        # command queue for this mailbox then they are all tossed when they
        # pre-emptively select another mailbox (this could cause the client
        # some heartburn as commands they issues will never get their final
        # message.. but that is their problem.)
        #
        if self.state != "selected":
            raise No("Client must be in the selected state")

        if self.mbox:
            try:
                self.mbox.unselected(self)
            except MailboxLock:
                pass
            self.mbox = None
        self.pending_expunges = []
        self.state = "authenticated"
        return

    #########################################################################
    #
    def do_examine(self, cmd):
        """
        examine a specific mailbox (just like select, but read only)
        """
        return self.do_select(cmd, examine = True)

    ##################################################################
    #
    def do_create(self, cmd):
        """
        Create the specified mailbox.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        # You can create a mailbox while you have commands in the command
        # queue, but no notifies are sent in that case.
        #
        if self.process_or_queue(cmd, queue = False):
            try:
                self.notifies()
            except MailboxLock:
                pass
        asimap.mbox.Mailbox.create(cmd.mailbox_name, self.server)
        return

    ##################################################################
    #
    def do_delete(self, cmd):
        """
        Delete the specified mailbox.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        # You can delete a mailbox while you have commands in the command
        # queue, but no notifies are sent in that case.
        #
        if self.process_or_queue(cmd, queue = False):
            try:
                self.notifies()
            except MailboxLock:
                pass
        asimap.mbox.Mailbox.delete(cmd.mailbox_name, self.server)
        return

    ##################################################################
    #
    def do_rename(self, cmd):
        """
        Renames a mailbox from one name to another.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        # You can delete a mailbox while you have commands in the command
        # queue, but no notifies are sent in that case.
        #
        if self.process_or_queue(cmd, queue = False):
            try:
                self.notifies()
            except MailboxLock:
                pass
        try:
            asimap.mbox.Mailbox.rename(cmd.mailbox_src_name,
                                       cmd.mailbox_dst_name,
                                       self.server)
        except MailboxLock, e:
            raise Bad("unable to lock mailbox %s, try again" % e.mbox.name)
        return

    ##################################################################
    #
    def do_subscribe(self, cmd):
        """
        The SUBSCRIBE command adds the specified mailbox name to the
        server's set of "active" or "subscribed" mailboxes as returned by
        the LSUB command.  This command returns a tagged OK response only
        if the subscription is successful.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        # You can subscribe while you have commands in the command
        # queue, but no notifies are sent in that case.
        #
        if self.process_or_queue(cmd, queue = False):
            try:
                self.notifies()
            except MailboxLock:
                pass

        mbox = self.server.get_mailbox(cmd.mailbox_name)
        mbox.subscribed = True
        mbox.commit_to_db()
        return None

    ##################################################################
    #
    def do_unsubscribe(self, cmd):
        """
        The UNSUBSCRIBE command removes the specified mailbox name
        from the server's set of "active" or "subscribed" mailboxes as
        returned by the LSUB command.  This command returns a tagged
        OK response only if the unsubscription is successful.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        # You can unsubscribe while you have commands in the command
        # queue, but no notifies are sent in that case.
        #
        if self.process_or_queue(cmd, queue = False):
            try:
                self.notifies()
            except MailboxLock:
                pass
        mbox = self.server.get_mailbox(cmd.mailbox_name)
        mbox.subscribed = False
        mbox.commit_to_db()
        return None

    ##################################################################
    #
    def do_list(self, cmd, lsub = False):
        """
        The LIST command returns a subset of names from the complete
        set of all names available to the client.  Zero or more
        untagged LIST replies are returned, containing the name
        attributes, hierarchy delimiter, and name; see the description
        of the LIST reply for more detail.

        Arguments:
        - `cmd`: The IMAP command we are executing
        - `lsub`: If True this will only match folders that have their
          subscribed bit set.
        """
        # You can list while you have commands in the command
        # queue, but no notifies are sent in that case.
        #
        if self.process_or_queue(cmd, queue = False):
            try:
                self.notifies()
            except MailboxLock:
                pass
        # Handle the special case where the client is basically just probing
        # for the hierarchy sepration character.
        #
        if cmd.mailbox_name == "" and \
           cmd.list_mailbox == "":
            self.client.push('* LIST (\Noselect) "/" ""\r\n')
            return

        results = asimap.mbox.Mailbox.list(cmd.mailbox_name, cmd.list_mailbox,
                                           self.server, lsub)
        res = "LIST"
        if lsub:
            res = "LSUB"

        for mbox_name, attributes in results:
            if mbox_name.lower() == "inbox":
                mbox_name = "INBOX"
            self.client.push(str('* %s (%s) "/" %s\r\n' % \
                                 (res, ' '.join(attributes), mbox_name)))
        return None

    ####################################################################
    #
    def do_lsub(self, cmd):
        """
        The lsub command lists mailboxes we are subscribed to with the
        'SUBSCRIBE' command. Since we do not support subscribing to
        mailboxes, this list will always be empty, no?

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        return self.do_list(cmd, lsub = True)

    ##################################################################
    #
    def do_status(self, cmd):
        """
        Get the designated mailbox and return the requested status
        attributes to our client.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        # You can lsub while you have commands in the command
        # queue, but no notifies are sent in that case.
        #
        if self.process_or_queue(cmd, queue = False):
            try:
                self.notifies()
            except MailboxLock:
                pass

        mbox = self.server.get_mailbox(cmd.mailbox_name, expiry = 45)

        # We can only call resync on this mbox if it has an empty
        # command queue.
        #
        if len(mbox.command_queue) == 0:
            mbox.resync()

        result = []
        for att in cmd.status_att_list:
            if att == "messages":
                result.append("MESSAGES %d" % mbox.num_msgs)
            elif att == "recent":
                result.append("RECENT %d" % mbox.num_recent)
            elif att == "uidnext":
                result.append("UIDNEXT %d" % mbox.next_uid)
            elif att == "uidvalidity":
                result.append("UIDVALIDITY %d" % mbox.uid_vv)
            elif att == "unseen":
                if 'unseen' in mbox.sequences:
                    result.append("UNSEEN %d" %  len(mbox.sequences['unseen']))
                else:
                    result.append("UNSEEN 0")
            else:
                raise Bad("Unsupported STATUS attribute '%s'" % att)

        self.client.push("* STATUS %s (%s)\r\n" % \
                             (cmd.mailbox_name," ".join(result)))
        return

    ##################################################################
    #
    def do_append(self, cmd):
        """
        Append a message to a mailbox.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        # You can append while you have commands in the command
        # queue, but no notifies are sent in that case.
        #
        if self.process_or_queue(cmd, queue = False):
            try:
                self.notifies()
            except MailboxLock:
                pass

        try:
            mbox = self.server.get_mailbox(cmd.mailbox_name, expiry = 0)
            # We can only call resync on this mbox if it has an empty
            # command queue.
            #
            if len(mbox.command_queue) == 0:
                mbox.resync()
            mbox.append(cmd.message, cmd.flag_list, cmd.date_time)
        except asimap.mbox.NoSuchMailbox:
            # For APPEND and COPY if the mailbox does not exist we
            # MUST supply the TRYCREATE flag so we catch the generic
            # exception and return the appropriate NO result.
            #
            raise No("[TRYCREATE] No such mailbox: '%s'" % cmd.mailbox_name)
        return

    ##################################################################
    #
    def do_check(self, cmd):
        """
        state: must be selected

        Do a 'checkpoint' of the currently selected mailbox. Basically
        this means for us we just do a resync.

        This may cause messages to be generated but this is
        okay. Clients should be prepared for that (but they should not
        expect this to happen.)

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != "selected":
            raise No("Client must be in the selected state")

        # If self.mbox is None then this mailbox was deleted while this user
        # had it selected. In that case we disconnect the user and let them
        # reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            self.unceremonious_bye("Your selected mailbox no longer exists")
            return

        # We can only do a resync if there are no commands in the command
        # queue. We can only send expunges to our client if it does not have
        # commands in the mailbox's command queue.
        #
        if self.process_or_queue(cmd, queue = False):
            self.mbox.resync()

        if not self.mbox.has_queued_commands(self):
            self.send_pending_expunges()
        return

    ##################################################################
    #
    def do_close(self, cmd):
        """
        state: must be selected

        The CLOSE command permanently removes all messages that have
        the \Deleted flag set from the currently selected mailbox, and
        returns to the authenticated state from the selected state.
        No untagged EXPUNGE responses are sent.

        No messages are removed, and no error is given, if the mailbox is
        selected by an EXAMINE command or is otherwise selected read-only.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != "selected":
            raise No("Client must be in the selected state")

        # We allow for the mailbox to be deleted.. it has no effect on this
        # operation.
        #
        self.pending_expunges = []
        self.state = "authenticated"
        mbox = None
        try:
            if self.mbox:
                self.mbox.unselected(self)
                mbox = self.mbox
                self.mbox = None

            # If the mailbox was selected via 'examine' then closing the
            # mailbox does NOT do a purge of all messages marked with '\Delete'
            #
            if self.examine:
                return

            # Otherwise closing the mailbox (unlike doing a 'select' 'examine'
            # or 'logout') will perform an expunge (just no messages will be
            # sent to this client.) We pass no client parameter so the expunge
            # does its work 'silently.'
            #
            if mbox:
                mbox.resync()
                mbox.expunge()
        except MailboxUnlock:
            pass
        return

    ##################################################################
    #
    def do_expunge(self, cmd):
        """
        Delete all messages marked with '\Delete' from the mailbox and send out
        untagged expunge messages...

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        # If there are commands pending in the queue this gets put on the queue
        # waiting for those to be finished before processing.
        #
        if not self.process_or_queue(cmd):
            return False

        if self.state != "selected":
            raise No("Client must be in the selected state")

        # If self.mbox is None then this mailbox was deleted while this user
        # had it selected. In that case we disconnect the user and let them
        # reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            self.unceremonious_bye("Your selected mailbox no longer exists")
            return

        self.send_pending_expunges()

        # If we selected the mailbox via 'examine' then we can not make any
        # changes anyways...
        #
        if self.examine:
            return

        self.mbox.expunge(self)
        return

    ##################################################################
    #
    def do_search(self, cmd):
        """
        Search... NOTE: Can not send untagged EXPUNGE messages during this
        command.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != "selected":
            raise No("Client must be in the selected state")

        # If there are commands pending in the queue this gets put on the queue
        # waiting for those to be finished before processing.
        #
        if not self.process_or_queue(cmd):
            return False

        # If self.mbox is None then this mailbox was deleted while this user
        # had it selected. In that case we disconnect the user and let them
        # reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            self.unceremonious_bye("Your selected mailbox no longer exists")
            return

        # If this client has pending EXPUNGE messages then we return a tagged
        # No response.. the client should see this and do a NOOP or such and
        # receive the pending expunges. Unless this is a UID command. It is
        # okay to send pending expunges during the operations of a UID SEARCH.
        #
        self.mbox.resync(notify = cmd.uid_command)
        if len(self.pending_expunges) > 0:
            if cmd.uid_command:
                self.send_pending_expunges()
            else:
                raise No("There are pending EXPUNGEs.")

        count = 0
        success = False
        while not success:
            try:
                count += 1
                results = self.mbox.search(cmd.search_key, cmd)
                if len(results) > 0:
                    self.client.push("* SEARCH %s\r\n" % \
                                         ' '.join([str(x) for x in results]))
                break
            except MailboxInconsistency, e:
                self.server.msg_cache.clear_mbox(self.mbox.name)
                self.log.warn("do_search: %s, "
                              "Try %d" % (str(e),count))
                if count > 5:
                    raise e
                self.mbox.resync(notify = False, optional = False)

        # If 'needs_continuation' is True then we have actually only partially
        # processed this command. We push this command on to the end of the
        # command_queue for this folder. It will get picked off and processed
        # later through the event loop. The command itself keeps track of where
        # it is in terms of processing.
        #
        if cmd.needs_continuation:
            self.mbox.command_queue.append((self, cmd))
            return False
        return None

    ##################################################################
    #
    def _fetch_internal(self, cmd, count):
        """
        The internal part of the 'do_fetch' command that can fail with a
        MailboxInconsistency exception such that if we hit that except we try
        this command again after a resync.

        Arguments:
        - `cmd`: The IMAP command being processed
        - `count`: Number of times we have been called. If more than 1 we force
          the resync.

        """
        force = False
        optional = True
        if count > 1:
            # force = True
            optional = False

        self.mbox.resync(notify = cmd.uid_command, force = force,
                         optional = optional)
        results,seq_changed = self.mbox.fetch(cmd.msg_set, cmd.fetch_atts,
                                              cmd)

        for r in results:
            idx, iter_results = r
            self.client.push("* %d FETCH (%s)\r\n" % \
                                 (idx, " ".join(iter_results)))

        return seq_changed

    ##################################################################
    #
    def do_fetch(self, cmd):
        """
        Fetch data from the messages indicated in the command.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != "selected":
            raise No("Client must be in the selected state")

        # If self.mbox is None then this mailbox was deleted while this user
        # had it selected. In that case we disconnect the user and let them
        # reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            self.unceremonious_bye("Your selected mailbox no longer exists")
            return

        # If there are commands pending in the queue this gets put on the queue
        # waiting for those to be finished before processing.
        #
        if not self.process_or_queue(cmd):
            return False

        # If this client has pending EXPUNGE messages then we return a tagged
        # No response.. the client should see this and do a NOOP or such and
        # receive the pending expunges. Unless this is a UID command. It is
        # okay to send pending expunges during the operations of a UID FETCH.
        #
        if len(self.pending_expunges) > 0:
            if cmd.uid_command:
                self.send_pending_expunges()
            else:
                raise No("There are pending EXPUNGEs.")

        success = False
        count = 0
        while not success:
            try:
                count += 1
                seq_changed = self._fetch_internal(cmd, count)
                break
            except MailboxInconsistency, e:
                self.server.msg_cache.clear_mbox(self.mbox.name)
                self.log.warn("do_fetch: %s, "
                              "Try %d" % (str(e),count))
                if count > 5:
                    raise e

        # If the fetch caused sequences to change then we need to make the
        # resync non-optional so that we will send FETCH messages to the other
        # clients listening to this mailbox.
        #
        if seq_changed:
            self.mbox.resync(optional = False)

        # If 'needs_continuation' is True then we have actually only partially
        # processed this command. We push this command on to the end of the
        # command_queue for this folder. It will get picked off and processed
        # later through the event loop. The command itself keeps track of where
        # it is in terms of processing.
        #
        if cmd.needs_continuation:
            self.mbox.command_queue.append((self, cmd))
            return False

        return None

    ##################################################################
    #
    def do_store(self, cmd):
        """
        The STORE command alters data associated with a message in the
        mailbox.  Normally, STORE will return the updated value of the
        data with an untagged FETCH response.  A suffix of ".SILENT" in
        the data item name prevents the untagged FETCH, and the server
        SHOULD assume that the client has determined the updated value
        itself or does not care about the updated value.

        By data we mean the flags on a message.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != "selected":
            raise No("Client must be in the selected state")

        # If self.mbox is None then this mailbox was deleted while this user
        # had it selected. In that case we disconnect the user and let them
        # reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            self.unceremonious_bye("Your selected mailbox no longer exists")
            return

        # If there are commands pending in the queue this gets put on the queue
        # waiting for those to be finished before processing.
        #
        if not self.process_or_queue(cmd):
            return False

        # If this client has pending EXPUNGE messages then we return a tagged
        # No response.. the client should see this and do a NOOP or such and
        # receive the pending expunges.  Unless this is a UID command. It is
        # okay to send pending expunges during the operations of a UID FETCH.
        #
        if len(self.pending_expunges) > 0:
            if cmd.uid_command:
                self.send_pending_expunges()
            else:
                raise No("There are pending EXPUNGEs.")

        self.mbox.resync(notify = cmd.uid_command)

        # We do not issue any messages to the client here. This is done
        # automatically when 'resync' is called because resync will examine the
        # in-memory copy of the sequences with what is on disk and if there are
        # differences issue FETCH messages for each message with different
        # flags.
        #
        # Unless 'SILENT' was set in which case we still notify all other
        # clients listening to this mailbox, but not this client.
        #
        self.mbox.store(cmd.msg_set, cmd.store_action, cmd.flag_list,
                        cmd)
        if cmd.silent:
            self.mbox.resync(notify = False, dont_notify = self,
                             publish_uids = cmd.uid_command)
        else:
            self.mbox.resync(notify = False, publish_uids = cmd.uid_command)

        # If 'needs_continuation' is True then we have actually only partially
        # processed this command. We push this command on to the end of the
        # command_queue for this folder. It will get picked off and processed
        # later through the event loop. The command itself keeps track of where
        # it is in terms of processing.
        #
        if cmd.needs_continuation:
            self.mbox.command_queue.append((self, cmd))
            return False

        return

    ##################################################################
    #
    def do_copy(self, cmd):
        """
        Copy the given set of messages to the destination mailbox.

        NOTE: Causes a resync of the destination mailbox.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != "selected":
            raise No("Client must be in the selected state")

        # If self.mbox is None then this mailbox was deleted while this user
        # had it selected. In that case we disconnect the user and let them
        # reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            self.unceremonious_bye("Your selected mailbox no longer exists")
            return

        # The copy can be done immediately. However, we can not send pending
        # expunges if this client has queued commands.
        #
        if not self.mbox.has_queued_commands(self):
            self.send_pending_expunges()

        try:
            dest_mbox = self.server.get_mailbox(cmd.mailbox_name, expiry = 0)
            try:
                # dest_mbox.mailbox.lock()
                self.mbox.copy(cmd.msg_set, dest_mbox, cmd.uid_command)
                dest_mbox.resync()
            finally:
                # dest_mbox.mailbox.unlock()
                pass
        except asimap.mbox.NoSuchMailbox:
            # For APPEND and COPY if the mailbox does not exist we
            # MUST supply the TRYCREATE flag so we catch the generic
            # exception and return the appropriate NO result.
            #
            raise No("[TRYCREATE] No such mailbox: '%s'" % cmd.mailbox_name)
        finally:
            # If 'needs_continuation' is True then we have actually only
            # partially processed this command. We push this command on to the
            # end of the command_queue for this folder. It will get picked off
            # and processed later through the event loop. The command itself
            # keeps track of where it is in terms of processing.
            #
            if cmd.needs_continuation:
                self.mbox.command_queue.append((self, cmd))
                return False
                
        return



