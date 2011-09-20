#!/usr/bin/env python
#
# File: $Id$
#
"""
The module that deals with the mailbox objects.

There will be a mailbox per MH folder (but not one for the top level
that holds all the folders.)
"""

# system imports
#
import time
import logging
import mailbox

# asimap import
#
from asimap.exceptions import No, Bad
from asimap.constants import SYSTEM_FLAGS, PERMANENT_FLAGS, SYSTEM_FLAG_MAP

##################################################################
##################################################################
#
class MailboxException(No):
    def __init__(self, value = "no"):
        self.value = value
    def __str__(self):
        return repr(self.value)
class MailboxExists(MailboxException):
    pass
class NoSuchMailbox(MailboxException):
    pass
class InvalidMailbox(MailboxException):
    pass
        
##################################################################
##################################################################
#
class Mailbox(object):
    """
    An instance of an active mailbox folder.

    We create instances of this class for every mailbox that is
    selected/examined/subscribed to.

    When a Mailbox object is created it registers itself with the user server
    so that the server knows what all the active mailboxes are.

    Each mailbox tracks how many clients are interested in it and sets a
    timestamp when there are no longer any clients interested in it.

    At the end of every i/o loop the server pokes each active mailbox. If a
    mailbox has had no clients for a certain amount of time its state is
    persisted in to the database and then that mailbox object is deleted.

    Also at the end of each i/o loop, after inactive mailboxes have been
    removed, the server goes to each active mailbox and tells it to make sure
    it is up to date with its underlying MH mail folder.

    This may cause each mailbox to send out notifications to every client.
    """

    ##################################################################
    #
    def __init__(self, name, server):
        """
        This represents an active mailbox. You can only instantiate
        this class for mailboxes that actually in the file system.

        You need to use the class method 'create()' if you wish to
        create a mailbox that does not already exist.

        Arguments:
        - `name`: The mailbox name. This must represent a mailbox that exists.
        - `server`: A reference to the user_server object which ties
                    together all of the active mailboxes, the
                    database connection, and all of the IMAP clients
                    currently connected to us.
        """
        self.log = logging.getLogger("%s.%s" % (__name__, self.__class__.__name__))
        self.server = server
        self.name = name
        self.uid_vv = None
        self.mtime = None
        self.next_uid = 1

        # You can not instantiate a mailbox that does not exist in the
        # underlying file system.
        #
        try:
            self.mailbox = server.mailbox.get_folder(name)
        except mailbox.NoSuchMailboxError, e:
            raise NoSuchMailbox("No such mailbox: '%s'" % name)
        
        # The list of attributes on this mailbox (this is things such as
        # '\Noselect'
        #
        self.attributes = []

        # When a mailbox is no longer selected by _any_ client, then after a
        # period of time we destroy this instance of the Mailbox object (NOT
        # the underlying mailbox.. just that we have an active instance
        # attached to our server object.)
        #
        # This way we do not spend any resources scanning a mailbox that no
        # client is actively looking at, but we take our time doing this in
        # case a client is selecting between various mailboxes.
        #
        # Once no client has this mailbox selected this gets a
        # timestamp. Every loop through the main server will check all the
        # active mailboxes and remove the ones that have no clients for some
        # period of time (like 15 minutes.)
        #
        self.time_since_selected = None

        # The dict of clients that currently have this mailbox selected.
        # This includes clients that used 'EXAMINE' instead of 'SELECT'
        #
        self.clients = { }

        # After initial setup fill in any persistent values from the database
        # (and if there are no, then create an entry in the db for this
        # mailbox.
        #
        self._restore_from_db()

        # And finally we add ourself to the dictionary of active mailboxes
        # that the server is tracking.
        #
        self.server.active_mailboxes[name] = self

        return

    ##################################################################
    #
    def _restore_from_db(self):
        """
        Restores this mailbox's persistent state from the database.  If this
        mailbox does not exist in the db we create an entry for it with
        defaults.
        """
        c = self.server.db.conn.cursor()
        c.execute("select uid_vv,attributes from mailboxes where name=?",
                  (self.name,))
        results = c.fetchone()
        if results is None:
            self.uid_vv = self.server.get_next_uid_vv()
            c.execute("insert into mailboxes (name,uid_vv,attributes) values "
                      "(?,?,?)", (self.name, str(self.uid_vv),
                                  ",".join(self.attributes)))
            c.close()
            self.server.db.commit()
        else:
            uid_vv,attributes = results
            self.uid_vv = int(uid_vv)
            self.attributes = attributes.split(",")
            c.close()
        return
    
    ##################################################################
    #
    def selected(self, client):
        """
        This mailbox is being selected by a client.

        Add the client to the dict of clients that have this mailbox selected.
        Resets the self.time_since_selected attribute to None.

        from rfc2060:

           The SELECT command selects a mailbox so that messages in the
           mailbox can be accessed.  Before returning an OK to the client,
           the server MUST send the following untagged data to the client:

              FLAGS       Defined flags in the mailbox.  See the description
                          of the FLAGS response for more detail.

              <n> EXISTS  The number of messages in the mailbox.  See the
                          description of the EXISTS response for more detail.

              <n> RECENT  The number of messages with the \Recent flag set.
                          See the description of the RECENT response for more
                          detail.

              OK [UIDVALIDITY <n>]
                          The unique identifier validity value.  See the
                          description of the UID command for more detail.

           to define the initial state of the mailbox at the client.

           The server SHOULD also send an UNSEEN response code in an OK
           untagged response, indicating the message sequence number of the
           first unseen message in the mailbox.

           If the client can not change the permanent state of one or more of
           the flags listed in the FLAGS untagged response, the server SHOULD
           send a PERMANENTFLAGS response code in an OK untagged response,
           listing the flags that the client can change permanently.

           If the client is permitted to modify the mailbox, the server SHOULD
           prefix the text of the tagged OK response with the "[READ-WRITE]"
           response code.

           If the client is not permitted to modify the mailbox but is
           permitted read access, the mailbox is selected as read-only, and
           the server MUST prefix the text of the tagged OK response to SELECT
           with the "[READ-ONLY]" response code.

        Arguments:
        - `client`: The client that has selected this mailbox.
        """
        if client.client.port in self.clients:
            raise No("Mailbox '%s' is already selected" % self.name)
        self.clients[client.client.port] = client

        self.mailbox.lock()
        try:
            # Now send back messages to this client that it expects upon
            # selecting a mailbox.
            #
            # Flags on messages are represented by being in an MH sequence.
            # The sequence name == the flag on the message.
            #
            # NOTE: '\' is not a permitted character in an MH sequence name so
            #       we translate "SRecent" to '\Recent', 'SUnseen
            #
            seq = self.mailbox.get_sequences()
            mbox_keys = self.mailbox.keys()
            self.client.client.push("* %d EXISTS\r\n" % len(mbox_keys))
            if "SRecent" in seq:
                self.client.client.push("* %d RECENT\r\n" % \
                                            len(seq["SRecent"]))
            else:
                self.client.client.push("* 0 RECENT\r\n")
            if "unseen" in seq:
                # Message id of the first message that is unseen.
                #
                self.client.client.push("* OK [UNSEEN %d]\r\n" % \
                                            mbox_keys.index(seq['unseen'][0]))
            self.client.client.push("* OK [UIDVALIDITY %d]\r\n" % self.uid_vv)

            # Each sequence is a valid flag.. we send back to the client all
            # of the system flags and any other sequences that are defined on
            # this mailbox.
            #
            flags = SYSTEM_FLAGS[:]
            for k in seq.keys():
                if k not in SYSTEM_FLAG_MAP:
                    flags.append(k)
            self.client.client.push("* FLAGS (%s)" % " ".join(flags))
            self.client.client.push("* OK [PERMANENTFLAGS (%s)]" % \
                                        " ".join(PERMANENT_FLAGS))
        finally:
            self.mailbox.unlock()

        return

    ##################################################################
    #
    def unselected(self, client):
        """
        When the client is no longer selecting/examining this mailbox.

        Pretty simple in that we remove the client from the dict of
        clients. If there are no clients then we set the time at which the
        last time client left so the server can know when this mailbox has
        been around for long enough with no active clients to warrant getting
        rid of.

        Arguments:
        - `client`: The client that is no longer looking at this mailbox.
        """
        # We only bother with doing anything in the client is actually
        # in this mailbox's list of clients.
        #
        if client.client.port not in self.clients:
            return
        
        del self.clients[client.client.port]
        if len(self.clients) == 0:
            self.log.debug("unselected(): No clients, starting timer")
            self.time_since_selected = time.time()
        return
    
    #########################################################################
    #
    @classmethod
    def create(cls, name, server):
        """
        Creates a mailbox on disk that does not already exist and
        instantiates a Mailbox object for it.
        """
        if name == "inbox":
            raise InvalidMailbox("Can not create a mailbox named 'inbox'")

        # ... ....
        # ... .... Do useful stuff here
        # ... ....
        
        return cls(name, server)
    
    ####################################################################
    #
    @classmethod
    def delete(name, server):
        """
        Delete the specified mailbox.

        Each of the non-permitted failure cases will raise MailboxException.

        You can not delete the mailbox named 'inbox'
        
        You can delete mailboxes that contain other mailboxes BUT what happens
        is that the mailbox is emptied of all messages and it then gets the
        '\Noselect' flag.

        You can NOT delete a mailbox that has the '\Noselect' flag AND
        contains sub-mailboxes.

        If the mailbox is selected by any client then what happens is the same
        as if the mailbox had an inferior mailbox: all the messages are empty
        and the mailbox gains the '\Noselect' flag.

        Arguments:
        - `name`: The name of the mailbox to delete
        - `server`: The user server object
        """
        if name == "inbox":
            raise InvalidMailbox("You are not allowed to delete the inbox")

        try:
            mailbox = server.mailbox.get_folder(name)
        except mailbox.NoSuchMailboxError, e:
            raise NoSuchMailbox("No such mailbox: '%s'" % name)

        try:
            mailbox.lock()
            inferior_mailboxes = mailbox.list_folders()

            # See if this mailbox is an activemailbox
            #
            active_mailbox = None
            if name in server.active_mailboxes:
                active_mailbox = server.active_mailboxes[name]

            # When deleting a mailbox it will cause to be deleted every
            # message in that mailbox to be deleted. If we have an
            # activemailbox then we need to follow its forms for deleting all
            # the messages. If we do not we can just delete them directly.
            #
            if active_mailbox:
                # XXX How this works will be defined when we get to the
                #     methods for storing flags on a message.
                # for key in mailbox.iterkeys():
                #     mailbox.message(key).store('\\Deleted')
                # mailbox.expunge()
                pass
            else:
                mailbox.clear()

                # If there are any message references in our db do a SQL
                # command that deletes all message entries stored in this
                # mailbox.
                #
                # XXX c.execute("delete from messages where mailbox=?",(name,))

            # If the mailbox has inferior mailboxes then we do not actually
            # delete it. It gets the '\Noselect' flag though.
            #
            if len(inferior_mailboxes) > 0:
                # If we have an active mailbox we will use its methods for
                # setting the flags.
                #
                if active_mailbox:
                    active_mailbox.set_flags(["\\Noselect"])
                else:
                    c = server.db.cursor()
                    c.execute("update mailboxes set flags='\\Noselect' where "
                              "name = ?", (name,))
                    c.close()
                    server.db.commit()
            else:
                # We have no inferior mailboxes. This mailbox is gone. If it
                # is active we remove it from the list of active mailboxes
                # and if it has any clients that have it selected they are
                # moved back to the unauthenticated state.
                #
                if active_mailbox:
                    for client in active_mailbox.clients.itervalues():
                        client.state = "authenticated"
                        client.mbox = None
                    del server.active_mailboxes[name]

                    # XXX rfc2060 says nothing about notifying other clients
                    #     that the mailbox they have selected is now gone. ^_^;;
                    #     I _guess_ they will get a "No" response to any
                    #     'selected' state command they send us.
                
                # Delete all traces of the mailbox from our db.
                #
                c = server.db.cursor()
                c.execute("delete from mailboxes where name = ?", (name,))
                c.close()
                server.db.commit()

                # And remove the mailbox from the filesystem.
                #
                server.mailbox.remove_folder(name)

        finally:
            mailbox.close()
        return

    
        
    
