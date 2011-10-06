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
from datetime import datetime

# asimap import
#
import asimap.utils
import asimap.search
from asimap.exceptions import No, Bad
from asimap.constants import SYSTEM_FLAGS, PERMANENT_FLAGS, SYSTEM_FLAG_MAP
from asimap.constants import REVERSE_SYSTEM_FLAG_MAP, seq_to_flag, flag_to_seq
from asimap.parse import REPLACE_FLAGS, ADD_FLAGS, REMOVE_FLAGS
from asimap.fetch import FetchAtt

# RE used to see if a mailbox being created is just digits.
#
digits_re = re.compile(r'^[0-9]+$')

##################################################################
##################################################################
#
class MailboxException(No):
    def __init__(self, value = "no"):
        self.value = value
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
        self.next_uid = 1
        self.num_msgs = 0
        self.num_recent = 0
        self.uids = []

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
        # XXX We almost always call resync after getting a mailbox and
        #     operating on it causing back to back resyncs.. should we skip
        #     this one?
        #
        self.resync(force = not force_resync, optional = False)
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
    def resync(self, force = False, notify = True, only_notify = None,
               dont_notify = None, publish_uids = False, optional = True):
        """
        This will go through the mailbox on disk and make sure all of the
        messages have proper uuid's, make sure we have a .mh_sequences file
        and that it is up to date with what messages are in the 'seen'
        sequence.

        This is also what controls setting the '\Marked' and '\Unmarked' flags
        on the mailbox as well as marking individual messages as '\Recent'

        We have a '\Seen' flag and we derive this by seeing what messages are
        in the unseen sequence.

        Since the definition of '\Recent' in rfc3501 is a bit vague on when the
        \Recent flag is reset (when you select the folder they all get reset?
        But then how do you find them? It makes little sense) I am going to
        define a \Recent message as any message whose mtime is at least one
        hour before the mtime of the folder.

        This way all new messages are marked '\Recent' and eventually as the
        folder's mtime moves forward with new messages messages will lose their
        '\Recent' flag.

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

        - `notify`: If this is False we will NOT send an EXISTS message when
          we resync the mailbox. This is likely because the size of the mailbox
          has shrunk in size due to an expunge and we are not allowed to send
          EXISTS that reduce the number of messages in the mailbox. Those
          clients that have not gotten the expungues should get them the next
          time they do a command.

        - `only_notify`: A client. If this is set then when we do our exists
          updates we will ONLY sent exists/recent messages to the client passed
          in via `only_notify` (and those clients in IDLE)

        - `dont_notify`: A client. If this is set then if we issue and
          FETCH messages we will NOT include this client.

        - `publish_uids`: If True when we issue FETCH messages for messages
          with changed flags then we also include the message's UID. This is
          likely triggered by a UID STORE command in which we must include the
          UID. It is okay to send this to all clients because even if they did
          not ask for the UID's they should be okay with getting that info.

        - `optional`: If this is True then this entire resync will be skipped
          as a no-op if the mtime on the folder is NOT different than the mtime
          in self.mtime. Watching the server do its thing almost all of the
          resyncs could be skipped because the state on the folder had not
          changed. This is made to be a flag because there are times when we
          want a resync to happen even if the mtime has not changed. The result
          of a STORE command that changes flags on a message and us needing to
          send FETCH's to clients listening to this mailbox is an example of
          this.
        """

        # Get the mtime of the folder at the start so when we need to check to
        # see if we need to do a full scan we have this value before anything
        # we have done in this routine has a chance to modify it.
        #
        start_mtime = int(os.path.getmtime(self.mailbox._path))
        seq_mtime =  int(os.path.getmtime(os.path.join(self.mailbox._path,
                                                          ".mh_sequences")))
        # If `optional` is set and the mtime is the same as what is on disk
        # then we can totally skip this resync run.
        #
        if optional and start_mtime == self.mtime and seq_mtime == self.mtime:
            self.log.debug("Skipping resync")
            return

        self.log.debug("Starting resync")
        # If only_notify is not None then notify is forced to False.
        #
        if only_notify is not None:
            notify = False

        try:
            self.mailbox.lock()
            # Whenever we resync the mailbox we update the sequence for 'seen'
            # based on 'seen' are all the messages that are NOT in the
            # 'unseen' sequence.
            #
            msgs = self.mailbox.keys()
            seq = self.mailbox.get_sequences()
            if 'unseen' in seq:
                # Create the 'Seen' sequence by the difference between all the
                # messages in the mailbox and the unseen ones.
                #
                seq['Seen'] = list(set(msgs) - set(seq['unseen']))
            else:
                # There are no unseen messages in the mailbox thus the Seen
                # sequence mirrors the set of all messages.
                #
                seq['Seen'] = msgs

            # If the list of uids is empty but the list of messages is not then
            # force a full resync of the mailbox.. likely this is just an
            # initial data problem and does not require rewriting every
            # message.
            #
            if len(self.uids) == 0 and len(msgs) > 0:
                self.log.debug("resync: len uids: %d, len msgs: %d, forcing resync" % (len(self.uids), len(msgs)))
                force = True

            # Loop through all messages getting their mtime. If a message's
            # mtime is no more than one hour before the mtime of the folder
            # then it gets added to the recent sequence.
            #
            recent = []
            horizon = start_mtime - 3600
            for msg in msgs:
                if os.path.getmtime(os.path.join(self.mailbox._path,
                                                 str(msg))) > horizon:
                    recent.append(msg)
            if len(recent) > 0:
                seq['Recent'] = recent
            elif 'Recent' in seq:
                del seq['Recent']

            # A mailbox gets '\Marked' if it has any unseen messages or
            # '\Recent' messages.
            #
            if 'unseen' in seq or 'Recent' in seq:
                self.marked(True)
            else:
                self.marked(False)

            self.mailbox.set_sequences(seq)

            check_all_start = time.time()

            # NOTE: We handle a special case where the db was reset.. if the
            #       last message in the folder has a uid greater than what is
            #       stored in the folder then set that plus to be the next_uid,
            #       and force a resync of the folder.
            #
            if len(msgs) > 0:
                uid_vv, uid = self.get_uid_from_msg(msgs[-1])
                if uid is not None and uid_vv is not None and \
                        uid_vv == self.uid_vv and uid >= self.next_uid:
                    self.log.warn("resync: last message uid: %d, next_uid: "
                                  "%d - mismatch forcing full resync" % \
                                      (uid, self.next_uid))
                    self.next_uid = uid+1
                    force = True

                # Now comes the task that can take an obscene amount of time to
                # accomplish thus we apply a few tricks to reduce when we do
                # it, and if we do it, account for the most common case which
                # is considerably cheaper to do.
                #
                # Usually we ONLY need to rescan the new messages that have
                # been added to the folder.
                #
                # Scan forward through the mailbox to find the first message
                # with an mtime > the folder's mtime - 30sec. This makes sure
                # we check all messages that would have been added to this
                # folder since our last automatic resync check.
                #
                # Scan back from the end of the mailbox until we find the first
                # message that has a uid_vv that matches the uid_vv of our
                # folder.
                #
                # Given these two references to a message choose the lower of
                # the two and scan from that point forward.
                #
                if force == True:
                    self.log.debug("rescanning all %d messages" % len(msgs))
                    self.uids = [None for x in range(len(msgs))]
                    self._check_update_msg_uids(msgs, 0, seq)
                else:
                    first_new_msg = self._find_first_new_message(msgs,
                                                                 horizon=30)
                    first_msg_wo_uid = self._find_msg_without_uidvv(msgs)
                    if first_new_msg or first_msg_wo_uid:
                        start = min(x for x in [first_new_msg,first_msg_wo_uid] if x is not None)
                        # If the start_idx is more than one beyond the end of
                        # self.uids then we have a problem in that there seems
                        # to be a gap that should not be in self.uids, so we
                        # will for a total resync.
                        #
                        # Otherwise we extend self.uids by the number of
                        # messages we are going to rescan.
                        #
                        start_idx = msgs.index(start)
                        if start_idx + 2 > len(self.uids):
                            self.log.warn("resync: start_idx: %d, length "
                                          "of self.uids: %s. Doing full "
                                          "resync" % (start_idx,
                                                      len(self.uids)))
                            start_idx = 0
                            self.uids = [None for x in range(len(msgs))]
                        elif len(msgs) > len(self.uids):
                            # If the number of messages in the folder is larger
                            # than the number of entries in self.uids then
                            # extend self.uids by the right amount so that they
                            # have the same number of elements.
                            #
                            self.log.debug("resync: Extending self.uids "
                                           "by %d elements" % \
                                               len(msgs)-len(self.uids))
                            self.uids.extend([None for x in range(len(msgs)-len(self.uids))])
                        elif len(msgs) < len(self.uids):
                            # We have fewer messages than entries in self.uids
                            # so truncate self.uids down to be the same length.
                            self.log.debug("resync: truncating self.uids "
                                           "down to %d elements" % len(msgs))
                            self.uids = self.uids[:len(msgs)]
                        self.log.debug("rescanning from %d to %d" % \
                                           (start, msgs[-1]))
                        self._check_update_msg_uids(msgs[start_idx:],
                                                    start_idx, seq)
            else:
                # number of messages in the mailbox is zero.. make sure our
                # list of uid's for this mailbox is also empty.
                #
                if len(self.uids) != 0:
                    self.log.warn("resync: Huh, list of msgs is empty, but "
                                  "list of uid's was not. Emptying.")
                    self.uids = []

            # Before we finish if the number of messages in the folder or the
            # number of messages in the Recent sequence is different than the
            # last time we did a resync then this folder is intersted (\Marked)
            # and we need to tell all clients listening to this folder about
            # its new sizes.
            #
            seq = self.mailbox.get_sequences()
            num_recent = 0
            if 'Recent' in seq:
                num_recent = len(seq['Recent'])

            # NOTE: Only send EXISTS messages if notify is True and the client
            # is not idling and the client is not the one passed in via
            # 'only_notify'
            #
            if len(msgs) != self.num_msgs or \
                    num_recent != self.num_recent:
                # Notify all listening clients that the number of messages and
                # number of recent messages has changed.
                #
                for client in self.clients.itervalues():
                    if notify or \
                       client.idling or \
                       (only_notify is not None and \
                        only_notify.client.port == client.client.port):
                        client.client.push("* %d EXISTS\r\n" % len(msgs))
                        client.client.push("* %d RECENT\r\n" % num_recent)

            # Make sure to update our mailbox object with the new counts.
            #
            self.num_msgs = len(msgs)
            self.num_recent = num_recent

            # Now if any messages have changed which sequences they are from
            # the last time we did this we need to issue untagged FETCH %d
            # (FLAG (..)) to all of our active clients. This does not suffer
            # the same restriction as EXISTS, RECENT, and EXPUNGE.
            #
            self._compute_and_publish_fetches(msgs, seq, dont_notify,
                                              publish_uids = publish_uids)

            # And see if the folder is getting kinda 'gappy' with spaces
            # between message keys. If it is, pack it.
            #
            self.sequences = seq
            self._pack_if_necessary(msgs)

        finally:
            self.mailbox.unlock()

        # And update the mtime before we leave..
        #
        self.mtime = int(os.path.getmtime(self.mailbox._path))
        self.commit_to_db()
        self.log.debug("resync: Complete.")
        return

    ##################################################################
    #
    def _compute_and_publish_fetches(self, msgs, seqs, dont_notify = None,
                                     publish_uids = False):
        """
        A helper function for resync()

        We see what messages have been added to any of the sequences since the
        last time self.sequences was synchornized with what is on disk.

        For every message that is a member of a sequence that was not a member
        previously we issue a "FETCH" to every client listening to this
        mailbox.

        The "FETCH" lists all of the flags associated with that message.

        We _skip_ the client that is indicated by 'dont_notify'

        Arguments:
        - `msgs`: A list of all of the message keys in this folder
        - `seqs`: The latest representation of what the on disk sequences are
        - `dont_notify`: The client to NOT send "FETCH" notices to
        - `publish_uids`: If this is true then ALSO include the messages UID in
          the FETCH response
        """
        # We build up the set of messages that have changed flags
        #
        changed_msgs = set()

        # If any sequence exists now that did not exist before, or does not
        # exist now but did exist before then all of those messages in those
        # sequences have changed flags.
        #
        for seq in set(seqs.keys()) ^ set(self.sequences.keys()):
            if seq in seqs:
                changed_msgs |= set(seqs[seq])
            if seq in self.sequences:
                changed_msgs |= set(self.sequences[seq])

        # Now that we have handled the messages that were in sequences that do
        # not exist in one of seqs or self.sequences go through the sequences
        # in seqs. For every sequence if it is in self.sequences find out what
        # messages have either been added or removed from these sequences and
        # add it to the set of changed messages.
        #
        for seq in seqs.keys():
            if seq not in self.sequences:
                continue
            changed_msgs |= set(seqs[seq]) ^ set(self.sequences[seq])

        # Now eliminate all entries in our changed_msgs set that are NOT in
        # msgs. We can not send FETCH's for messages that are no longer in the
        # folder.
        #
        # NOTE: XXX a 'pack' of a folder is going to cause us to send out many
        #       many FETCH's and most of these will be meaningless and
        #       basically noops. My plan is that pack's will rarely be done
        #       outside of asimapd, and asimapd will have a strategy for doign
        #       occasional packs at the end of a resync and when it does it
        #       will immediately update the in-memory copy of the list of
        #       sequences so that the next time a resync() is done it will not
        #       think all these messages have had their flags changed.
        #
        changed_msgs = changed_msgs & set(msgs)

        # And go through each message and publish a FETCH to every client with
        # all the flags that this message has.
        #
        for msg in sorted(list(changed_msgs)):
            flags = []
            for seq in seqs.keys():
                if msg in seqs[seq]:
                    flags.append(seq_to_flag(seq))

            # Publish to every listening client except the one we are supposed
            # to ignore.
            #
            flags = " ".join(flags)
            msg_idx = msgs.index(msg) + 1
            for client in self.clients.itervalues():
                if dont_notify and \
                        client.client.port == dont_notify.client.port:
                    continue
                uidstr = ""
                if publish_uids:
                    try:
                        uidstr = " UID %d" % self.uids[msg_idx-1]
                    except IndexError:
                        self.log.error("compute_and_publish: UID command but "
                                       "message index: %d is not inside list "
                                       "of UIDs, whose length is: %d" % \
                                           (msg_idx-1, len(self.uids)))
                client.client.push("* %d FETCH (FLAGS (%s)%s)\r\n" % (msg_idx,
                                                                      flags,
                                                                      uidstr))
        return

    ##################################################################
    #
    def _pack_if_necessary(self, msgs):
        """
        We use the array of message keys from the folder to determine if it is
        time to pack the folder.

        The key is if there is more than a 20% difference between the number of
        messages in the folder and the highest number in the folder and the
        folder is larger than 100. This tells us it has a considerable number
        of gaps and we then call pack on the folder.

        NOTE: Immediately after calling 'pack' we update the in-memory copy of
              the sequences with what is on the disk so that we do not generate
              spurious 'FETCH' messages on the next folder resync().

        Arguments:
        - `msgs`: The list of all message keys in the folder.
        """
        if len(msgs) < 100:
            return

        if float(len(msgs)) / float(msgs[-1]) > 0.8:
            return

        self.mailbox.pack()
        self.sequences = self.mailbox.get_sequences()
        return

    ##################################################################
    #
    def get_uid_from_msg(self, msg):
        """
        Get the uid from the given message (where msg is the integer key into
        the folder.)

        We return the tuple of (uid_vv,uid)

        If the message does NOT have a uid_vv or uid we return None for those
        elements in the tuple.

        Arguments:
        - `msg`: the message key in the folder we want the uid_vv/uid for.
        """

        try:
            fp = self.mailbox.get_file(msg)
            # Look through the header for the 'x-asimapd-uid' header. If we
            # encounter a blank line then we have reached the end of the
            # header.
            #
            for line in fp:
                if len(line.strip()) == 0:
                    return (None, None)
                if line[0:15].lower() == 'x-asimapd-uid: ':
                    break

            # We only get here if we encountered the 'x-asimapde-uid'
            # header. Take the value part of the heade and split it around "."
            #
            uid_vv = None
            uid = None
            try:
                # Convert the strings we parse out of the header to ints.
                #
                uid_vv,uid = [int(x) for x in (line[15:].strip().split('.'))]
            except ValueError:
                self.log.info("get_uid_from_msg: msg %s had malformed uid "
                              "header: %s" % (msg, line))
        finally:
            fp.close()
        return uid_vv, uid

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
            return None
        horizon_mtime = self.mtime - horizon
        found = None
        for msg in msgs:
            # XXX Ug, hate having to get the path this way..
            #
            if int(os.path.getmtime(os.path.join(self.mailbox._path,str(msg)))) > horizon_mtime:
                found = msg
                break
        return found

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
        msgs = sorted(msgs, reverse = True)
        found = None
        for msg in msgs:
            uid_vv, uid = self.get_uid_from_msg(msg)
            if uid_vv == self.uid_vv:
                return found
            else:
                found = msg
        return found

    ##################################################################
    #
    def _check_update_msg_uids(self, msgs, start_index, seq):
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
        - `msgs`: A list of the message keys that we need to check. NOTE: This
          will frequently be a subset of all messages in the folder.

        - `start_index` - where in the list of all messages in this folder
          'msgs' starts. We need this so when we are discovering new UID's we
          can update the self.uids list properly.

        - `seq`: The existing sequences for this folder (may not be in sync
          with self.sequences for differencing purposes, and is passed in to
          save us from having to load them from disk again.
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
        for i, msg in enumerate(msgs):
            # If we are not redoing the rest of the folder check to see if
            # this messages uid_vv / uid is what we expect.
            #
            if not redoing_rest_of_folder:
                uid_vv, uid = self.get_uid_from_msg(msg)

                # If the uid_vv is different or the uid is NOT
                # monotonically increasing from the previous uid then
                # we have to redo the rest of the folder.
                #
                if uid_vv != self.uid_vv or uid <= prev_uid or \
                        uid_vv == None or uid == None:
                    redoing_rest_of_folder = True
                    self.log.debug("Found msg %d uid_vv/uid %s.%s out of "
                                   "sequence. Redoing rest of folder." % \
                                       (msg, uid_vv,uid))
                else:
                    prev_uid = uid
                    self.uids[start_index + i] = uid

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
                new_uid = "%010d.%010d" % (self.uid_vv, self.next_uid)
                self.uids[start_index + i] = self.next_uid
                self.next_uid +=1
                del full_msg['X-asimapd-uid']
                full_msg['X-asimapd-uid'] = new_uid

                # Make sure any sequences that the message is in are handled
                # properly
                #
                for name, key_list in seq.iteritems():
                    if msg in key_list:
                        full_msg.add_sequence(name)

                # Save the updated message back to disk. We get the mtime
                # before we do this so that we can set the mtime back to this
                # AFTER we write the message file. We do not want adding uid's
                # to messges to mess up their mtime (which we use for IMAP
                # 'internal-date' value of a message)
                #
                p = os.path.join(self.mailbox._path, str(msg))
                mtime = os.path.getmtime(p)
                self.mailbox[msg] = full_msg
                os.utime(p, (mtime,mtime))

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
                  "num_recent,uids from mailboxes where name=?", (self.name,))
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
            self.id,self.uid_vv,attributes,self.mtime,self.next_uid,self.num_msgs,self.num_recent,uids = results
            self.attributes = set(attributes.split(","))
            if len(uids) == 0:
                self.uids = []
            else:
                self.uids = [int(x) for x in uids.split(",")]

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
    def commit_to_db(self):
        """
        Write the state of the mailbox back to the database for persistent
        storage.
        """
        values = (self.uid_vv,",".join(self.attributes),self.next_uid,
                  self.mtime,self.num_msgs,self.num_recent,
                  ",".join([str(x) for x in self.uids]),self.id)
        c = self.server.db.cursor()
        c.execute("update mailboxes set uid_vv=?, attributes=?, next_uid=?,"
                  "mtime=?, num_msgs=?, num_recent=?,uids=? where id=?",values)
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
        for name in names_to_delete:
            c.execute("delete from sequences where mailbox_id=? and name=?",
                  (self.id,name))
        for name in names_to_insert:
            c.execute("insert into sequences (id,name,mailbox_id,sequence) "
                      "values (NULL,?,?,?)",
                      (name,self.id,
                       ",".join([str(x) for x in self.sequences[name]])))
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
    def get_and_cache_msg(self, msg_key):
        """
        Get the message associated with the given message key in our mailbox.
        We check the cache first to see if it is there.
        If it is not we retrieve it from the MH folder and add it to the cache.

        Arguments:
        - `msg_key`: message key to look up the message by
        """
        msg = self.server.msg_cache.get(self.name, msg_key)
        if msg = None:
            self.mailbox.get_message(msg_key)
            self.server.msg_cache.add(self.name, msg_key, msg)
            self.debug("add %d to msg cache: %s" % (msg_key,
                                                    str(self.server.msg_cache)))
        return msg

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
            client.client.push("* OK [UIDNEXT %d]\r\n" % self.next_uid)

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

    ##################################################################
    #
    def append(self, message, flags = [], date_time = None):
        """
        Append the given message to this mailbox.
        Set the flags given. We also set the \Recent flag.
        If date_time is not given set it to 'now'.
        The internal date on the message is set to date_time.

        Arguments:
        - `message`: The email.message being appended to this mailbox
        - `flags`: A list of flags to set on this message
        - `date_time`: The internal date on this message
        """
        msg = mailbox.MHMessage(message)
        for flag in flags:
            # We need to translate some flags in to the equivalent MH mailbox
            # sequence name.
            #
            if flag in REVERSE_SYSTEM_FLAG_MAP:
                flag = REVERSE_SYSTEM_FLAG_MAP[flag]
            msg.add_sequence(flag)
        # And messages that are appended to the mailbox always get the \Recent
        # flag
        #
        msg.add_sequence("Recent")
        try:
            self.mailbox.lock()
            key = self.mailbox.add(msg)
            self.log.debug("append: message: %d, sequences: %s" % \
                               (key,", ".join(msg.get_sequences())))

            # if a date_time was supplied then set the mtime on the file to
            # that. We use mtime as our 'internal date' on messages.
            #
            if date_time is not None:
                c = time.mktime(date_time.timetuple())
                os.utime(os.path.join(self.mailbox._path, str(key)),(c,c))
        finally:
            self.mailbox.unlock()
        self.resync()
        return

    ##################################################################
    #
    def expunge(self, client = None):
        """
        Perform an expunge. All messages in the 'Deleted' sequence are removed.

        If a client is passed in then we send untagged expunge messages to that
        client.

        The RFC is pretty clear that we MUST NOT send an untagged expunge
        message to any client if that client has no command in progress so we
        can only send expunge's to the given client immediately.

        Also we can not send an expunge during FETCH, STORE, or SEARCH
        commands.

        However, we CAN (MUST?) send expunges that are pending during all the
        other commands so we need to store up the expunges that we register
        here and during any of those other commands send out the built up
        expunge's.

        I think.

        NOTE: We only store pending expunge messages if there are any clients
              attached to this mailbox (besides the client passed in the
              arguments.)

        NOTE: IDLE is 'in the middle of a command' and is not on the prohibited
              list so we also send untagged expunge messages to any clients in
              IDLE

        Arguments:

        - `client`: the client to send the immediate untagged expunge messags
          to.
        """

        try:
            self.mailbox.lock()
            # If there are no messages in the 'Deleted' sequence then we have
            # nothing to do.
            #
            seq = self.mailbox.get_sequences()
            if 'Deleted' not in seq:
                return

            # Because there was a 'Deleted' sequence we know that there are
            # messages to delete from the folder. This will mess up the index
            # of the message keys in this mailbox's message cache so we are
            # just going to toss the entire message cache for this mailbox.
            #
            self.server.msg_cache.clear_mbox(self.name)

            # See if there any other clients other than the one passed in the
            # arguments and any NOT in IDLE that have this mailbox selected.
            # This tells us whether we need to keep track of these expunges to
            # send to the other clients.
            #
            clients_to_notify = { }
            clients_to_pend = []
            if client is not None:
                clients_to_notify[client.client.port] = client

            for port, c in self.clients.iteritems():
                if c.idling:
                    clients_to_notify[port] = c
                elif port not in clients_to_notify:
                    clients_to_pend.append(c)

            # Now that we know who we are going to send expunges to immediately
            # and who we are going to record them for later sending, go through
            # the mailbox and delete the messages.
            #
            msgs = self.mailbox.keys()
            for msg in seq['Deleted']:
                # XXX Work around a bug in mailbox.MH() that tries to unlock a
                #     closed file handle after deleting the file if the folder
                #     is locked.
                self.mailbox.unlock()
                self.mailbox.discard(msg)
                self.mailbox.lock()
                which = msgs.index(msg) + 1
                for c in clients_to_notify.itervalues():
                    c.client.push("* %d EXPUNGE\r\n" % which)
                for c in clients_to_pend:
                    c.pending_expunges.append("* %d EXPUNGE\r\n" % which)

                # Remove the message from the folder.. and also remove it from
                # our uids to message index mapping. (NOTE: 'which' is in IMAP
                # message sequence order, so its actual position in the array
                # is one less.
                #
                msgs.remove(msg)
                self.uids.remove(self.uids[which-1])
        finally:
            self.mailbox.unlock()

        # Resync the mailbox, but send NO exists messages because the mailbox
        # has shrunk: 5.2.: "it is NOT permitted to send an EXISTS response
        # that would reduce the number of messages in the mailbox; only the
        # EXPUNGE response can do this.
        #
        # Unless a client is sitting in IDLE, then it is okay send them
        # exists/recents.
        #
        self.resync(notify = False)
        return

    ##################################################################
    #
    def search(self, search, uid_command = False):
        """
        Take the given IMAP search object and apply it to all of the messages
        in the mailbox.

        Form a list (by message index) of the messages that match and return
        that list to our caller.

        Arguments:
        - `search`: An IMAPSearch object instance
        - `uid_command`: True if this is for a UID SEARCH command, which means
          we have to return not message sequence numbers but message UID's.
        """
        # Before we do a search we do a resync to make sure that we have
        # attached uid's to all of our messages and various counts are up to
        # sync. But we do it with notify turned off because we can not send any
        # conflicting messages to this client (other clients that are idling do
        # get any updates though.)
        #
        self.resync(notify = False)
        if uid_command:
            self.log.debug("search(): Doing a UID SEARCH")

        results = []
        try:
            self.mailbox.lock()

            # We get the full list of keys instead of using an iterator because
            # we need the max id and max uuid.
            #
            msgs = self.mailbox.keys()
            if len(msgs) == 0:
                return results
            seq_max = len(msgs)
            uid_vv, uid_max = self.get_uid_from_msg(msgs[-1])
            if uid_vv is None or uid_max is None:
                self.resync(notify = False)

            # Go through the messages one by one and pass them to the search
            # object to see if they are or are not in the result set..
            #
            self.log.debug("Applying search to messages: %s" % str(search))
            for idx, msg in enumerate(msgs):
                # IMAP messages are numbered starting from 1.
                #
                i = idx + 1
                ctx = asimap.search.SearchContext(self, msg, i, seq_max,
                                                  uid_max, self.sequences)
                if search.match(ctx):
                    # The UID SEARCH command returns uid's of messages
                    #
                    if uid_command:
                        results.append(ctx.uid)
                    else:
                        results.append(i)
        finally:
            self.mailbox.unlock()
        return results

    #########################################################################
    #
    def fetch(self, msg_set, msg_data_items, uid_command = False):
        """
        Go through the messages in the mailbox. For the messages that are
        within the indicated message set parse them and pull out the data
        indicated by 'msg_data_items'

        Return a list of tuples where the first element is the IMAP message
        sequence number and the second is the requested data.

        The requested data itself is a list of tuples. The first element is the
        name of the data item from 'msg_data_items' and the second is the
        requested data.

        Arguments:
        - `msg_set`: The set of messages we want to
        - `msg_data_items`: The things to fetch for the messags indiated in
          msg_set
        - `uid_command`: True if this is for a UID SEARCH command, which means
          we have to return not message sequence numbers but message UID's.
        """
        # Before we do a fetch we do a resync to make sure that we have
        # attached uid's to all of our messages and various counts are up to
        # sync. But we do it with notify turned off because we can not send any
        # conflicting messages to this client (other clients that are idling do
        # get any updates though.)
        #
        # NOTE: If we are doing a UID FETCH then we are allowed to notify the
        #       client of possible mailbox changs.
        #
        self.resync(notify = uid_command)

        if uid_command:
            self.log.debug("fetch: Doing UID FETCH")

        results = []
        try:
            self.mailbox.lock()

            # We get the full list of keys instead of using an iterator because
            # we need the max id and max uuid.
            #
            msgs = self.mailbox.keys()
            uid_vv, uid_max = self.get_uid_from_msg(msgs[-1])
            seq_max = len(self.mailbox)

            if uid_command:
                # If we are doing a 'UID FETCH' command we need to use the max
                # uid for the sequence max.
                #
                uid_list = asimap.utils.sequence_set_to_list(msg_set, uid_max,
                                                             uid_command)

                # We want to convert this list of UID's in to message indices
                # So for every uid we we got out of the msg_set we look up its
                # index in self.uids and from that construct the msg_idxs
                # list. Missing UID's are fine. They just do not get added to
                # the list.
                #
                msg_idxs = []
                for uid in uid_list:
                    if uid in self.uids:
                        msg_idxs.append(self.uids.index(uid) + 1)

                # Also, if this is a UID FETCH then we MUST make sure UID is
                # one of the fields being fetched, and if it is not add it.
                #
                fetch_found = False
                for mdi in msg_data_items:
                    if mdi.attribute.lower() == "uid":
                        fetch_found = True
                if not fetch_found:
                    msg_data_items.insert(0, FetchAtt("uid"))
            else:
                msg_idxs = asimap.utils.sequence_set_to_list(msg_set, seq_max)

            # Go through each message and apply the msg_data_items.fetch() to
            # it building up a set of data to respond to the client with.
            #
            for idx in msg_idxs:
                ctx = asimap.search.SearchContext(self, msgs[idx-1], idx,
                                                  seq_max, uid_max,
                                                  self.sequences)
                msg_sequences = ctx.msg.get_sequences()
                iter_results = []
                for elt in msg_data_items:
                    # self.log.debug("fetch: %s on %d (key: %d)" % \
                    #                    (str(elt), idx, msgs[idx-1]))
                    iter_results.append(elt.fetch(ctx))

                results.append((idx, iter_results))

                # If the message's sequences has changed from before we did the
                # fetch, rewrite the sequences on disk. A later resync() call
                # will send out any necessary FETCH responses related to
                # changed flags.
                #
                seq_changed = False
                if sorted(msg_sequences) != sorted(ctx.msg.get_sequences()):
                    seq_changed = True
                    self.mailbox._dump_sequences(ctx.msg, msgs[idx-1])
        finally:
            self.mailbox.unlock()
        return (results, seq_changed)

    ##################################################################
    #
    def store(self, msg_set, action, flags, uid_command = False):
        """
        Update the flags (sequences) of the messages in msg_set.

        NOTE: No matter what flags are set/reset etc. \Recent is not affected.

        Arguments:
        - `msg_set`: The set of messages to modify the flags on
        - `action`: one of REMOVE_FLAGS, ADD_FLAGS, or REPLACE_FLAGS
        - `flags`: The flags to add/remove/replace
        - `uid_command`: True if this is for a UID SEARCH command, which means
          we have to return not message sequence numbers but message UID's.
        """
        ####################################################################
        #
        def add_flags(flags, msgs, seqs):
            """
            Helpe function to add a flag to a sequence.

            Also handles removing a message from then Seen/unseen sequences if
            the flag being added is unseen/Seen.

            Arguments:
            - `flags`: The flags being added
            - `msgs`: The messages it is being added to
            - `seqs`: The dict of sequeneces
            """
            for flag in flags:
                if flag not in seqs:
                    seqs[flag] = []
                for msg in msgs:
                    if msg not in seqs[flag]:
                        seqs[flag].append(msg)

                        # If the message exists in the message cache remove it.
                        # XXX we should be smarter and update the message if it
                        #     exists in the message cache.
                        #
                        self.server.msg_cache.remove(self.name, msg)

                    # When we add a message to the Seen sequence make sure
                    # that message is not in the unseen sequence anymore.
                    #
                    if flag == "Seen" and 'unseen' in seqs and \
                            msg in seqs['unseen']:
                        seqs['unseen'].remove(msg)

                    # Conversely, if we are adding a message to the unseen
                    # sequence be sure to remove it from the seen sequence.
                    #
                    if flag == "unseen" and "Seen" in seqs and \
                            msg in seqs['Seen']:
                        seqs['Seen'].remove(msg)
            return
        ####################################################################
        #
        def remove_flags(flags, msgs, sequs):
            """
            Helper function to move a flag from a list of messages.

            Also handles adding a message from then Seen/unseen sequences if
            the flag being removed is unseen/Seen.

            Arguments:
            - `flags`: The flag being added
            - `msgs`: The messages it is being added to
            - `seqs`: The dict of sequeneces
            """
            for flag in flags:
                if flag in seqs:
                    for msg in msgs:
                        if msg in seqs[flag]:
                            seqs[flag].remove(msg)

                            # If we remove a message from the Seen sequence
                            # be sure to add it to the unseen sequence.
                            #
                            if flag == "Seen":
                                if "unseen" in seqs:
                                    seqs["unseen"].append(msg)
                                else:
                                    seqs["unseen"] = [msg]
                            # And conversely, message removed from unseen,
                            # add it to Seen.
                            #
                            if flag == "unseen":
                                if "Seen" in seqs:
                                    seqs["Seen"].append(msg)
                                else:
                                    seqs["Seen"] = [msg]

                    if len(seqs[flag]) == 0:
                        del seqs[flag]
            return

        #
        ##############################################################
        #
        # store() logic begins here:
        #
        if '\\Recent' in flags:
            raise No("You can not add or remove the '\\Recent' flag")

        if uid_command:
            self.log.debug("fetch: Doing UID STORE")

        try:
            self.mailbox.lock()

            # Get the list of message keys that msg_set indicates.
            #
            msg_keys = self.mailbox.keys()
            seq_max = len(msg_keys)

            if uid_command:
                # If we are doing a 'UID FETCH' command we need to use the max
                # uid for the sequence max.
                #
                uid_vv, uid_max = self.get_uid_from_msg(msg_keys[-1])
                uid_list = asimap.utils.sequence_set_to_list(msg_set, uid_max,
                                                             uid_command)

                # We want to convert this list of UID's in to message indices
                # So for every uid we we got out of the msg_set we look up its
                # index in self.uids and from that construct the msg_idxs
                # list. Missing UID's are fine. They just do not get added to
                # the list.
                #
                msg_idxs = []
                for uid in uid_list:
                    if uid in self.uids:
                        msg_idxs.append(self.uids.index(uid) + 1)
            else:
                msg_idxs = asimap.utils.sequence_set_to_list(msg_set, seq_max)

            # Build a set of msg keys that are just the messages we want to
            # operate on.
            #
            msgs = [msg_keys[x-1] for x in msg_idxs]

            # Convert the flags to MH sequence names..
            #
            flags = [flag_to_seq(x) for x in flags]
            seqs = self.mailbox.get_sequences()

            # Now for the type of operation involved do the necessasry
            #
            if action == ADD_FLAGS:
                # For every message add it to every sequence in the list.
                #
                add_flags(flags,msgs, seqs)
            elif action == REMOVE_FLAGS:
                # For every message remove it from every seq in flags.
                #
                remove_flags(flags, msgs, seqs)
            elif action == REPLACE_FLAGS:
                seqs_to_remove = set(seqs.keys()) - set(flags)
                seqs_to_remove.discard('Recent')

                # Add new flags to messages.
                #
                add_flags(flags, msgs, seqs)

                # Remove computed flags from messages...
                #
                remove_flags(seqs_to_remove, msgs, seqs)
            else:
                raise Bad("'%s' is an invalid STORE option" % action)

            # And when it is all done save our modified sequences back
            # to .mh_sequences
            #
            self.mailbox.set_sequences(seqs)
        finally:
            self.mailbox.unlock()
        return

    ##################################################################
    #
    def copy(self, msg_set, dest_mbox, uid_command = False):
        """
        Copy the messages in msg_set to the destination mailbox.
        Flags (sequences), and internal date are preserved.
        Messages get the '\Recent' flag in the new mailbox.
        Arguments:
        - `msg_set`: Set of messages to copy.
        - `dest_mbox`: mailbox instance messages are being copied to
        - `uid_command`: True if this is for a UID SEARCH command, which means
          we have to return not message sequence numbers but message UID's.
        """
        if uid_command:
            self.log.debug("copy: Doing UID COPY")

        try:
            self.mailbox.lock()

            # We get the full list of keys instead of using an iterator because
            # we need the max id and max uuid.
            #
            msgs = self.mailbox.keys()
            uid_vv, uid_max = self.get_uid_from_msg(msgs[-1])
            seq_max = len(self.mailbox)
            if uid_command:
                # If we are doing a 'UID FETCH' command we need to use the max
                # uid for the sequence max.
                #
                uid_list = asimap.utils.sequence_set_to_list(msg_set, uid_max,
                                                             uid_command)

                # We want to convert this list of UID's in to message indices
                # So for every uid we we got out of the msg_set we look up its
                # index in self.uids and from that construct the msg_idxs
                # list. Missing UID's are fine. They just do not get added to
                # the list.
                #
                msg_idxs = []
                for uid in uid_list:
                    if uid in self.uids:
                        msg_idxs.append(self.uids.index(uid) + 1)
            else:
                msg_idxs = asimap.utils.sequence_set_to_list(msg_set, seq_max)

            for idx in msg_idxs:
                key = msgs[idx-1] # NOTE: imap messages start from 1.

                # XXX We copy this message the easy way. This may be
                #     unacceptably slow if you are copying hundreds of
                #     messages. Hopefully it will not come down to that but
                #     beware!
                #
                # We do this so we can do the easy way of preserving all the
                # sequences.
                #
                # We get and set the mtime because this is what we use for the
                # 'internal-date' on a message and it SHOULD be preserved when
                # copying a message to a new mailbox.
                #
                p = os.path.join(self.mailbox._path, str(key))
                mtime = os.path.getmtime(p)

                msg = self.get_and_cache_msg(key)
                # msg = self.mailbox.get_message(key)
                new_key = dest_mbox.mailbox.add(msg)

                # new_msg = dest_mbox.mailbox.get_message(new_key)
                dest_mbox.get_and_cache_msg(new_key)
                new_msg.add_sequence('Recent')
                dest_mbox.mailbox[new_key] = new_msg

                p = os.path.join(dest_mbox.mailbox._path, str(new_key))
                os.utime(p, (mtime,mtime))

                self.log.debug("copy: Copied message %d(%d) to %d" % \
                                   (idx, key, new_key))
        finally:
            self.mailbox.unlock()
        return

    #########################################################################
    #
    @classmethod
    def create(cls, name, server):
        """
        Creates a mailbox on disk that does not already exist and
        instantiates a Mailbox object for it.
        """
        # You can not create 'INBOX' nor, because of MH rules, create a mailbox
        # that is just the digits 0-9.
        #
        if name.lower() == "inbox":
            raise InvalidMailbox("Can not create a mailbox named 'inbox'")
        if digits_re.match(name):
            raise InvalidMailbox("Due to MH restrictions you can not create a "
                                 "mailbox that is just digits: '%s'" % name)

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
                mbox.commit_to_db()
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

        # Remember to delete this mailbox from the message cache..
        #
        self.server.msg_cache.clear_mbox(name)

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
            # message in that mailbox to be deleted.
            #
            mailbox.mailbox.clear()

            # If the mailbox has any active clients we set their selected
            # mailbox to None. client.py will know if they try to do any
            # operations that require they have a mailbox selected that this
            # mailbox no longer exists and it will disconnect those clients.
            #
            # A bit rude, but it is the simplest accepted action in this case.
            #
            for client in mailbox.clients.itervalues():
                client.mbox = None
            mailbox.clients = { }

            # If the mailbox has inferior mailboxes then we do not actually
            # delete it. It gets the '\Noselect' flag though. It also gets a
            # new uid_vv so that if it is recreated before being fully removed
            # from the db no imap client will confuse it with the existing
            # mailbox.
            #
            if len(inferior_mailboxes) > 0:
                mailbox.attributes.add("\\Noselect")
                mailbox.uid_vv = server.get_next_uid_vv()
                mailbox.commit_to_db()
            else:
                # We have no inferior mailboxes. This mailbox is gone. If it
                # is active we remove it from the list of active mailboxes
                # and if it has any clients that have it selected they are
                # moved back to the unauthenticated state.
                #
                del server.active_mailboxes[name]

                # Delete all traces of the mailbox from our db.
                #
                c = server.db.cursor()
                c.execute("delete from mailboxes where id = ?", (mailbox.id,))
                c.execute("delete from sequences where mailbox_id = ?",
                          (mailbox_id,))
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
                    new_uid = "%010d.%010d" % (new_mbox.uid_vv,
                                               new_mbox.next_uid)
                    new_mbox.next_uid +=1
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

        # We need to escape all possible regular expression characters
        # in our string so that it only matches what is expected by the
        # imap specification.
        #
        mbox_match = "^" + re.escape(mbox_match) + "$"

        # Every '\*' becomes '.*' and every % becomes [^/]
        #
        mbox_match = mbox_match.replace(r'\*', r'.*').replace(r'\%', r'[^\/]*')
        results = []
        c = server.db.cursor()
        r = c.execute("select name,attributes from mailboxes where name regexp ?", (mbox_match,))
        for row in r:
            mbox_name, attributes = row
            attributes = set(attributes.split(","))

            # INBOX has to be specially named I believe.
            #
            if mbox_name.lower() == "inbox":
                mbox_name = "INBOX"

            results.append((mbox_name, attributes))
        c.close()
        return results
