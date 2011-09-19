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
import mailbox

# asimap import
#
from asimap.exceptions import No, Bad

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
        self.server = server
        self.name = name
        self.uid_vv = None

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
                  self.name)
        if c.rowcount <= 0:
            self.uid_vv = self.server.get_next_uid_vv()
            c.execute("insert into mailboxes (name,uid_vv,attributes) "
                      "(?,?,?)", (name, str(self.uid_vv),
                                  ",".join(self.attributes)))
            c.close()
            self.server.db.commit()
        else:
            uid_vv,attributes = c.fetchone()
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
        return

    
        
    
