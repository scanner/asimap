############################################################################
#
# File: $Id: IMAPProcess.py 1460 2007-12-18 08:25:00Z scanner $
#
# Copyright (C) 2006 Eric "Scanner" Luce
#
# Author: Scanner
#
"""This module contains the classes that process an IMAP command.
"""

# System imports
#
import sys
import os
import os.path
import threading
import mhlib

# mhimap imports
#
import mhimap.Client
import mhimap.Mailbox

from mhimap.Auth import AuthenticationException
from mhimap.rfc2060_constants import system_flags, non_settable_flags

# Constants used by this and other modules
#
CAPABILITIES = ('IMAP4rev1', 'IDLE', 'NAMESPACE', 'ID', 'UIDPLUS')
SERVER_ID = { 'name'        : 'py-mh-imap',
              'version'     : '0.1',
              'vendor'      : 'Apricot Systematic',
              'support-url' : 'http://trac.apricot.com/py-mh-imap',
              'command'     : sys.argv[0],
              'os'          : sys.platform,
              }

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
    

#######################################################################
#
class BaseIMAPCommandProcessor(threading.Thread):
    """The abstract base IMAP command processor. It does not process any
    commands. It servers as the base class for various kinds of IMAP command
    processors.

    You will instantiate some sub-class of this class for every command you
    wish to process. This class is a sub-class of a Thread. Thus, after you
    instantiate it you invoke its 'start()' method to begin processing.
    """

    #######################################################################
    #
    def __init__(self, client, imap_command, name = "IMAPCommandProcessor"):
        # Since we are a subclass of thread we are required by the thread class
        # to inoke its __init__ method before we do anything else.
        #
        threading.Thread.__init__(self)
        self.client = client
        self.conn = client.conn
        self.imap_command = imap_command
        self.setName(name)
        self.logged_out = False

        # If a command is 'blocking' that means it needs to finish its
        # processing before the server reads any more input from the client.
        # This is currently only used by commands that themselves require
        # additional data from the client (and thus will use this to grab the
        # connection's reading socket. Each sub-class of this class will
        # need to determine if it is processing a blocking command
        # in the __init__() method.
        #
        self.blocking = False

        # The 'idle' command is a blocking command.
        #
        if self.imap_command.command == 'idle':
            self.blocking = True
        return

    #########################################################################
    #
    def run(self):
        """This method is invoked as the top of a new thread of execution.
        This is where we actually process the IMAPCommand
        """
