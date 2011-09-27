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

####################################################################
#
def get_uid_from_file(fp, msg, log = None):
    """
    This is a special helper function that efficiently (more efficiently than
    HeaderParser) looks through the given file pointer for the asimap
    uid_vv/uid.

    If we find it we return the tuple (uid_vv, uid). If we do not find it we
    return (None, None)

    Basically we read a line at a time from the file looking for a line that
    begins with the string: "X-asimapd-uid: "

    Once we find that we split it apart looking for the <uid_vv>.<uid>.

    We stop looking when we hit a blank line.

    Arguments:

    - `fp`: the file we are reading from. We do not close it. If someone after
            us wants to use it they will likely need to call '.rewind()' to set
            the pointer back to the beginning of the file.
    """

    # Look through the header for the 'x-asimapd-uid' header. If we encounter a
    # blank line then we have reached the end of the header.
    #
    for line in fp:
        if len(line.strip()) == 0:
            return (None, None)
        if line[0:15].lower() == 'x-asimapd-uid: ':
            break

    # We only get here if we encountered the 'x-asimapde-uid' header. Take the
    # value part of the heade and split it around "."
    #
    uid_vv = None
    uid = None
    try:
        uid_vv,uid = (line[15:].strip().split('.'))
        uid_vv = int(uid_vv)
        uid = int(uid)
    except ValueError:
        log.info("msg %s had malformed uid header: %s" % (msg, line))
    return uid_vv, uid

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
    def __init__(self, name, server, expiry = 900):
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

        - `expiry`: If not none then it specifies the number of seconds in the
                    future when we want this mailbox to be turfed out if it has
                    no active clients. Defaults to 15 minutes.
        """
        self.log = logging.getLogger("%s.%s.%s" % (__name__, self.__class__.__name__,name))
        self.server = server
        self.name = name
        self.id = None
        self.uid_vv = None
        self.mtime = 0
        self.next_uid = 0
        self.num_msgs = 0
        self.num_recent = 0

        # It is important to note that self.sequences is the value of the
        # sequences we stored in the db from the end of the last resync. As
        # such they are useful for measuring what has changed in various
        # sequences between resync() runs. NOT as a definitive set of what the
        # current sequences in the mailbox are.
        #
        # These are basically updated at the end of each resync() cycle.
        #
        self.sequences = { }

        # You can not instantiate a mailbox that does not exist in the
        # underlying file system.
        #
        try:
            self.mailbox = server.mailbox.get_folder(name)
        except mailbox.NoSuchMailboxError, e:
            raise NoSuchMailbox("No such mailbox: '%s'" % name)

        # If the .mh_sequence file does not exist create it.
        #
        # XXX Bad that we are reaching in to the mailbox.MH object to
        #     find thepath to the sequences file.
        #
        if not os.path.exists(os.path.join(self.mailbox._path,'.mh_sequences')):
            os.close(os.open(os.path.join(self.mailbox._path, '.mh_sequences'),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0600))

        # The list of attributes on this mailbox (this is things such as
        # '\Noselect'
        #
        self.attributes = set(['\\Unmarked'])

        # When a mailbox is no longer selected by _any_ client, then after a
        # period of time we destroy this instance of the Mailbox object (NOT
        # the underlying mailbox.. just that we have an active instance
        # attached to our server object.)
        #
        # This way we do not spend any resources scanning a mailbox that no
        # client is actively looking at, but we take our time doing this in
        # case a client is selecting between various mailboxes.
        #
        # Whenever the list of active clients attached to this mailbox is
        # non-zero self.expiry will have a value of None. Otherwise it has the
        # time since epoch in seconds when this mailbox should be turfed out.
        #
        # We let the caller pass in a desired expiry. This is used when just
        # wanting to resync() a mailbox but not have it hang around for a long
        # time.
        #
        self.expiry = time.time() + expiry

        # The dict of clients that currently have this mailbox selected.
        # This includes clients that used 'EXAMINE' instead of 'SELECT'
        #
        self.clients = { }

        # After initial setup fill in any persistent values from the database
        # (and if there are no, then create an entry in the db for this
        # mailbox.
        #
        # NOTE: If we get back 'False' from _restore_from_db() that means there
        #       was no entry for this mailbox in the db which means that we
        #       need to force a full resync of the mailbox since it is new.
        #
        force_resync = self._restore_from_db()

        # And make sure our mailbox on disk state is up to snuff and update
        # our db if we need to.
        #
        self.resync(force = not force_resync)
        return

    ##################################################################
    #
    def marked(self, bool):
        """
        A helper function that toggles the '\Marked' or '\Unmarked' flags on a
        folder (another one of those annoying things in the RFC you really only
        need one of these flags.)

        Arguments:
        - `bool`: if True the \Marked attribute is added to the folder. If
                  False the \Unmarked attribute is added to the folder.
        """
        if bool:
            if '\\Unmarked' in self.attributes:
                self.attributes.remove('\\Unmarked')
                self.attributes.add('\\Marked')
        else:
            if '\\Marked' in self.attributes:
                self.attributes.remove('\\Marked')
                self.attributes.add('\\Unmarked')
        return

    ##################################################################
    #
    def resync(self, force = False, safe_changes = False):
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

        Arguments:

        - `force`: If true we will do a complete resync even if the mtime of
                   the folder is not greater than the mtime recorded in the
                   mailbox object. Otherwise we will skip the 'check all
                   messages for valid uids' step of the process. The theory is
                   that if the mtime has not changed no new messages have been
                   added or removed so there is no need to do a complete uid
                   update.

        - `safe_changes`: This flag goes one step further than `force`. If this
                          is true then we are saying that any changes that
                          exist to the mailbox that caused its mtime to change
                          are indeed 'safe' and we do NOT need to scan all of
                          the messages to make sure their UID's match up
                          properly.

                          This is typically called when _we_ the asimap user
                          server have caused the mtime on the folder to change
                          and we have already assured that all of the uid's for
                          all of the messages properly exist.
        """

        start_time = time.time()

        # Get the mtime of the folder at the start so when we need to check to
        # see if we need to do a full scan we have this value before anything
        # we have done in this routine has a chance to modify it.
        #
        start_mtime = int(os.path.getmtime(self.mailbox._path))

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
                self.marked(True)
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
                self.marked(False)

            self.mailbox.set_sequences(seq)

            check_all_start = time.time()
            self.log.debug("resync: Time to check sequences: %d" % \
                               (check_all_start - start_time))

            # Now comes the task that can take an obscene amount of time to
            # accomplish thus we apply a few tricks to reduce when we do it,
            # and if we do it, account for the most common case which is
            # considerably cheaper to do. Also, skip this entirely if we were
            # told when this method was inovked that the state of the mailbox
            # with respect to uid_vv/uid's of all the messages is safe.
            #
            # The big work is scanning the messages in a folder to make sure
            # that they have valid uid_vv/uid's. If they do not then we need to
            # update all the messages that have invlid or missing uid_vv/uid's.
            #
            # However we only do this if:
            #
            # safe_changes == False AND
            #   (mtime of actual folder > self.mtime OR
            #    force == True)
            #
            # Once we agree that we need to rescan the folder usually we ONLY
            # need to rescan the new messages that have been added to the
            # folder.
            #
            # To do this we compare the _current_ value of the unseen sequence
            # to the value of the unseen sequence on this mailbox object. If
            # they are different then the unseen sequence has changed. This is
            # the most common case and the ONLY messages that need to be
            # checked are the ones starting at the first message in the unseen
            # sequence.
            #
            # Otherwise we fall back to scanning the entire folder.
            #
            if len(msgs) > 0 and safe_changes == False and \
                    (start_mtime > self.mtime or force == True):

                self.log.debug("Doing a message resync because: %d > %d or "
                               "%s" % (start_mtime, self.mtime, repr(force)))

                # Now the question is "do we scan all the messages in the
                # folder" or "some subset of messages"
                #
                # Scanning all of the messages can be prohibitively expensive
                # (15 seconds where the entire server does not respond) and
                # more often than not the only thing happening is: messages are
                # being removed and new messages are appended to the end of the
                # mailbox neither of which require a rescan of the entire
                # mailbox.
                #
                # if force is true then we will scan the entire folder.
                #
                # Otherwise we do some manipulations.
                #
                # If the unseen sequence is different than we find the lowest
                # numbered message that is in the new unseen sequence than was
                # not in the old unseen sequence.
                #
                # Scan back from the end of the mailbox until we find the first
                # message that has a uid_vv that matches the uid_vv of our
                # folder.
                #
                # Given these two references to a message choose the lower of
                # the two and scan from that point forward.
                #
                # XXX One has to wonder should we just use the 'first new msg'
                #     and not bother with the unseen sequence?
                #
                if force == True:
                    self.log.debug("rescanning all %d messages" % len(msgs))
                    self._check_update_msg_uids(msgs)
                else:
                    start = min(self._find_first_new_message(msgs),
                                self._find_msg_without_uidvv(msgs))
                    if start in msgs:
                        self.log.debug("rescanning from %d to %d" % \
                                       (start, msgs[-1]))
                        self._check_update_msg_uids(msgs[msgs.index(start):])

                check_all_done = time.time()
                self.log.debug("resync: time to check all uids: %d, total "
                               "resync time so far: %d" % \
                                   ((check_all_done-check_all_start),
                                    (check_all_done-start_time)))
            else:
                self.log.debug("Skipping 'check_update_all_msg_uids' because "
                               "safe_changes: %s, start_mtime: %d, mtime: %d, "
                               "force: %s" % (repr(safe_changes), start_mtime,
                                              self.mtime, force))
                # we need this for the final time delta debug statement.
                check_all_done = time.time()

            # Before we finish if the number of messages in the folder or the
            # number of messages in the Recent sequence is different than the
            # last time we did a resync then this folder is intersted (\Marked)
            # and we need to tell all clients listening to this folder about
            # its new sizes.
            #
            seq = self.mailbox.get_sequences()
            self.seq_unseen = set(seq.get('unseen', []))

            num_recent = 0
            if 'Recent' in seq:
                num_recent = len(seq['Recent'])

            if len(msgs) != self.num_msgs or num_recent != self.num_recent:
                # Notify all listening clients that the number of messages and
                # number of recent messages has changed.
                #
                for client in self.clients.itervalues():
                    client.client.push("* %d EXISTS\r\n" % len(msgs))
                    client.client.push("* %d RECENT\r\n" % num_recent)

            # Make sure to update our mailbox object with the new counts.
            #
            self.num_msgs = len(msgs)
            self.num_recent = num_recent

        finally:
            self.mailbox.unlock()

        # And update the mtime before we leave..
        #
        self.mtime = int(os.path.getmtime(self.mailbox._path))
        self.log.debug("mtime on folder is now: %d" % self.mtime)
        self._commit_to_db()

        end_time = time.time()
        self.log.debug("resync: Time to do all client updates: %d, total "
                       "resync time: %d" % ((end_time-check_all_done),
                                            (end_time-start_time)))
        return

    ##################################################################
    #
    def _find_first_new_message(self, msgs, horizon = 0):
        """
        This goes through the list of msgs given and finds the lowest numbered
        one whose mtime is greater than the mtime of the folder minus <horizon>
        seconds which defaults to 0 seconds.

        The goal is to find messages that were 'recently' (ie: since the last
        time we scanned this mailbox) added to the mailbox.

        This helps us find messages that are added not at the end of the
        mailbox.

        Arguments:
        - `msgs`: The list of messages we are going to check.
        - `horizon`: The delta back in time from the mtime of the folder we use
          as the mtime for messages considered 'new'
        """

        # We use self.mtime (instead of getting the mtime from the actual
        # folder) because we want to find all messages that have been modified
        # since the folder was last scanned.
        #
        # Since we are looking for the first modified message we can stop our
        # scan the instant we find a msg with a mtime greater than self.mtime.
        #
        if len(msgs) == 0:
            return 0
        horizon_mtime = self.mtime - horizon
        for msg in msgs:
            # XXX Ug, hate having to get the path this way..
            #
            if int(os.path.getmtime(os.path.join(self.mailbox._path,str(msg)))) > horizon_mtime:
                break
        self.log.debug("first_new_message is %d (out of [%d..%d])" % \
                       (msg,msgs[0],msgs[-1]))
        return msg

    ##################################################################
    #
    def _find_msg_without_uidvv(self, msgs):
        """
        This is a helper function for 'resync()'

        It looks through the folder from the highest numbered message down to
        find for the first message a valid uid_vv.

        In general messages are only added to the end of a folder and this is
        usually just a fraction of the entire folder's contents. Even after a
        repack you do not need to rescan the entire folder.

        This quickly lets us find the point at the end of the folder where
        messages with a missing or invalid uid_vv are.

        This works even for the rare first-run case on a large folder which has
        no messages we have added uid_vv/uid's to (it just takes longer..but it
        was going to take longer anyways.)

        We return the first message we find that has a valid uid_vv (or the
        first message in the folder if none of them have valid a uid_vv.)

        Arguments:
        - `msgs`: the list of messages we are going to look through (in reverse)
        """
        for msg in sorted(msgs, reverse = True):
            try:
                fp = self.mailbox.get_file(msg)
                uid_vv, uid = get_uid_from_file(fp, msg, self.log)
            finally:
                fp.close()

            # If we find a valid uid_vv then we are done.
            #
            if uid_vv == self.uid_vv:
                return msg

        # We get here we ran through the entire list of messages and did not
        # find one with a valid uuid_vv. We return the first message in the
        # folder.
        #
        return msg

    ##################################################################
    #
    def _check_update_msg_uids(self, msgs):
        """
        This will loop through all of the msgs whose keys were passed in the
        msgs list. We assume these keys are in order. We see if they have
        UID_VV.UID's in them. If they do not or it is out of sequence (UID's
        must be monotonically increasing at all times) then we have to generate
        new UID's for every message after the out-of-sequence one we
        encountered.

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

        Arguments:
        - `msgs`: A list of all of the message keys. Since we already looked
          this information up in the function that is calling us there is
          little point in diving back down to the disk to enumerate the list of
          message keys again.
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
        for msg in msgs:
            # If we are not redoing the rest of the folder check to see if
            # this messages uid_vv / uid is what we expect.
            #
            if not redoing_rest_of_folder:
                try:
                    fp = self.mailbox.get_file(msg)
                    uid_vv, uid = get_uid_from_file(fp, msg, self.log)
                finally:
                    fp.close()

                # If the uid_vv is different or the uid is NOT
                # monotonically increasing from the previous uid then
                # we have to redo the rest of the folder.
                #
                if uid_vv != self.uid_vv or uid <= prev_uid or \
                        uid_vv == None or uid == None:
                    redoing_rest_of_folder = True
                    self.log.debug("Found uid_vv/uid out of sequence. Redoing rest of folder.")
                else:
                    prev_uid = uid

            # at this point we MAY be redoing this message (and the rest of the
            # folder) so we check again.. if we are not then skip to the next
            # iteration of this loop.
            #
            if redoing_rest_of_folder:
                try:
                    fp = self.mailbox.get_file(msg)
                    full_msg = mailbox.MHMessage(HeaderParser().parse(fp))
                finally:
                    fp.close()

                # Remove the old header if it exists and add the new one
                #
                self.next_uid +=1
                new_uid = "%010d.%010d" % (self.uid_vv, self.next_uid)
                del full_msg['X-asimapd-uid']
                full_msg['X-asimapd-uid'] = new_uid
                self.mailbox[msg] = full_msg

        # If we had to redo the folder then we believe it is indeed now
        # interesting so set the \Marked attribute on it.
        #
        if redoing_rest_of_folder:
            self.marked(True)

        # And we are done..
        #
        return

    ##################################################################
    #
    def _restore_from_db(self):
        """
        Restores this mailbox's persistent state from the database.  If this
        mailbox does not exist in the db we create an entry for it with
        defaults.

        We return True if we restored the data from the db.

        We return False if we had to create the record for this mailbox in the
        db.
        """
        c = self.server.db.cursor()
        c.execute("select id, uid_vv,attributes,mtime,next_uid,num_msgs,"
                  "num_recent from mailboxes where name=?", (self.name,))
        results = c.fetchone()

        # If we got back no results than this mailbox does not exist in the
        # database so we need to create it.
        #
        if results is None:

            # Upon create the entry in the db reflects what is on the disk as
            # far as we know.
            #
            self.mtime = int(os.path.getmtime(self.mailbox._path))
            self.uid_vv = self.server.get_next_uid_vv()
            c.execute("insert into mailboxes (id, name, uid_vv, attributes, "
                      "mtime, next_uid, num_msgs, num_recent) "
                      "values (NULL,?,?,?,?,?,?,0)", \
                          (self.name,
                           self.uid_vv,
                           ",".join(self.attributes),
                           self.mtime,
                           self.next_uid,
                           len(self.mailbox.keys())))

            # After we insert the record we pull it out again because we need
            # the mailbox id to relate the mailbox to its sequences.
            #
            c.execute("select id from mailboxes where name=?", (self.name,))
            results = c.fetchone()
            self.id = results[0]

            # For every sequence we store it in the db also so we can later on
            # do smart diffs of sequence changes between mailbox resyncs.
            #
            self.sequences = self.mailbox.get_sequences()
            for name,values in self.sequences.iteritems():
                c.execute("insert into sequences (id,name,mailbox_id,sequence) "
                          "values (NULL,?,?,?)",
                          (name,self.id,
                           ",".join([str(x) for x in values])))
            c.close()
            self.server.db.commit()
            return False
        else:

            # We got back an actual result. Fill in the values in the mailbox.
            #
            self.id,self.uid_vv,attributes,self.mtime,self.next_uid,self.num_msgs,self.num_recent = results
            self.attributes = set(attributes.split(","))

            # And fill in the sequences we find for this mailbox.
            #
            results = c.execute("select name, sequence from sequences where "
                                "mailbox_id=?", (self.id,))
            for row in results:
                name,values = row
                self.sequences[name] = set([int(x) for x in values.split(",")])
            c.close()
        return True

    ##################################################################
    #
    def _commit_to_db(self):
        """
        Write the state of the mailbox back to the database for persistent
        storage.
        """
        c = self.server.db.cursor()
        c.execute("update mailboxes set uid_vv=?, attributes=?, next_uid=?,"
                  "mtime=?, num_msgs=?, num_recent=? where id=?",
                  (self.uid_vv,
                   ",".join(self.attributes),
                   self.next_uid,
                   self.mtime,
                   self.num_msgs,
                   self.num_recent,
                   self.id))

        # For the sequences we have to do a fetch before a store because we
        # need to delete the sequence entries from the db for sequences that
        # are no longer in this mailbox's list of sequences.
        #
        old_names = set()
        r =c.execute("select name from sequences where mailbox_id=?",(self.id,))
        for row in r:
            old_names.add(row[0])
        new_names = set(self.sequences.keys())

        names_to_delete = old_names.difference(new_names)
        names_to_insert = new_names.difference(old_names)
        names_to_update = new_names.intersection(old_names)
        if len(names_to_delete) > 0:
            self.log.debug("Deleting sequences %s from db", str(names_to_delete))
        for name in names_to_delete:
            c.execute("delete from sequences where mailbox_id=? and name=?",
                  (self.id,name))
        if len(names_to_insert) > 0:
            self.log.debug("Inserting sequences %s from db", str(names_to_insert))
        for name in names_to_insert:
            c.execute("insert into sequences (id,name,mailbox_id,sequence) "
                      "values (NULL,?,?,?)",
                      (name,self.id,
                       ",".join([str(x) for x in self.sequences[name]])))
        if len(names_to_insert) > 0:
            self.log.debug("Updating sequences %s from db", str(names_to_update))
        for name in names_to_update:
            c.execute("update sequences set sequence=? where mailbox_id=? "
                      "and name=?",
                      (",".join([str(x) for x in self.sequences[name]]),
                       self.id, name))
        c.close()
        self.server.db.commit()
        return

    ##################################################################
    #
    def selected(self, client):
        """
        This mailbox is being selected by a client.

        Add the client to the dict of clients that have this mailbox selected.
        Resets the self.expiry attribute to None.

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

        # A client has us selected. Turn of the expiry time.
        #
        self.expiry = None

        try:
            self.mailbox.lock()
            # When a client selects a mailbox we do a resync to make sure we
            # give it up to date information.
            #
            self.resync()

            # Add the client to the mailbox _after_ we do the resync. This way
            # we will not potentially send EXISTS and RECENT messages to the
            # client twice.
            #
            self.clients[client.client.port] = client

            # Now send back messages to this client that it expects upon
            # selecting a mailbox.
            #
            # Flags on messages are represented by being in an MH sequence.
            # The sequence name == the flag on the message.
            #
            # NOTE: '\' is not a permitted character in an MH sequence name so
            #       we translate "Recent" to '\Recent'
            #
            seq = self.mailbox.get_sequences()
            mbox_keys = self.mailbox.keys()
            client.client.push("* %d EXISTS\r\n" % len(mbox_keys))
            if "Recent" in seq:
                client.client.push("* %d RECENT\r\n" % \
                                            len(seq["Recent"]))
            else:
                client.client.push("* 0 RECENT\r\n")
            if "unseen" in seq:
                # Message id of the first message that is unseen.
                #
                first_unseen = sorted(seq['unseen'])[0]
                first_unseen = mbox_keys.index(first_unseen) + 1
                client.client.push("* OK [UNSEEN %d]\r\n" % first_unseen)
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
            self.log.debug("unselected(): No clients, starting expiry timer")
            self.expiry = time.time() + 900 # Expires in 15 minutes
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

        log = logging.getLogger("%s.%s.create()" % (__name__,cls.__name__))
        # If the mailbox already exists than it can not be created. One
        # exception is if the mailbox exists but with the "\Noselect"
        # flag.. this means that it was previously deleted and sitting in its
        # place is a phantom mailbox. In this case we remove the '\Noselect'
        # flag and return success.
        #
        try:
            mbox = server.get_mailbox(name, expiry = 0)
        except NoSuchMailbox:
            mbox = None

        # See if the mailbox exists but with the '\Noselect' attribute. This
        # will basically make the mailbox selectable again.
        #
        if mbox:
            if '\\Noselect' in mbox.attributes:
                mbox.attributes.remove('\\Noselect')
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
            mbox = server.get_mailbox(m, expiry = 0)

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
        log = logging.getLogger("%s.%s.delete()" % (__name__,cls.__name__))

        if name == "inbox":
            raise InvalidMailbox("You are not allowed to delete the inbox")

        mailbox = server.get_mailbox(name, expiry = 0)

        do_delete = False
        try:
            mailbox.mailbox.lock()
            inferior_mailboxes = mailbox.mailbox.list_folders()

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
            mailbox.mailbox.clear()
            for client in mailbox.clients.itervalues():
                client.client.push("* 0 EXISTS\r\n")
                client.client.push("* 0 RECENT\r\n")

            # If the mailbox has inferior mailboxes then we do not actually
            # delete it. It gets the '\Noselect' flag though. It also gets a
            # new uid_vv so that if it is recreated before being fully removed
            # from the db no imap client will confuse it with the existing
            # mailbox.
            #
            if len(inferior_mailboxes) > 0:
                mailbox.attributes.add("\\Noselect")
                mailbox.uid_vv = server.get_next_uid_vv()
                mailbox._commit_to_db()
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
                for client in mailbox.clients.itervalues():
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
            mailbox.mailbox.close()

        # And remove the mailbox from the filesystem.
        #
        if do_delete:
            server.mailbox.remove_folder(name)
        return

    ####################################################################
    #
    @classmethod
    def rename(cls, old_name, new_name, server):
        """
        Rename a mailbox from odl_name to new_name.

        It is an error to attempt to rename from a mailbox name that does not
        exist or to a mailbox name that already exists.

        Renaming INBOX will create a new mailbox, leaving INBOX empty.

        Arguments:
        - `cls`: Mailbox class
        - `old_name`: the original name of the mailbox
        - `new_name`: the new name of the mailbox
        - `server`: the user server object
        """

        ####################################################################
        #
        def change_name(mbox, new_name, c):
            """
            Internal function.. recursively updates mailbox names when a parent
            mailbox has been renamed.

            Arguments:
            - `mbox`:
            - `new_name`:
            - `c`: open database cursor to use
            """
            affected_mailboxes.append(mbox)
            old_name = mbox.name
            log.debug("change_name: old name: %s, new name: %s" % (old_name,
                                                                   new_name))
            del mbox.server.active_mailboxes[old_name]
            mbox.name = new_name
            mbox.server.active_mailboxes[new_name] = mbox
            c.execute("update mailboxes set name=? where id=?", (new_name,
                                                                 mbox.id))
            # Now do the same for any subfolders.
            #
            for mbox_name in mbox.mailbox.list_folders():
                old_name = os.path.join(old_name, mbox_name)
                new_name = os.path.join(new_name, mbox_name)
                mbox = mbox.server.get_mailbox(old_name, expiry = 0)
                change_name(mbox, new_name, c)
            return
        #
        ####################################################################

        log = logging.getLogger("%s.%s.rename()" % (__name__,cls.__name__))
        mbox = server.get_mailbox(old_name, expiry = 0)

        # The mailbox we are moving to must not exist.
        #
        try:
            tmp = server.mailbox.get_folder(new_name)
        except mailbox.NoSuchMailboxError:
            pass
        else:
            raise MailboxExists("Destination mailbox '%s' exists" % new_name)

        # Inbox is handled specially.
        #
        if mbox.name.lower() != "inbox":
            # This is a recursive function that carries out the work on this
            # mailbox and all inferior mailboxes of it.
            #
            affected_mailboxes = []
            c = server.db.cursor()
            change_name(mbox, new_name, c)
            c.close()

            # Rename the top folder in the file system.
            #
            old_dir = mbox.mailbox._path
            new_dir = os.path.join(os.path.dirname(mbox.mailbox._path),
                                   os.path.basename(new_name))
            log.debug("rename(): renaming dir '%s' to '%s'" % (old_dir,new_dir))
            os.rename(old_dir, new_dir)
            server.db.commit()

            # Go through all the of the mailboxes affected by this rename and
            # make sure that their associated mailbox.MH() objects are
            # re-generated.
            #
            for m in affected_mailboxes:
                m.mailbox = server.mailbox.get_folder(m.name)
            return
        else:
            # when you rename 'inbox' what happens is you create a new mailbox
            # with the new name and all messages in 'inbox' are moved to this
            # new mailbox. Inferior mailboxes of 'inbox' are unchanged and not
            # copied.
            #
            cls.create(new_name, server)

            # Now move all the messages currently in inbox to the new mailbox.
            #
            new_mbox = server.get_mailbox(new_name, expiry = 0)
            try:
                mbox.mailbox.lock()
                new_mbox.mailbox.lock()
                for key in mbox.mailbox.iterkeys():
                    try:
                        fp = mbox.mailbox.get_file(key)
                        full_msg = mailbox.MHMessage(HeaderParser().parse(fp))
                    finally:
                        fp.close()
                    new_mbox.next_uid +=1
                    new_uid = "%010d.%010d" % (new_mbox.uid_vv,
                                               new_mbox.next_uid)
                    del full_msg['X-asimapd-uid']
                    full_msg['X-asimapd-uid'] = new_uid
                    new_mbox.add(full_msg)

                mbox.clear()
                mbox.resync()
                new_mbox.resync()
            finally:
                mbox.mailbox.unlock()
                new_mbox.mailbox.unlock()
        return
    
    ####################################################################
    #
    @classmethod
    def list(cls, ref_mbox_name, mbox_match, server):
        """
        This returns a list of tuples of mailbox names and that mailboxes
        attributes. The list is generated from the mailboxes db shelf. The
        'ref_mbox_name' defines the prefix of the mailboxes that will
        match. ie: the mailbox name must begin with ref_mbox_name.

        mbox_match is a pattern that determines which of the subset that match
        ref_mbox_name will be returned to our caller.

        The tricky part is that we need to re-interpret 'mbox_match' as a
        regular expression that we can apply to our mbox names.

        As near as I can tell '*' is like the shell glob pattern - it matches
        zero or more characters.

        '%' is special in that it matches zero or more characters, but not the
        character that separates the hierarchies of mailboxes (ie: '/' in our
        case.)

        So we should be able to get away with: '*' -> '.*', '%' -> '[^/]*' We
        also need to escape any characters that could be interpreted as part of
        a regular expression.
        
        Arguments:
        - `cls`: the mailbox class (this is a clas method)
        - `server`: the user server object instance
        - `ref_mbox_name`: The reference mailbox name
        - `mbox_match`: The pattern of mailboxes to match under the reference
          mailbox name.
        """

        log = logging.getLogger("%s.%s.list()" % (__name__,cls.__name__))

        # The mbox_match character can not begin with '/' because our mailboxes
        # are unrooted.
        #
        if len(mbox_match) > 0 and mbox_match[0] == '/':
            mbox_match = mbox_match[:1]

        # we use normpath to collapse redundant separators and up-level
        # references. But normpath of "" == "." so we make sure that case is
        # handled.
        #
        if mbox_match != "":
            mbox_match = os.path.normpath(mbox_match)

        # Now we tack the ref_mbox_name and mbox_match together.
        #
        mbox_match = os.path.join(ref_mbox_name, mbox_match)
        log.debug("mailbox match pattern: '%s'" % mbox_match)

        # We need to escape all possible regular expression characters
        # in our string so that it only matches what is expected by the
        # imap specification.
        #
        mbox_match = "^" + re.escape(mbox_match) + "$"

        # Every '\*' becomes '.*' and every % becomes [^/]
        #
        mbox_match = mbox_match.replace(r'\*', r'.*').replace(r'\%', r'[^\/]*')
        log.debug("mailbox match regexp: '%s'" % mbox_match)
        results = []
        c = server.db.cursor()
        r = c.execute("select name,attributes from mailboxes where name regexp ?", (mbox_match,))
        for row in r:
            mbox_name, attributes = row
            attributes = set(attributes.split(","))
            results.append((mbox_name, attributes))
        c.close()
        return results
