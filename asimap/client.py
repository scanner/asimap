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
from asimap.exceptions import No, Bad

# Local constants
#
CAPABILITIES = ('IMAP4rev1', 'IDLE', 'NAMESPACE', 'ID', 'UIDPLUS')
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

        # Before anything else if we are idling then we only accept
        # a DONE continuation from the IMAP client. Everything else
        # it sends us a is a BAD command.
        #
        # XXX This is not how this is going to work.. need to revisit
        #     this when we actually get around to implementing 'IDLE'
        #
        if self.idling and imap_command.command != "done":
            self.client.push("%s BAD Expected 'DONE' not: %s\r\n" % \
                                 (imap_command.tag,
                                  imap_command.command))
            return
                
        # Since the imap command was properly parsed we know it is a valid
        # command. If it is one we support there will be a method
        # on this object of the format "do_%s" that will actually do the
        # command.
        #
        # If no such method exists then this is not a supported command.
        #
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
        else:
            self.client.push("%s OK %s %s completed\r\n" % \
                                 (imap_command.tag, result,
                                  imap_command.command.upper()))
        return

    ## The following commands are supported in any state.
    ##

    #########################################################################
    #
    def do_capability(self, imap_command):
        """
        Return the capabilities of this server.

        Arguments:
        - `imap_command`: The full IMAP command object.
        """
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
        self.idling = True

    #########################################################################
    #
    def do_noop(self, imap_command):
        """
        This does nothing. In subclasses we might want to see if there are any
        pending messages to send the client (but that should not be necessary
        since our server will of its own accord send async messages to the
        client when various things happen.

        Arguments:
        - `imap_command`: The full IMAP command object.
        """
        return None

    #########################################################################
    #
    def do_logout(self, imap_command):
        """
        This just sets our state to 'logged out'. Our caller will take the
        appropriate actions to finishing a client's log out request.

        Arguments:
        - `imap_command`: The full IMAP command object.
        """
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
        except auth.AuthenticationException, e:
            raise No(str(e.value))
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

    #########################################################################
    #
    def do_authenticate(self, cmd):
        raise Bad("client already is in the authenticated state")

    #########################################################################
    #
    def do_login(self, cmd):
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
        asimap.mbox.Mailbox.rename(cmd.mailbox_src_name,cmd.mailbox_dst_name,
                                   self.server)
        return