#        print "Processing: %s" % str(self.imap_command)

        # Since the imap command was properly parsed we know it is a valid
        # command. If it is one we support there will be a method
        # on this object of the format "do_%s" that will actually do the
        # command.
        #
        # If no such method exists then this is not a supported command.
        #
        if not hasattr(self, 'do_%s' % self.imap_command.command):
            self.conn.write("%s BAD Sorry, %s is not a supported " \
                            "command\r\n" % (self.imap_command.tag,
                                             self.imap_command.command))
            return

        # Okay. The command was a known command. Process it.
        #
        try:
            result = getattr(self, 'do_%s' % self.imap_command.command)()
        except No, e:
            self.conn.write("%s NO %s\r\n" % (self.imap_command.tag, str(e)))
            return
        except Bad, e:
            self.conn.write("%s BAD %s\r\n" % (self.imap_command.tag, str(e)))
            return
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception, e:
            self.conn.write("%s BAD Unhandled exception: %s\r\n" % \
                            (self.imap_command.tag, str(e)))
            raise

        # Be sure to post any pending messages for this client. If we were
        # processing a STORE, FETCH, or SEARCH be sure NOT to send expunges.
        #
        if self.client:
            if self.imap_command.command in ('search', 'fetch', 'store',
                                             'close', 'logout'):
                self.client.post_pending_notifies(expunges = False)
            else:
                self.client.post_pending_notifies()

        # Otherwise everything was fine.
        #
        if result is None:
            self.conn.write("%s OK %s completed\r\n" % \
                            (self.imap_command.tag,
                             self.imap_command.command.upper()))
        else:
            self.conn.write("%s OK %s %s completed\r\n" % \
                            (self.imap_command.tag, result,
                             self.imap_command.command.upper()))

        # If after processing our message we are supposed to be logged out
        # then close our connection. This should cause all the associated
        # objects to be released and GC'd.
        #
        if self.logged_out:
            self.conn.shutdown()
        return

    #########################################################################
    #
    def do_capability(self):
        self.conn.write("* CAPABILITY %s\r\n" % ' '.join(CAPABILITIES))
        return None

    #########################################################################
    #
    def do_namespace(self):
        """We currently only support a single personal name space. No leading
        prefix is used on personal mailboxes and '/' is the hierarchy delimiter.
        """
        self.conn.write('* NAMESPACE (("" "/")) NIL NIL\r\n')
        return None

    #########################################################################
    #
    def do_id(self):
        self.client.id = self.imap_command.id_dict
        res = []
        for k,v in SERVER_ID.iteritems():
            res.extend(['"%s"' % k,'"%s"' % v])
        self.conn.write("* ID (%s)\r\n" % ' '.join(res))
        return None

    #########################################################################
    #
    def do_idle(self):
        """The idle command causes the server to wait until the client sends
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
        self.conn.write("+ idling\r\n")
        self.client.idling = True
        if self.client.selected():
            self.client.post_pending_notifies()
        line = self.conn.rfile.readline().lower()
        if line[:4] == 'done':
            self.client.idling = False
            return None
        else:
            raise Bad("Expected 'DONE' (not: '%s')" % line)

    #########################################################################
    #
    def do_noop(self):
        """Right now this does nothing. We should probably collect any
        status messages that there might be and take this chance to send them
        to the client.
        """
        return None

    #########################################################################
    #
    def do_logout(self):
        self.client.shutdown()
        self.conn.write("* BYE pymap server logging out\r\n")
        self.logged_out = True
        self.client = None
        return None

#######################################################################
#
class PasswordPreAuthenticatedIMAPCommandProcessor(BaseIMAPCommandProcessor):
    """This IMAP command processor handles all of pre-authenticated
    IMAP commands running inside our test server.
    """
    #######################################################################
    #
    def __init__(self, client, imap_command, auth_system,
                 name = "IMAPCommandProcessor"):
        """The caller must pass in an auth system to use. We expect this
        auth system to have an 'authenticate' method that takes a username and
        password and returns a User object (or raises an authentication
        exception.)

        The client object must not be authenticated.
        """
        BaseIMAPCommandProcessor.__init__(self, client, imap_command, name)
        if self.client.authenticated():
            raise Bad("client already is in the authenticated state")
        self.auth_system = auth_system

    #########################################################################
    #
    def do_authenticate(self):
        # we do not support any authentication mechanisms at this time.
        #
        if self.client.authenticated():
            raise Bad("client already is in the authenticated state")
        raise No("unsupported authentication mechanism")

    #########################################################################
    #
    def do_login(self):
        if self.client.authenticated():
            raise Bad("client already is in the authenticated state")

        try:
            user = self.auth_system.authenticate(self.imap_command.user_name,
                                                 self.imap_command.password)
        except AuthenticationException, e:
            raise No(str(e.value))

        self.client.lock.acquire()
        self.client.state = mhimap.Client.AUTHENTICATED
        self.client.user = user
        self.client.lock.release()
        return None
    
#######################################################################
#
class AuthenticatedIMAPCommandProcessor(BaseIMAPCommandProcessor):
    """This IMAP command processor is what handles all the authenticated and
    selected client state commands.
    """

    #######################################################################
    #
    def __init__(self, client, imap_command, usermhdir,
                 name = "IMAPCommandProcessor"):
        BaseIMAPCommandProcessor.__init__(self, client, imap_command, name)
        self.usermhdir = usermhdir
        
    #########################################################################
    #
    def do_authenticate(self):
        raise Bad("client already is in the authenticated state")

    #########################################################################
    #
    def do_login(self):
        raise Bad("client already is in the authenticated state")

    #########################################################################
    #
    def do_select(self, examine = False):
        """select a specific mailbox. If 'examine' is True then we treat
        this as an EXAMINE message. ie: readonly mailbox.. 
        """
        if not self.client.authenticated():
            raise No("client must be authenticated first")

        try:
            self.client.lock.acquire()
            # See if we already have a mailbox associated. If we do, deselect
            # it.
            #
            self.client.deselect_mbox()
            
            # See if the mbox they want exists.
            #
            try:
                mbox = self.usermhdir.get_mailbox( \
                    self.imap_command.mailbox_name)
                if examine:
                    self.client.examine_mbox(mbox)
                else:
                    self.client.select_mbox(mbox)
            except mhimap.Mailbox.MailboxException, e:
                raise No(str(e))
        finally:
            self.client.lock.release()

        # Check to see if the mbox is out of sync. This may generate messages
        # for our client.
        #
        mbox.big_lock.acquire()
        try:
            if not mbox.resync():
                # The resync operation did nothing. This means that we need to
                # send a message to this client about this mailbox's
                # particulars.
                #
                self.conn.write("* %d EXISTS\r\n" % mbox.num_msgs)
                self.conn.write("* %d RECENT\r\n" % mbox.num_recent_msgs)
                if mbox.first_unseen_msg():
                    self.conn.write("* OK [UNSEEN %d]\r\n" % \
                                    mbox.first_unseen_msg())
                self.conn.write("* OK [UIDVALIDITY %d]\r\n" % mbox.uid_vv)
                self.conn.write("* FLAGS (%s)\r\n" % ' '.join(mbox.flags))
                if examine:
                    self.conn.write("* OK [PERMANENTFLAGS ()]\r\n")
                else:
                    self.conn.write("* OK [PERMANENTFLAGS (%s)]\r\n" % \
                                    ' '.join(mbox.permanentflags()))
            else:
                # There was a resync operation. That caused the EXISTS and
                # RECENT messages to be sent to all interested clients,
                # including this one. Other messages we are required to send in
                # response to a SELECT were not, though, so we need to send
                # those now.
                #
                if mbox.first_unseen_msg():
                    self.conn.write("* OK [UNSEEN %d]\r\n" % \
                                    mbox.first_unseen_msg())
                self.conn.write("* OK [UIDVALIDITY %d]\r\n" % mbox.uid_vv)
                self.conn.write("* FLAGS (%s)\r\n" % ' '.join(mbox.flags))
                if examine:
                    self.conn.write("* OK [PERMANENTFLAGS ()]\r\n")
                else:
                    self.conn.write("* OK [PERMANENTFLAGS (%s)]\r\n" % \
                                    ' '.join(mbox.permanentflags()))

            # Whenever a mailbox is selected, if it had the '\Marked'
            # attribute, selecting it resets that attribute. From what
            # I can interpret in the rfc, a mbox being EXAMINEd will not
            # change its attributes.
            #
            if not examine:
                mbox.unset_attribute('\Marked')
                mbox.set_attribute('\Unmarked')
        finally:
            mbox.big_lock.release()
        if examine:
            return "[READ-ONLY]"
        else:
            return "[READ-WRITE]"

    #########################################################################
    #
    def do_examine(self):
        """select a specific mailbox
        """
        return self.do_select(examine = True)

    #########################################################################
    #
    def do_create(self):
        mbox_name = self.imap_command.mailbox_name
        try:
            mhimap.Mailbox.Mailbox.create(mbox_name, self.usermhdir)
        except mhimap.Mailbox.MailboxException, e:
            raise No(str(e.value))
        return None

    #########################################################################
    #
    def do_delete(self):
        mbox_name = self.imap_command.mailbox_name
        try:
            mbox = self.usermhdir.get_mailbox(mbox_name)
        except mhimap.Mailbox.NoSuchMailbox, e:
            raise No(str(e.value))

        try:
            mbox.delete(self.usermhdir, mbox_name)
        except mhimap.Mailbox.InvalidMailbox, e:
            raise No(str(e.value))
        return None

    #########################################################################
    #
    def do_rename(self):
        src_mbox_name = self.imap_command.mailbox_src_name
        dst_mbox_name = self.imap_command.mailbox_dst_name

        # The mailbox they are trying to rename must exist.
        #
        try:
            mbox = self.usermhdir.get_mailbox(src_mbox_name)
        except mhimap.Mailbox.NoSuchMailbox, e:
            raise No(str(e.value))

        try:
            mbox.rename(dst_mbox_name)
        except mhimap.Mailbox.MailboxException, e:
            raise No(str(e.value))
        return None

    #########################################################################
    #
    def do_subscribe(self):
        mbox_name = self.imap_command.mailbox_name
        try:
            mbox = self.usermhdir.get_mailbox(mbox_name)
        except mhimap.Mailbox.NoSuchMailbox, e:
            raise No(str(e.value))

        self.client.lock.acquire()
        try:
            self.client.subscribe_mbox(mbox)
        finally:
            self.client.lock.release()
        return None

    #########################################################################
    #
    def do_unsubscribe(self):
        mbox_name = self.imap_command.mailbox_name
        try:
            mbox = self.usermhdir.get_mailbox(mbox_name)
        except mhimap.Mailbox.NoSuchMailbox, e:
            raise No(str(e.value))

        self.client.lock.acquire()
        try:
            try:
                self.client.unsubscribe_mbox(mbox)
            except mhimap.Client.ClientException, e:
                raise No(str(e.value))
        finally:
            self.client.lock.release()
        return None

    #########################################################################
    #
    def do_list(self):
        # Handle the special case where the client is basically just probing
        # for the hierarchy sepration character.
        #
        if self.imap_command.mailbox_name == "" and \
           self.imap_command.list_mailbox == "":
            self.conn.write('* LIST (\Noselect) "/" ""\r\n')
            return None
        
        mbox_list = \
                  self.usermhdir.list_mailboxes(self.imap_command.mailbox_name,
                                                self.imap_command.list_mailbox)
        for mbox_name, attributes in mbox_list:
            self.conn.write('* LIST (%s) "/" %s\r\n' % \
                            (' '.join(attributes), mbox_name))
        return None

    #########################################################################
    #
    def do_lsub(self):
        # Handle the special case where the client is basically just probing
        # for the hierarchy sepration character.
        #
        if self.imap_command.mailbox_name == "" and \
           self.imap_command.list_mailbox == "":
            self.conn.write('* LIST (\Noselect) "/" ""\r\n')
            return None
        
        mbox_list = self.client.subscribed_mailboxes(\
            self.imap_command.mailbox_name, self.imap_command.list_mailbox)
        for mbox_name, attributes in mbox_list:
            self.conn.write('* LIST (%s) "/" %s\r\n' % \
                            (' '.join(attributes), mbox_name))
        return None

    #########################################################################
    #
    def do_status(self):
        try:
            mbox = self.usermhdir.get_mailbox(self.imap_command.mailbox_name)
        except mhimap.Mailbox.MailboxException, e:
            raise No(str(e))
        mbox.big_lock.acquire()
        try:
            mbox.resync()
            att_results = []
            for att in self.imap_command.status_att_list:
                if att == "messages":
                    att_results.append("MESSAGES")
                    att_results.append(str(mbox.num_msgs))
                elif att == "recent":
                    att_results.append("RECENT")
                    att_results.append(str(mbox.num_recent_msgs))
                elif att == "uidnext":
                    att_results.append("UIDNEXT")
                    att_results.append(str(mbox.cur_uid))
                elif att == "uidvalidity":
                    att_results.append("UIDVALIDITY")
                    att_results.append(str(mbox.uid_vv))
                elif att == "unseen":
                    att_results.append("UNSEEN")
                    att_results.append(str(mbox.num_unseen_msgs()))
            self.conn.write('* STATUS %s (%s)\r\n' % (mbox.name,
                                                      ' '.join(att_results)))
        finally:
            mbox.big_lock.release()

        return None

    #########################################################################
    #
    def do_append(self):
        try:
            mbox = self.usermhdir.get_mailbox(self.imap_command.mailbox_name)
        except mhimap.Mailbox.MailboxException, e:
            raise No(str(e))
        (uid_vv, uid) = mbox.append_msg(self.imap_command.message,
                                        self.imap_command.flag_list,
                                        self.imap_command.date_time)
        return "[APPENDUID %d %d]" % (uid_vv, uid)

    #########################################################################
    #
    def do_check(self):
        if not self.client.selected():
            raise Bad("client is not in the selected state")
        self.client.selected_mbox.checkpoint()
        return None
    
    #########################################################################
    #
    def do_close(self):
        if not self.client.selected():
            raise Bad("client is not in the selected state")
        self.client.selected_mbox.close(self.client)
        return None
    
    #########################################################################
    #
    def do_expunge(self):
        if not self.client.selected():
            raise Bad("client is not in the selected state")
        self.client.selected_mbox.expunge()
        return None

    #########################################################################
    #
    def do_search(self):
        if not self.client.selected():
            raise Bad("client is not in the selected state")
        results = self.client.selected_mbox.search(self.imap_command.search_key)
        self.conn.write("* SEARCH %s\r\n" % ' '.join([str(x) for x in results]))
        return None

    #########################################################################
    #
    def do_fetch(self):
        if not self.client.selected():
            raise Bad("client is not in the selected state")
        results = self.client.selected_mbox.fetch(self.imap_command.msg_set,
                                                  self.imap_command.fetch_atts)
        self.conn.write("* FETCH %s\r\n" % ' '.join([str(x) for x in results]))
        return None
