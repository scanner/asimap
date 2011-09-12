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

# asimapd imports
#

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

#######################################################################
#
# We have some basic exceptions used during the processing of commands
# to determine how we respond in exceptional situations
#
class No(Exception):
    def __init__(self, value = "no"):
        self.value = value
    def __str__(self):
        return repr(self.value)

class Bad(Exception):
    def __init__(self, value = "bad"):
        self.value = value
    def __str__(self):
        return repr(self.value)

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

        # Before anything else if we are idling then we only accept
        # a DONE continuation from the IMAP client. Everything else
        # it sends us a is a BAD command.
        #
        # XXX This is not how this is going to work.. need to revisit
        #     this when we actually get around to implementing 'IDLE'
        #
        if idling and imap_command.command != "done":
            self.client.push("%s BAD Expected 'DONE' not: %s\r\n" % \
                                 (self.imap_command.tag,
                                  self.imap_command.command))
            return
                
        # Since the imap command was properly parsed we know it is a valid
        # command. If it is one we support there will be a method
        # on this object of the format "do_%s" that will actually do the
        # command.
        #
        # If no such method exists then this is not a supported command.
        #
        if not hasattr(self, 'do_%s' % self.imap_command.command):
            self.client.push("%s BAD Sorry, %s is not a supported "
                             "command\r\n" % (self.imap_command.tag,
                                              self.imap_command.command))
            return

        # Okay. The command was a known command. Process it. Each 'do_' method
        # will send any messages back to the client specific to that command
        # except the "OK" response and any exceptional errors which are handled
        # by this method.
        #
        try:
            result = getattr(self, 'do_%s' % self.imap_command.command)()
        except No, e:
            self.client.push("%s NO %s\r\n" % (self.imap_command.tag, str(e)))
            return
        except Bad, e:
            self.client.push("%s BAD %s\r\n" % (self.imap_command.tag, str(e)))
            return
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception, e:
            self.client.push("%s BAD Unhandled exception: %s\r\n" % \
                                 (self.imap_command.tag, str(e)))
            raise

        # If there was no result from running this command then everything went
        # okay and we send back a final 'OK' to the client for processing this
        # command.
        #
        if result is None:
            self.client.push("%s OK %s completed\r\n" % \
                                 (self.imap_command.tag,
                                  self.imap_command.command.upper()))
        else:
            self.client.push("%s OK %s %s completed\r\n" % \
                                 (self.imap_command.tag, result,
                                  self.imap_command.command.upper()))
        return

    ## The following commands are supported in any state.
    ##

    #########################################################################
    #
    def do_capability(self):
        self.client.push("* CAPABILITY %s\r\n" % ' '.join(CAPABILITIES))
        return None

    #########################################################################
    #
    def do_namespace(self):
        """
        We currently only support a single personal name space. No leading
        prefix is used on personal mailboxes and '/' is the hierarchy delimiter.
        """
        self.client.push('* NAMESPACE (("" "/")) NIL NIL\r\n')
        return None

    #########################################################################
    #
    def do_id(self):
        self.client.id = self.imap_command.id_dict
        res = []
        for k,v in SERVER_ID.iteritems():
            res.extend(['"%s"' % k,'"%s"' % v])
        self.client.push("* ID (%s)\r\n" % ' '.join(res))
        return None

    #########################################################################
    #
    def do_idle(self):
        """
        The idle command causes the server to wait until the client sends
        us a 'DONE' continuation. During that time the client can not send
        any commands to the server. However, the client can still get
        asynchronous messages from the server.
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
    def do_noop(self):
        """
        This does nothing. In subclasses we might want to see if there are any
        pending messages to send the client (but that should not be necessary
        since our server will of its own accord send async messages to the
        client when various things happen.
        """
        return None

    #########################################################################
    #
    def do_logout(self):
        """
        This just sets our state to 'logged out'. Our caller will take the
        appropriate actions to finishing a client's log out request.
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
    def __init__(self, client):
        """
        Arguments:
        - `client`: An asynchat.async_chat object that is connected to the IMAP
                    client we are handling. This lets us send messages to that
                    IMAP client.
        """
        BaseClientHandler.__init__(self, client)
        self.log = logging.getLogger("%s.PreAuthenticated" % __name__)
        return
    
    ## The following commands are supported in the non-authenticated state.
    ##

    ##################################################################
    #
    def do_authenticated(self, ):
        """
        We do not support any authentication mechanisms at this time.. just
        password authentication via the 'login' IMAP client command.
        """
        if self.state == "authenticated":
            raise Bad("client already is in the authenticated state")
        raise No("unsupported authentication mechanism")

    ##################################################################
    #
    def do_login(self):
        """
        Process a LOGIN command with a username and password from the IMAP
        client.
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

        # try:
        #     user = self.auth_system.authenticate(self.imap_command.user_name,
        #                                          self.imap_command.password)
        # except AuthenticationException, e:
        #     raise No(str(e.value))

        # self.state = "authenticated"
        # self.user = user
        if self.imap_command.user_name == "test" and \
                self.imap_command.password == "test":
            self.user = "test"
            self.state = "authenticated"
        else:
            raise No("Bad login")
        return None
    
        
        
