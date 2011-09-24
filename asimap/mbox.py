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
import os.path
import time
import logging
import mailbox
import re
from email.parser import HeaderParser

# asimap import
#
from asimap.exceptions import No, Bad
from asimap.constants import SYSTEM_FLAGS, PERMANENT_FLAGS, SYSTEM_FLAG_MAP


uid_re = re.compile(r'^(?P<uidvv>\d+)\.(?P<uid>\d+)$')

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
    def __init__(self, name, server, add_to_active = True):
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

        - `add_to_active`: if true when we create this instance we will also
                           add it to the list of active folders on the server
                           object.

                           This is used when we wish to check if a folder has
                           had any interesting changes that warrant it getting
                           a '\Marked' or '\Unmarked' flag, but there are no
                           clients currently selecting it or poking it so it
                           does not warrant hanging around after we have
                           updated its state.
        """
        self.log = logging.getLogger("%s.%s.%s" % (__name__, self.__class__.__name__,name))
        self.server = server
        self.name = name
        self.uid_vv = self.server.get_next_uid_vv()
        self.mtime = 0
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
        self.attributes = set()

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
        self.time_since_selected = time.time()

        # The dict of clients that currently have this mailbox selected.
        # This includes clients that used 'EXAMINE' instead of 'SELECT'
        #
        self.clients = { }

        # After initial setup fill in any persistent values from the database
        # (and if there are no, then create an entry in the db for this
        # mailbox.
        #
        self._restore_from_db()

        # And make sure our mailbox on disk state is up to snuff and update
        # our db if we need to.
        #
        self.resync()

        # And finally we add ourself to the dictionary of active mailboxes
        # that the server is tracking.
        #
        if add_to_active:
            self.server.active_mailboxes[name] = self

        return

    ##################################################################
    #
    def resync(self):
        """
        This will go through the mailbox on disk and make sure all of the
        messages have proper uuid's, make sure we have a .mh_sequences file
        and that it is up to date with what messages are in the 'seen'
        sequence.

        This is also what controls setting the '\Marked' and '\Unmarked' flags
        on the mailbox as well as marking individual messages as '\Recent'

        We have a '\Seen' flag and we derive this by seeing what messages are
        in the unseen sequence.

        Since the definition of '\Recent' in rfc2060 is rather vague we are
        going to take all messages marked as '\Unseen' and also give them the
        attribute '\Recent'

        Any folder with unseen messages will be tagged with '\Marked.' That is
        how we are going to treat that flag.

        Calling this method will cause 'EXISTS' and 'RECENT' messages to be
        sent to any clients attached to this mailbox if the mailbox has had
        changes in the number of messages or the messages that were added to
        it.
        """

        # If the .mh_sequence file does not exist create it.
        #
        # XXX Bad that we are reaching in to the mailbox.MH object to
        #     find thepath to the sequences file.
        #
        if not os.path.exists(os.path.join(self.mailbox._path,'.mh_sequences')):
            os.close(os.open(os.path.join(self.mailbox._path, '.mh_sequences'),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0600))

        try:
            self.mailbox.lock()
            # Whenever we resync the mailbox we update the sequence for 'seen'
            # based on 'seen' are all the messages that are NOT in the
            # 'unseen' sequence.
            #
            seq = self.mailbox.get_sequences()
            msgs = self.mailbox.keys()
            if 'unseen' in seq:
                # Create the 'Seen' sequence by the difference between all the
                # messages in the mailbox and the unseen ones.
                #
                # The Recent sequence mirrors the unseen sequence.
                #
                seq['Seen'] = list(set(msgs) - set(seq['unseen']))
                seq['Recent'] = seq['unseen']

                # A mailbox gets '\Marked' if it has any unseen messages.
                #
                if '\\Unmarked' in self.attributes:
                    self.attributes.remove('\\Unmarked')
                self.attributes.add('\\Marked')

            else:
                # There are no unseen messages in the mailbox thus the Seen
                # sequence mirrors the set of all messages.
                #
                seq['Seen'] = msgs

                # Since the Recent sequence mirrors the unseen sequence make
                # sure it is removed too.
                #
                if 'Recent' in seq:
                    del seq['Recent']

                # A mailbox is '\Unmarked' if it has no unseen messages.
                #
                if '\\Marked' in self.attributes:
                    self.attributes.remove('\\Marked')
                self.attributes.add('\\Unmarked')

            self.mailbox.set_sequences(seq)

            # Now comes the big work. We need to make sure every message
            # has a UID following the rules of the rfc. This is a fairly
            # hairy process so we put it off into its own method.
            #
            self._check_update_all_msg_uids()

            # Now see if our mtime is different than the mtime of the actual
            # mail folder. If it is then update our version and the database.
            #
            mtime = int(os.path.getmtime(self.mailbox._path))
            if self.mtime != mtime:
                self.mtime = mtime
                self._commit_to_db()

        finally:
            self.mailbox.unlock()
        return
    
    ##################################################################
    #
    def _check_update_all_msg_uids(self):
        """
        This will loop through all of the messages in the folder checking to
        see if they have UID_VV.UID's in them. If they do not or it is out of
        sequence (UID's must be monotonically increasing at all times) then we
        have to generate new UID's for every message after the out-of-sequence
        one we encountered.

        NOTE: The important thing to note is that we store the uid_vv / uid for
              message _in the message_ itself. This way if the message is moved
              around we will know if it is out of sequence, a totally new
              message, or from a different mailbox.

              The downside is that we need to pick at every message to find
              this header information but we will try to do this as efficiently
              as possible.

        NOTE: My big worry is the spam mailboxes which get many many messages
              per minute which means we will be scanning that directory
              constantly and that load may be just too much even for our
              'acceptably slow server'.

              We may need to add an attribute to a mailbox like "do not scan"
              so that it is never looked at. We generally do not care if we get
              new mail in the spam folder anyways.
        """

        # As we go through messages we need to know if the current UID we are
        # looking at is proper (ie: greater than the one of the previous
        # message.)
        #
        # If we hit one that is not then from that message on we need to
        # re-number all of their UID's.
        #
        redoing_rest_of_folder = False
        prev_uid = 0

        # Loop through all of the messages in this mailbox.
        #
        # For each message see if it has the header that we use to define the
        # uid_vv / uid for a message.
        #
        # If the message does not, or if it has a uid lower than the previous
        # uid, or if its uid_vv does not match the uid_vv of this mailbox then
        # 'redoing' goes to true and we now update this message and every
        # successive message adding a proper uid_vv/uid header.
        #
        for msg in self.mailbox.keys():

            # If we are not redoing the rest of the folder check to see if
            # this messages uid_vv / uid is what we expect.
            #
            if not redoing_rest_of_folder:
                try:
                    fp = self.mailbox.get_file(key)
                    msg_hdrs = HeaderParser().parse(fp, headersonly = True)
                finally:
                    fp.close()

                if 'x-asimapd-uid' in msg_hdrs:
                    try:
                        uid_vv,uid = msg_hdrs['x-asimapd-uid'].split(".")
                        uid_vv = int(uid_vv)
                        uid = int(uid)

                        # If the uid_vv is different or the uid is NOT
                        # monotonically increasing from the previous uid then
                        # we have to redo the rest of the folder.
                        #
                        if uid_vv != self.uid_vv or uid <= prev_uid:
                            redoing_rest_of_folder = True
                        else:
                            prev_uid == uid

                    except ValueError:
                        # the uid was not properly formed. This counts as
                        # having no uid.
                        #
                        self.log.info("msg %s had malformed uid header: %s" % \
                                          (key, msg_hdrs['x-asimapd-uid']))
                        redoing_rest_of_folder = True

            # at this point we MAY be redoing this message (and the rest of the
            # folder) so we check again.. if we are not then skip to the next
            # iteration of this loop.
            #
            if redoing_rest_of_folder:
                self._update_msg_uid(key)
        
        # And we are done..
        #
        return

    ##################################################################
    #
    def _update_msg_uid(self, msg):
        """
        
        Arguments:
        - `msg`:
        """
        pass
    
    
    ##################################################################
    #
    def _restore_from_db(self):
        """
        Restores this mailbox's persistent state from the database.  If this
        mailbox does not exist in the db we create an entry for it with
        defaults.
        """
        c = self.server.db.cursor()
        c.execute("select uid_vv,attributes,mtime,next_uid from mailboxes "
                  "where name=?", (self.name,))
        results = c.fetchone()
        if results is None:
            c.execute("insert into mailboxes (name, uid_vv, attributes, "
                      "mtime, next_uid) values (?,?,?,?,?)",
                      (self.name, self.uid_vv,",".join(self.attributes),
                       int(os.path.getmtime(self.mailbox._path)),self.next_uid))
            c.close()
            self.server.db.commit()
        else:
            self.uid_vv,attributes,self.mtime,self.next_uid = results
            self.attributes = set(attributes.split(","))
            c.close()
        return

    ##################################################################
    #
    def _commit_to_db(self):
        """
        Write the state of the mailbox back to the database for persistent
        storage.
        """
        c = self.server.db.cursor()
        c.execute("update mailboxes set uid_vv=?, attributes=?, next_uid=?,"
                  "mtime=? where name=?",
                  (self.uid_vv, ",".join(self.attributes),self.next_uid,
                   int(os.path.getmtime(self.mailbox._path)),self.name))
        c.close()
        self.server.db.commit()
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

        if '\\Noselect' in self.attributes:
            raise No("You can not select the mailbox '%s'" % self.name)
        
        self.clients[client.client.port] = client

        # A client has us selected. Turn of the expiry time.
        #
        self.time_since_selected = None

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
            client.client.push("* %d EXISTS\r\n" % len(mbox_keys))
            if "SRecent" in seq:
                client.client.push("* %d RECENT\r\n" % \
                                            len(seq["SRecent"]))
            else:
                client.client.push("* 0 RECENT\r\n")
            if "unseen" in seq:
                # Message id of the first message that is unseen.
                #
                client.client.push("* OK [UNSEEN %d]\r\n" % \
                                            mbox_keys.index(seq['unseen'][0]))
            client.client.push("* OK [UIDVALIDITY %d]\r\n" % self.uid_vv)

            # Each sequence is a valid flag.. we send back to the client all
            # of the system flags and any other sequences that are defined on
            # this mailbox.
            #
            flags = list(SYSTEM_FLAGS)
            for k in seq.keys():
                if k not in SYSTEM_FLAG_MAP:
                    flags.append(k)
            client.client.push("* FLAGS (%s)\r\n" % " ".join(flags))
            client.client.push("* OK [PERMANENTFLAGS (%s)]\r\n" % \
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

    ####################################################################
    #
    @classmethod
    def is_outofdate(cls, name, server):
        """
        This gets the mtime of the folder both as it is recorded in the
        database and what it is in the file actual file system and compares
        them.

        We use the active mailbox object if it exists instead of doing a
        sqlite3 query.

        If the mtime recorded in the db differs from the mtime of the file
        system then we return true.

        This provides a way to see if a folder has been modified without
        instantiating it.

        Arguments:
        - `cls`: The Mailbox class
        - `name`: The name of the mailbox
        - `server`: a handle on the server object
        """
        if name in server.active_mailboxes:
            mbox_mtime = server.active_mailboxes[name].mtime
        else:
            try:
                c = self.server.db.cursor()
                c.execute("select mtime from mailboxes where name=?",
                          (self.name,))
                results = c.fetchone()
                if results is None:
                    raise NoSuchMailbox("The mailbox '%s' does not exist")
                else:
                    mbox_mtime = int(results[0])
            finally:
                c.close()

        # XXX Ug. Not good to reach in to the mailbox to get the path to the
        #     directory..
        #
        dir_mtime = int(os.path.getmtime(os.path.abspath(os.path.expanduser(name))))
        return mbox_mtime != dir_mtime
    
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

        log = logging.getLogger("%s.%s" % (__name__, cls.__name__))

        # If the mailbox already exists than it can not be created either One
        # exception is if the mailbox exists but with the "\Noselect"
        # flag.. this means that it was previously deleted and sitting in its
        # place is a phantom mailbox. In this case we remove the '\Noselect'
        # flag and return success.
        #
        if name in server.active_mailboxes:
            mbox = server.active_mailboxes
        else:
            try:
                mbox = cls(name,server)
            except NoSuchMailbox:
                mbox = None

        # See if the mailbox exists but with the '\Noselect' attribute.
        #
        if mbox:
            if '\\Noselect' in mbox.attributes:
                mbox.attributes = []
                mbox._commit_to_db()
            else:
                raise MailboxExists("Mailbox %s already exists" % name)
        
        # The mailbox does not exist, we can create it.
        #
        # NOTE: We need to create any intermediate path elements, and what is
        #       more those intermediate path elements are not actually created
        #       mailboxes until they have been explicitly 'created'. But that
        #       is annoying. I will just create the intermediary directories.
        #
        mbox_chain = []
        chain_name = name
        while chain_name != "":
            mbox_chain.append(chain_name)
            chain_name = os.path.dirname(chain_name)

        mbox_chain.reverse()
        for m in mbox_chain:
            mbox = mailbox.MH(m, create = True)
            mbox = cls(m, server)
        return
    
    ####################################################################
    #
    @classmethod
    def delete(cls, name, server):
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

        do_delete = False
        try:
            mailbox.lock()
            inferior_mailboxes = mailbox.list_folders()

            # See if this mailbox is an activemailbox
            #
            active_mailbox = None
            if name in server.active_mailboxes:
                active_mailbox = server.active_mailboxes[name]
            else:
                active_mailbox = cls(name, server)

            # You can not delete a mailbox that has the '\Noselect' attribute.
            #
            if '\\Noselect' in mailbox.attributes:
                raise InvalidMailbox("The mailbox '%s' is already deleted" % \
                                         name)
                
            # When deleting a mailbox it will cause to be deleted every
            # message in that mailbox to be deleted. If we have an
            # activemailbox we need to tell all clients that have it selected
            # that the number of messages in it has changed.
            #
            mailbox.clear()
            for client in active_mailbox.clients.itervalues():
                client.client.push("* 0 EXISTS\r\n")
                client.client.push("* 0 RECENT\r\n")

            # If the mailbox has inferior mailboxes then we do not actually
            # delete it. It gets the '\Noselect' flag though. It also gets a
            # new uid_vv so that if it is recreated before being fully removed
            # from the db no imap client will confuse it with the existing
            # mailbox.
            #
            if len(inferior_mailboxes) > 0:
                active_mailbox.attributes = set("\\Noselect")
                active_mailbox.uid_vv = server.get_next_uid_vv()
                active_mailbox._commit_to_db()
            else:
                # We have no inferior mailboxes. This mailbox is gone. If it
                # is active we remove it from the list of active mailboxes
                # and if it has any clients that have it selected they are
                # moved back to the unauthenticated state.
                #
                # XXX rfc2060 says nothing about notifying other clients
                #     that the mailbox they have selected is now gone. ^_^;;
                #     I _guess_ they will get a "No" response to any
                #     'selected' state command they send us.
                #     Perhaps we should send back the 'TRYCREATE' flag?
                #
                for client in active_mailbox.clients.itervalues():
                    client.state = "authenticated"
                    client.mbox = None
                del server.active_mailboxes[name]

                # Delete all traces of the mailbox from our db.
                #
                c = server.db.cursor()
                c.execute("delete from mailboxes where name = ?", (name,))
                c.close()
                server.db.commit()

                # We need to delay the 'delete' of the actual mailbox until
                # after we release the lock.. but we only delete the actual
                # mailbox outside of the try/finally close if we are actually
                # deleting it.
                #
                do_delete = True
        finally:
            mailbox.close()

        # And remove the mailbox from the filesystem.
        #
        if do_delete:
            server.mailbox.remove_folder(name)
        return
