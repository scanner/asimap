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

# asimapd imports
#
import asimap.mbox
import asimap.auth
from asimap.exceptions import No, Bad

# Local constants
#
#CAPABILITIES = ('IMAP4rev1', 'IDLE', 'NAMESPACE', 'ID', 'UIDPLUS')
CAPABILITIES = ('IMAP4rev1', 'IDLE', 'ID')
SERVER_ID = { 'name'        : 'asimapd',
              'version'     : '0.1',
              'vendor'      : 'Apricot Systematic',
              # 'support-url' : 'http://trac.apricot.com/py-mh-imap',
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
        self.log = logging.getLogger("%s.BaseClientHandler" % __name__)
        self.client = client
        self.state = None

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
        self.log.debug("Processing received IMAP command: %s %s" % \
                           (imap_command.tag, imap_command.command))


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
        try:
            result = getattr(self, 'do_%s' % imap_command.command)(imap_command)
        except No, e:
            self.client.push("%s NO %s\r\n" % (imap_command.tag, str(e)))
            return
        except Bad, e:
            self.client.push("%s BAD %s\r\n" % (imap_command.tag, str(e)))
            return
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception, e:
            self.client.push("%s BAD Unhandled exception: %s\r\n" % \
                                 (imap_command.tag, str(e)))
            raise

        # If there was no result from running this command then everything went
        # okay and we send back a final 'OK' to the client for processing this
        # command.
        #
        if result is None:
            self.client.push("%s OK %s completed\r\n" % \
                                 (imap_command.tag,
                                  imap_command.command.upper()))
        elif result is False:
            # Some commands do NOT send an OK response immediately.. aka the
            # IDLE command. If result is false then we just return.
            #
            return
        else:
            # The command has some specific response it wants to send back as
            # part of the tagged OK response.
            #
            self.client.push("%s OK %s %s completed\r\n" % \
                                 (imap_command.tag, result,
                                  imap_command.command.upper()))
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
        self.log = logging.getLogger("%s.PreAuthenticated" % __name__)
        self.auth_system = auth_system
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
        self.log = logging.getLogger("%s.Authenticated" % __name__)
        self.server = user_server
        self.db = user_server.db
        self.mbox = user_server.mailbox
        self.state = "authenticated"
        self.examine = False # If a mailbox is selected in 'examine' mode

        return

    ##################################################################
    #
    def notifies(self):
        """
        Handles the common case of sending pending expunges and a resync where
        we only notify this client of exists/recent.
        """
        self.send_pending_expunges()
        if self.state == "selected" and self.mbox is not None:
            self.mbox.resync(only_notify = self)
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
        self.notifies()
        return None

    #########################################################################
    #
    def do_authenticate(self, cmd):
        self.notifies()
        raise Bad("client already is in the authenticated state")

    #########################################################################
    #
    def do_login(self, cmd):
        self.notifies()
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

        # Selecting a mailbox, even if the attempt fails, automatically
        # deselects any already selected mailbox.
        #
        self.pending_expunges = []
        if self.state == "selected":
            self.state = "authenticated"
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
        self.notifies()
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
        # IF we are deleting the mailbox we currently have selected then we pop
        # back to the authenticated state.
        #
        if self.mbox is not None and self.mbox.name == cmd.mailbox_name:
            self.mbox = None
            self.state = "authenticated"
        else:
            self.notifies()
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
        self.notifies()
        asimap.mbox.Mailbox.rename(cmd.mailbox_src_name,cmd.mailbox_dst_name,
                                   self.server)
        return

    ##################################################################
    #
    def do_subscribe(self, cmd):
        """
        The SUBSCRIBE command adds the specified mailbox name to the
        server's set of "active" or "subscribed" mailboxes as returned by
        the LSUB command.  This command returns a tagged OK response only
        if the subscription is successful.

        XXX we do not have any mailboxes that we support SUBSCRIBE for
            (as I understand the purpose of this mailbox.)

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        self.notifies()
        raise No("Can not subscribe to the mailbox %s" % cmd.mailbox_name)
    
    ##################################################################
    #
    def do_unsubscribe(self, cmd):
        """
        The UNSUBSCRIBE command removes the specified mailbox name
        from the server's set of "active" or "subscribed" mailboxes as
        returned by the LSUB command.  This command returns a tagged
        OK response only if the unsubscription is successful.
        
        XXX we do not have any mailboxes that we support SUBSCRIBE or
            UNSUBSCRIBE for (as I understand the purpose of this
            mailbox.)

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        self.notifies()
        raise No("Can not unsubscribe to the mailbox %s" % cmd.mailbox_name)

    ##################################################################
    #
    def do_list(self, cmd):
        """
        The LIST command returns a subset of names from the complete
        set of all names available to the client.  Zero or more
        untagged LIST replies are returned, containing the name
        attributes, hierarchy delimiter, and name; see the description
        of the LIST reply for more detail.
        
        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        # Handle the special case where the client is basically just probing
        # for the hierarchy sepration character.
        #
        self.notifies()
        if cmd.mailbox_name == "" and \
           cmd.list_mailbox == "":
            self.client.push('* LIST (\Noselect) "/" ""\r\n')
            return

        results = asimap.mbox.Mailbox.list(cmd.mailbox_name, cmd.list_mailbox,
                                           self.server)
        for mbox_name, attributes in results:
            self.client.push('* LIST (%s) "/" %s\r\n' % \
                                 (' '.join(attributes), mbox_name))
        return

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
        self.notifies()
        return None
    
    ##################################################################
    #
    def do_status(self, cmd):
        """
        Get the designated mailbox and return the requested status
        attributes to our client.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        self.notifies()
        mbox = self.server.get_mailbox(cmd.mailbox_name, expiry = 45)
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
        self.send_pending_expunges()
        try:
            mbox = self.server.get_mailbox(cmd.mailbox_name, expiry = 0)
        except asimap.mbox.NoSuchMailbox:
            # For APPEND and COPY if the mailbox does not exist we
            # MUST supply the TRYCREATE flag so we catch the generic
            # exception and return the appropriate NO result.
            #
            raise No("[TRYCREATE] No such mailbox: '%s'" % cmd.mailbox_name)
        
        mbox.append(cmd.message, cmd.flag_list, cmd.date_time)
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
        if self.state != "selected" or self.mbox is None:
            raise No("Client must be in the selected state")
        self.send_pending_expunges()
        self.mbox.resync()
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
        if self.state != "selected" or self.mbox is None:
            raise No("Client must be in the selected state")

        self.mbox.unselected(self)
        self.pending_expunges = []
        mbox = self.mbox
        self.mbox = None
        self.state = "authenticated"

        # If the mailbox was selected via 'examine' then closing the mailbox
        # does NOT do a purge of all messages marked with '\Delete'
        #
        if self.examine:
            return

        # Otherwise closing the mailbox (unlike doing a 'select' 'examine' or
        # 'logout') will perform an expunge (just no messages will be sent to
        # this client.) We pass no client parameter so the expunge does its
        # work 'silently.'
        #
        mbox.expunge()
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
        if self.state != "selected" or self.mbox is None:
            raise No("Client must be in the selected state")
        
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
        if self.state != "selected" or self.mbox is None:
            raise No("Client must be in the selected state")
        results = self.mbox.search(self.imap_command.search_key)
        self.client.push("* SEARCH %s\r\n" % ' '.join([str(x) for x in results]))
        return None
        
    
