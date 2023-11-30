"""
The module that deals with the mailbox objects.

There will be a mailbox per MH folder (but not one for the top level
that holds all the folders.)
"""
# system imports
#
import errno
import logging
import os.path
import re
import shutil
import stat
import time
from mailbox import FormatError, MHMessage, NoSuchMailboxError, NotEmptyError
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

# 3rd party imports
#
import aiofiles

# Project imports
#
import asimap.search
import asimap.utils

from .constants import (
    PERMANENT_FLAGS,
    REVERSE_SYSTEM_FLAG_MAP,
    SYSTEM_FLAG_MAP,
    SYSTEM_FLAGS,
    flag_to_seq,
    seq_to_flag,
)
from .exceptions import Bad, MailboxInconsistency, MailboxLock, No
from .fetch import FetchAtt
from .mh import MH
from .parse import ADD_FLAGS, REMOVE_FLAGS, REPLACE_FLAGS
from .utils import UpgradeableReadWriteLock, sequence_set_to_list

# Allow circular imports for annotations
#
if TYPE_CHECKING:
    from .user_server import IMAPClientProxy, IMAPUserServer


logger = logging.getLogger("asimap.mbox")

# The header that is used for holding a messages uid.
#
UID_HDR = "X-asimapd-uid"

# RE used to see if a mailbox being created is just digits.
#
DIGITS_RE = re.compile(r"^[0-9]+$")

# How many seconds after a mailbox instance has no clients before we
# expire it from the list of active mailboxes
#
MBOX_EXPIRY_TIME = 900


####################################################################
#
def mbox_msg_path(mbox: MH, x: str = "") -> Path:
    """
    Helper function for the common operation of getting the path to a
    file inside a mbox.

    Keyword Arguments:
    mbox -- the mailbox object we are getting a path into
    x -- the thing inside the mailbox we are referencing.. typically an
         integer representing a message by number inside the mailbox.
         default: '' (ie: nothing.. just return the path to the mailbox.)
    """
    return Path(mbox._path) / x


##################################################################
##################################################################
#
class MailboxException(No):
    def __init__(self, value="no"):
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
class Mailbox:
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
    def __init__(self, name, server, expiry=900):
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
        self.log = logging.getLogger(
            "{}.{}.{}".format(__name__, self.__class__.__name__, name)
        )
        self.server = server
        self.name = name
        self.id = None
        self.uid_vv = 0
        self.mtime = 0
        self.next_uid = 1
        self.num_msgs = 0
        self.num_recent = 0
        self.uids = []
        self.subscribed = False
        self.lock = UpgradeableReadWriteLock()

        # Some commands can take a significant amount of time to run.  Like
        # when some client asks for detailed information from message headers
        # for _every_ message in a folder of 15,000 messages..
        #
        # Since we want to maintain our single threaded nature of this program
        # we will have to be able to break up work we are doing so that other
        # commands running on other folders get a chance to run while such long
        # running operations progress as rapidly as they can.
        #
        # For this purpose we establish a command queue. If the queue is empty
        # we attempt to run the complete command in one go. However if while
        # processing this command we find that it is going to take a long time
        # and we have a way of breaking up the work between multiple runs, then
        # we do what we can. We mark our progress in the command and put the
        # command in to this queue.
        #
        # If the command queue is not empty when a new command for this folder
        # comes in then that command is appended to the end of this folder.
        #
        # If there are any folders with a non-empty command queue the main user
        # srever aynchat loop will not block waiting for input letting us
        # process existing queued commands without pause while still checking
        # for input from clients.
        #
        # The contents of the command queue are tuples of:
        #  (client, imap_command) because we need to excute command within
        # the context of a client.
        #
        self.command_queue = []

        # Time in seconds since the unix epoch when a resync was last tried.
        #
        self.last_resync = 0

        # NOTE: It is important to note that self.sequences is the value of the
        # sequences we stored in the db from the end of the last resync. As
        # such they are useful for measuring what has changed in various
        # sequences between resync() runs. NOT as a definitive set of what the
        # current sequences in the mailbox are.
        #
        # These are basically updated at the end of each resync() cycle.
        #
        self.sequences = {}

        # You can not instantiate a mailbox that does not exist in the
        # underlying file system.
        #
        try:
            self.mailbox = server.mailbox.get_folder(name)
        except NoSuchMailboxError:
            raise NoSuchMailbox("No such mailbox: '%s'" % name)

        # The list of attributes on this mailbox (this is things such as
        # '\Noselect'
        #
        self.attributes = {r"\Unmarked"}

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
        self.clients: Dict[str, "IMAPClientProxy"] = {}

    ####################################################################
    #
    @classmethod
    async def new(cls, *args, **kwargs):
        """
        We can not have __init__() be an async function, yet we need to do
        some async operations when we instantiate a mailbox. This code is for
        doing both. So this is the entry point to instantiate a mailbox.
        """
        mbox = cls(*args, **kwargs)

        # If the .mh_sequence file does not exist create it.
        #
        # XXX Bad that we are reaching in to the MH object to
        #     find thepath to the sequences file.
        #
        mh_seq_fname = mbox_msg_path(mbox.mailbox, ".mh_sequences")
        if not await aiofiles.os.path.exists(mh_seq_fname):
            f = await aiofiles.open(mh_seq_fname, "rb+")
            await f.close()
            open(mh_seq_fname, "a").close()
            os.chmod(mh_seq_fname, stat.S_IRUSR | stat.S_IWUSR)

        async with mbox.lock.read_lock():
            async with mbox.lock.write_lock():
                # After initial setup fill in any persistent values from the
                # database (and if there are none, then create an entry in the
                # db for this mailbox.)
                #
                force_resync = await mbox._restore_from_db()

            # And make sure our mailbox on disk state is up to snuff and update
            # our db if we need to.
            #
            await mbox.resync(force=not force_resync, optional=False)

    ##################################################################
    #
    def marked(self, bool):
        r"""
        A helper function that toggles the '\Marked' or '\Unmarked' flags on a
        folder (another one of those annoying things in the RFC you really only
        need one of these flags.)

        Arguments:
        - `bool`: if True the \Marked attribute is added to the folder. If
                  False the \Unmarked attribute is added to the folder.
        """
        if bool:
            if r"\Unmarked" in self.attributes:
                self.attributes.remove(r"\Unmarked")
                self.attributes.add(r"\Marked")
        else:
            if r"\Marked" in self.attributes:
                self.attributes.remove(r"\Marked")
                self.attributes.add(r"\Unmarked")
        return

    ####################################################################
    #
    def _update_seen_from_unseen(self, msgs) -> dict[str, list[int]]:
        """
        The sequence 'Seen' is not updated by the maillib.MH class. We have
        to update it manually.

        This function updates or creates the `Seen` sequence based on the
        `unseen` sequence (messages that are not `unseen` are `Seen`)

        Returns the sequences for this folder.

        Presumes that the the folder lock has been acquired.

        Raises a MailboxInconsistency exception if we are unable to read the
        .mh_sequences file.
        """
        try:
            seq = self.mailbox.get_sequences()
        except FormatError as exc:
            logger.exception(
                "Bad `.mh_sequences` for mailbox %s: %s",
                self.mailbox._path,
                exc,
            )
            raise MailboxInconsistency(str(exc))

        if "unseen" in seq:
            # Create the 'Seen' sequence by the difference between all
            # the messages in the mailbox and the unseen ones.
            #
            seq["Seen"] = list(set(msgs) - set(seq["unseen"]))
        else:
            # There are no unseen messages in the mailbox thus the Seen
            # sequence mirrors the set of all messages.
            #
            seq["Seen"] = msgs

        # A mailbox gets '\Marked' if it has any unseen messages or
        # '\Recent' messages.
        #
        if "unseen" in seq or "Recent" in seq:
            self.marked(True)
        else:
            self.marked(False)

        self.mailbox.set_sequences(seq)
        return seq

    ##################################################################
    #
    async def resync(
        self,
        force: bool = False,
        notify: bool = True,
        only_notify=None,
        dont_notify=None,
        publish_uids: bool = False,
        optional: bool = True,
    ):
        r"""
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
        assert self.lock.this_task_has_read_lock()  # XXX remove when confident
        # see if we need to do a full scan we have this value before anything
        # we have done in this routine has a chance to modify it.
        #
        self.last_resync = int(time.time())

        # We do NOT resync mailboxes marked '\Noselect'. These mailboxes
        # essentially do not exist as far as any IMAP client can really tell.
        #
        if r"\Noselect" in self.attributes:
            # We get the write lock because we are updating the db.
            async with self.lock.write_lock():
                self.mtime = await self.get_actual_mtime(
                    self.server.mailbox, self.name
                )
                await self.commit_to_db()
            return

        # Get the mtime of the folder at the start so when we need to check to
        #
        start_mtime = await self.get_actual_mtime(
            self.server.mailbox, self.name
        )

        # If `optional` is set and the mtime is the same as what is on disk
        # then we can totally skip this resync run.
        #
        if optional and start_mtime <= self.mtime:
            return

        # If only_notify is not None then notify is forced to False.
        #
        if only_notify is not None:
            notify = False

        async with (self.lock.write_lock(), self.mailbox.lock_folder()):
            # Whenever we resync the mailbox we update the sequence for
            # 'seen' based on 'seen' are all the messages that are NOT in
            # the 'unseen' sequence.
            #
            msgs: list[int] = await self.mailbox.keys()
            seq = self._update_seen_from_unseen(msgs)

            # If the list of uids is empty but the list of messages is not then
            # force a full resync of the mailbox.. likely this is just an
            # initial data problem for when a `Mailbox` instance is first
            # instantiated and does not require rewriting every message (but
            # requires reading every message)
            #
            if not self.uids and msgs:
                logger.debug(
                    "resync: len uids: %d, len msgs: %d, forcing "
                    "resync" % (len(self.uids), len(msgs))
                )
                force = True

            # If the folder is NOT empty scan for messages that have been added
            # to it by a third party and do not have UID's or the case where
            # the folder's contents have been re-bobbled and the UID's are no
            # longer in strictly ascending order.
            #
            found_uids = self.uids
            start_idx = 0
            if msgs:
                # NOTE: We handle a special case where the db was reset.. if
                #       the last message in the folder has a uid greater than
                #       what is stored in the folder then set that plus to be
                #       the next_uid, and force a resync of the folder.
                #
                uid_vv, uid = self.get_uid_from_msg(msgs[-1])
                if (
                    uid is not None
                    and uid_vv is not None
                    and uid_vv == self.uid_vv
                    and uid >= self.next_uid
                ):
                    logger.warning(
                        "resync: last message uid: %d, next_uid: "
                        "%d - mismatch forcing full resync",
                        uid,
                        self.next_uid,
                    )
                    self.next_uid = uid + 1
                    force = True

                # If the list of cached UID's is longer than the list of
                # messages in the folder then something else besides us has
                # removed messages from this folder (expunge properly keeps
                # msgs and self.uid's in sync). In this case we do not know
                # what messages have been removed so we force a full resync of
                # the folder. This will get us the list of UIDs that are still
                # in the folder and we can then diff this against the list of
                # UIDs that _were_ in the folder and generate the appropriate
                # EXPUNGE messages.
                #
                if len(msgs) < len(self.uids):
                    logger.warning(
                        "resync: number of messages in folder (%d) "
                        "is less than list of cached uids: %d. "
                        "Forcing resync.",
                        len(msgs),
                        len(self.uids),
                    )
                    force = True

                if force:
                    # If force is True then we scan every message in
                    # the folder.  This also clears the cached
                    # messages for this folder and insures that the
                    # self.uid's array is properly filled.
                    #
                    logger.debug(
                        "resync: Forced rescanning all %d " "messages",
                        len(msgs),
                    )
                    self.server.msg_cache.clear_mbox(self.name)
                    found_uids = await self._update_msg_uids(msgs, seq)

                    # Calculate what UID's were deleted and what order they
                    # were deleted in and send expunges as necessary to all
                    # connected clients.
                    #
                    await self.send_expunges(found_uids)
                else:
                    # Usually we ONLY need to rescan the new messages that have
                    # been added to the folder.
                    #
                    # Scan forward through the mailbox to find the first
                    # message with an mtime > the folder's mtime - 30sec. This
                    # makes sure we check all messages that would have been
                    # added to this folder since our last automatic resync
                    # check.
                    #
                    # Scan back from the end of the mailbox until we find the
                    # first message that has a uid_vv that matches the uid_vv
                    # of our folder.
                    #
                    # Given these two references to a message choose the lower
                    # of the two and scan from that point forward.
                    #
                    first_new_msg = self._find_first_new_message(
                        msgs, horizon=30
                    )
                    first_msg_wo_uid = self._find_msg_without_uidvv(msgs)

                    # If either of these is NOT None then we have some subset
                    # of messages we need to scan. If both of these ARE None
                    # then we have determined that there are no new messages to
                    # deal with in the mailbox.
                    #
                    if first_new_msg or first_msg_wo_uid:
                        # Start at the lower of these two message keys.
                        # 'start' is they MH message key. 'start_idx' index in
                        # to the list of message keys for 'start'
                        #
                        start = min(
                            x
                            for x in [first_new_msg, first_msg_wo_uid]
                            if x is not None
                        )
                        start_idx = msgs.index(start)
                        self.log.debug(
                            "resync: rescanning from %d to %d"
                            % (start, msgs[-1])
                        )

                        # Now make 'found_uids' be all the assumed known uid's
                        # _before_ start_index, and all the now newly
                        # discovered uid's at start_index to the end of the
                        # list of messages.
                        #
                        found_uids = self._update_msg_uids(
                            msgs[start_idx:], seq
                        )

                        found_uids = self.uids[:start_idx] + found_uids
                        # Calculate what UID's were deleted and what order they
                        # were deleted in and send expunges as necessary to all
                        # connected clients.
                        #
                        self.send_expunges(found_uids)
            else:
                # number of messages in the mailbox is zero.. make sure our
                # list of uid's for this mailbox is also empty.
                #
                self.server.msg_cache.clear_mbox(self.name)
                if len(self.uids) != 0:
                    self.log.warn(
                        "resync: Huh, list of msgs is empty, but "
                        "list of uid's was not. Emptying."
                    )

                    # Calculate what UID's were deleted and what order they
                    # were deleted in and send expunges as necessary to all
                    # connected clients.
                    #
                    self.send_expunges([])

            # Before we finish if the number of messages in the folder or the
            # number of messages in the Recent sequence is different than the
            # last time we did a resync then this folder is intersted (\Marked)
            # and we need to tell all clients listening to this folder about
            # its new sizes.
            #
            seq = self.mailbox.get_sequences()
            num_recent = 0
            if "Recent" in seq:
                num_recent = len(seq["Recent"])

            # NOTE: Only send EXISTS messages if notify is True and the client
            # is not idling and the client is not the one passed in via
            # 'only_notify'
            #
            if len(msgs) != self.num_msgs or num_recent != self.num_recent:
                # Notify all listening clients that the number of messages and
                # number of recent messages has changed.
                #
                to_notify = []
                for client in self.clients.values():
                    if (
                        notify
                        or client.idling
                        or (
                            only_notify is not None
                            and only_notify.client.port == client.client.port
                        )
                    ):
                        to_notify.append(client.client)

                # NOTE: We separate the generating which clients to notify from
                #       actually pushing messages out to those clients because
                #       if it disconnects due to us sending it we do not want
                #       to raise an exception because the self.clients
                #       dictionary changed.
                #
                for client in to_notify:
                    client.push("* %d EXISTS\r\n" % len(msgs))
                    client.push("* %d RECENT\r\n" % num_recent)

            # Make sure to update our mailbox object with the new counts.
            #
            self.num_msgs = len(msgs)
            self.num_recent = num_recent

            # Now if any messages have changed which sequences they are from
            # the last time we did this we need to issue untagged FETCH %d
            # (FLAG (..)) to all of our active clients. This does not suffer
            # the same restriction as EXISTS, RECENT, and EXPUNGE.
            #
            self._compute_and_publish_fetches(
                msgs, seq, dont_notify, publish_uids=publish_uids
            )

            # And see if the folder is getting kinda 'gappy' with spaces
            # between message keys. If it is, pack it.
            #
            self.sequences = seq
            self._pack_if_necessary(msgs)

        # And update the mtime before we leave..
        #
        self.mtime = asimap.mbox.Mailbox.get_actual_mtime(
            self.server.mailbox, self.name
        )
        # Update the attributes seeing if this folder has children or not.
        #
        self.check_set_haschildren_attr()

        self.commit_to_db()
        return

    ##################################################################
    #
    def _compute_and_publish_fetches(
        self, msgs, seqs, dont_notify=None, publish_uids=False
    ):
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
        for seq in list(seqs.keys()):
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
            for seq in list(seqs.keys()):
                if msg in seqs[seq]:
                    flags.append(seq_to_flag(seq))

            # Publish to every listening client except the one we are supposed
            # to ignore.
            #
            flags = " ".join(flags)
            msg_idx = msgs.index(msg) + 1
            clients = []
            for client in self.clients.values():
                if (
                    dont_notify
                    and client.client.port == dont_notify.client.port
                ):
                    continue
                clients.append(client)

            for client in clients:
                uidstr = ""
                if publish_uids:
                    try:
                        uidstr = " UID %d" % self.uids[msg_idx - 1]
                    except IndexError:
                        self.log.error(
                            "compute_and_publish: UID command but "
                            "message index: %d is not inside list "
                            "of UIDs, whose length is: %d"
                            % (msg_idx - 1, len(self.uids))
                        )
                client.client.push(
                    "* %d FETCH (FLAGS (%s)%s)\r\n" % (msg_idx, flags, uidstr)
                )
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
        self.log.debug("Packing folder")
        self.mailbox.pack()
        self.sequences = self.mailbox.get_sequences()
        return

    ##################################################################
    #
    async def send_expunges(self, uids):
        """
        This is called as part of resync()

        We are given the list of UID's that we know to be in the folder. This
        list may be different than the UID's we have from the last time we did
        a run through resync, stored in self.uids

        Our job is to see what uid's used to exist in this folder but no longer
        do. We then issue an EXPUNGE message for every UID that is missing,
        takin in to account its position in self.uids.

        Without this the IMAP client will get hopefully confused when the
        contents of the folder changes by having messages removed by some
        external force.

        Arguments:
        - `uids`: The list of uids, in order that are currently in the mailbox.
        """
        missing_uids = set(self.uids) - set(uids)

        # If none are missing then update the list of all uid's with what was
        # passed in and return. No EXPUNGE's need to be sent. At most we only
        # have new UID's added to our list.
        #
        if not missing_uids:
            self.uids = uids
            return

        logger.debug(
            "send_expunges: %d UID's missing. Sending EXPUNGEs.",
            len(missing_uids),
        )

        # Construct the set of clients we can send EXPUNGE's to immediately
        # and the set of clients we need to queue up EXPUNGE's for.
        #
        clients_to_notify = []
        clients_to_pend = []
        for c in self.clients.values():
            if c.idling:
                clients_to_notify.append(c)
            else:
                clients_to_pend.append(c)

        # Go through the UID's that are missing and send an expunge for each
        # one taking into account its position in the folder as we delete them.
        #
        for uid in missing_uids:
            which = self.uids.index(uid) + 1
            self.uids.remove(uid)
            exp = f"* {which} EXPUNGE\r\n"
            for c in clients_to_notify:
                await c.client.push(exp)
            for c in clients_to_pend:
                c.pending_expunges.append(exp)

        # and after we are done with that set our list of uid's to the list of
        # found uid's.
        #
        self.uids = uids

    ##################################################################
    #
    async def get_uid_from_msg(
        self, msg_key: int, cache: bool = True
    ) -> Tuple[Optional[int], Optional[int]]:
        """
        Get the uid from the given message (where msg_key is the integer
        key into the folder.)

        We return the tuple of (uid_vv,uid)

        If the message does NOT have a uid_vv or uid we return None for those
        elements in the tuple.

        Arguments:
        - `msg_key`: the message key in the folder we want the uid_vv/uid for.
        - `cache`: if True then also cache this message in the message cache.
        """
        try:
            msg = await self.get_and_cache_msg(msg_key, cache=cache)
        except KeyError:
            # Our caller should have locked the mailbox, but it may happen..
            #
            raise Bad("Unable to retrieve message. Deleted apparently.")

        if UID_HDR not in msg:
            return (None, None)

        try:
            uid_vv, uid = [int(x) for x in msg[UID_HDR].strip().split(".")]
            return (uid_vv, uid)
        except ValueError:
            logger.warning(
                "get_uid_from_msg: msg %s had malformed uid header: " "%s",
                msg_key,
                msg[UID_HDR],
            )
            return (None, None)

    ##################################################################
    #
    async def set_uid_in_msg(
        self, msg_key: int, new_uid: int, cache=False
    ) -> Tuple[int, int]:
        """
        Update the UID in the message with the new value.
        IF `cache` is True then also cache this message in the message cache.
        """
        # Get the mtime of the old message. We need to preserve this
        # because we use the mtime of the file as our IMAP 'internal-date'
        # value.
        #
        path = os.path.join(self.mailbox._path, str(msg_key))
        try:
            mtime = await aiofiles.os.path.getmtime(path)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise KeyError("No message with key: %s", msg_key)
            else:
                raise

        msg = await self.get_and_cache_msg(msg_key, cache=cache)
        del msg[UID_HDR]
        msg[UID_HDR] = f"{self.uid_vv:010d}.{new_uid:010d}"
        await self.mailbox.asetitem(msg_key, msg)

        # Sset its mtime to the mtime of the old file.
        #
        os.utime(path, (mtime, mtime))
        return (self.uid_vv, new_uid)

    ##################################################################
    #
    def _find_first_new_message(self, msgs, horizon=0):
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
            try:
                msg_path = mbox_msg_path(self.mailbox, msg)
                if int(os.path.getmtime(msg_path)) > horizon_mtime:
                    found = msg
                    break
            except OSError as e:
                if e.errno == errno.ENOENT:
                    self.log.error(
                        "find_first_new_msg: Message %d no longer "
                        "exists, errno: %s" % (msg, str(e))
                    )
                raise
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
        - `msgs`: the list of messages we are going to look through
                  (in reverse)
        """
        msgs = sorted(msgs, reverse=True)
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
    async def _update_msg_uids(self, msgs: List[int], seq):
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

        Arguments:
        - `msgs`: A list of the message keys that we need to check. NOTE: This
          will frequently be a subset of all messages in the folder.

        - `seq`: The existing sequences for this folder (may not be in sync
          with self.sequences for differencing purposes, and is passed in to
          save us from having to load them from disk again.
        """

        # We may need to re-write the .mh_sequences file if we need to tag
        # messages with 'Recent'. If we do that then we need a flag to let us
        # know the write the sequences back out to disk.
        #
        seq_changed = False

        # As we go through messages we need to know if the current UID we are
        # looking at is proper (ie: greater than the one of the previous
        # message.)
        #
        # If we hit one that is not then from that message on we need to
        # re-number all of their UID's.
        #
        redoing_rest_of_folder = False
        prev_uid = 0

        # We keep track of all of the UID's we find and any new ones we set.
        # This will allow us to compare with the UID's that were already in
        # our list of messages and lets us see if we need to issue any
        # 'EXPUNGE's for messages that have been removed.
        #
        uids_found = []

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
        # If we are not looking at too many messages (200?), then be sure to
        # try to cache them in the message cache.
        #
        num_msgs = len(msgs)
        cache = True if num_msgs < 200 else False
        for i, msg in enumerate(msgs):
            if i % 200 == 0:
                self.log.debug(
                    "check/update uids, at count %d, msg: %d out "
                    "of %d" % (i, msg, len(msgs))
                )

            if not redoing_rest_of_folder:
                # If the uid_vv is different or the uid is NOT
                # monotonically increasing from the previous uid then
                # we have to redo the rest of the folder.
                #
                uid_vv, uid = await self.get_uid_from_msg(msg, cache=cache)
                if (
                    uid_vv != self.uid_vv
                    or uid <= prev_uid
                    or uid_vv is None
                    or uid is None
                ):
                    redoing_rest_of_folder = True
                    self.log.debug(
                        "Found msg %d uid_vv/uid %s.%s out of "
                        "sequence. Redoing rest of folder." % (msg, uid_vv, uid)
                    )
                else:
                    uids_found.append(uid)
                    prev_uid = uid

            if redoing_rest_of_folder:
                # We are either replacing or adding a new UID header to this
                # message no matter what so do that.
                #
                uid_vv, uid = await self.set_uid_in_msg(
                    msg, self.next_uid, cache=cache
                )
                uids_found.append(self.next_uid)
                self.next_uid += 1

                # If the uid_vv we previously retrieved from the message is
                # different thant he uid_vv of this folder then this message is
                # new to this folder and needed to be added to the Recent
                # sequence.
                #
                if uid_vv != self.uid_vv:
                    # Make sure that seq has a 'Recent' sequence
                    #
                    if "Recent" not in seq:
                        seq["Recent"] = []

                    # IF the msg is not already in the Recent sequence add it.
                    #
                    if msg not in seq["Recent"]:
                        seq["Recent"].append(msg)
                        seq_changed = True

        # If we had to redo the folder then we believe it is indeed now
        # interesting so set the \Marked attribute on it.
        #
        if redoing_rest_of_folder:
            self.marked(True)

            # If seq_changed is True then we modified the sequencees too
            # so we need to re-write the sequences file.
            #
            if seq_changed is True:
                self.mailbox.set_sequences(seq)

        # And we are done.. we return the list of the uid's of all of the
        # messages we looked at or re-wrote (in order in which we encountered
        # them.)
        #
        return uids_found

    ##################################################################
    #
    async def _restore_from_db(self):
        """
        Restores this mailbox's persistent state from the database.  If this
        mailbox does not exist in the db we create an entry for it with
        defaults.

        We return True if we restored the data from the db.

        We return False if we had to create the record for this mailbox in the
        db.
        """
        results = await self.server.db.fetchone(
            "select id, uid_vv,attributes,mtime,next_uid,num_msgs,"
            "num_recent,uids,last_resync,subscribed from mailboxes "
            "where name=?",
            (self.name,),
        )

        # If we got back no results than this mailbox does not exist in the
        # database so we need to create it.
        #
        if results is None:
            # Create the entry in the db reflects what is on the disk as
            # far as we know.
            #
            self.check_set_haschildren_attr()
            self.mtime = await Mailbox.get_actual_mtime(
                self.server.mailbox, self.name
            )
            self.uid_vv = await self.server.get_next_uid_vv()
            await self.server.db.execute(
                "INSERT INTO mailboxes (id, name, uid_vv, attributes, "
                "mtime, next_uid, num_msgs, num_recent) "
                "VALUES (NULL,?,?,?,?,?,?,0)",
                (
                    self.name,
                    self.uid_vv,
                    ",".join(self.attributes),
                    self.mtime,
                    self.next_uid,
                    len(list(self.mailbox.keys())),
                ),
            )

            # After we insert the record we pull it out again because we need
            # the mailbox id to relate the mailbox to its sequences.
            #
            results = await self.server.db.fetchone(
                "SELECT id FROM mailboxes WHERE name=?", (self.name,)
            )
            self.id = results[0]

            # For every sequence we store it in the db also so we can later on
            # do smart diffs of sequence changes between mailbox resyncs.
            #
            self.sequences = await self.mailbox.aget_sequences()
            for name, values in self.sequences.items():
                await self.server.db.execute(
                    "INSERT INTO sequences (id,name,mailbox_id,sequence)"
                    " VALUES (NULL,?,?,?)",
                    (name, self.id, ",".join([str(x) for x in values])),
                )
            await self.server.db.commit()
            return False
        else:
            # We got back an actual result. Fill in the values in the mailbox.
            #
            (
                self.id,
                self.uid_vv,
                attributes,
                self.mtime,
                self.next_uid,
                self.num_msgs,
                self.num_recent,
                uids,
                self.last_resync,
                self.subscribed,
            ) = results
            self.attributes = set(attributes.split(","))
            if len(uids) == 0:
                self.uids = []
            else:
                self.uids = [int(x) for x in uids.split(",")]

            # And fill in the sequences we find for this mailbox.
            #
            async for row in self.server.db.query(
                "SELECT name, sequence FROM sequences WHERE mailbox_id=?",
                (self.id,),
            ):
                name, values = row
                self.sequences[name] = {int(x) for x in values.split(",")}
        return True

    ##################################################################
    #
    async def commit_to_db(self):
        """
        Write the state of the mailbox back to the database for persistent
        storage.
        """
        values = (
            self.uid_vv,
            ",".join(self.attributes),
            self.next_uid,
            self.mtime,
            self.num_msgs,
            self.num_recent,
            ",".join([str(x) for x in self.uids]),
            self.last_resync,
            self.subscribed,
            self.id,
        )
        await self.server.db.execute(
            "UPDATE mailboxes SET uid_vv=?, attributes=?, next_uid=?,"
            "mtime=?, num_msgs=?, num_recent=?, uids=?, last_resync=?, "
            "subscribed=? WHERE id=?",
            values,
        )

        # For the sequences we have to do a fetch before a store because we
        # need to delete the sequence entries from the db for sequences that
        # are no longer in this mailbox's list of sequences.
        #
        old_names = set()
        async for row in self.server.db.query(
            "SELECT name FROM sequences WHERE mailbox_id=?", (self.id,)
        ):
            old_names.add(row[0])

        new_names = set(self.sequences.keys())
        names_to_delete = old_names.difference(new_names)
        names_to_insert = new_names.difference(old_names)
        names_to_update = new_names.intersection(old_names)
        for name in names_to_delete:
            await self.server.db.execute(
                "DELETE FROM sequences WHERE mailbox_id=? AND name=?",
                (self.id, name),
            )
        for name in names_to_insert:
            await self.server.db.execute(
                "INSERT INTO sequences (id,name,mailbox_id,sequence) "
                "VALUES (NULL,?,?,?)",
                (
                    name,
                    self.id,
                    ",".join([str(x) for x in self.sequences[name]]),
                ),
            )
        for name in names_to_update:
            await self.server.db.execute(
                "UPDATE sequences SET sequence=? WHERE mailbox_id=? AND name=?",
                (
                    ",".join([str(x) for x in self.sequences[name]]),
                    self.id,
                    name,
                ),
            )
        await self.server.db.commit()

    ##################################################################
    #
    async def check_set_haschildren_attr(self):
        """
        In order to support RFC3348 we need to know if a given folder has
        children folders or not.

        I am being lazy here. Instead of just intelligentily and diligently
        checing and updating this flag and parent folders during folder
        instantiation, folder creation (adds flag to parent folders), and
        folder deletion I made this helper function. You call it on a folder
        instance and it will check to see if this folder has any children and
        update the attributes as appropriate.

        XXX The biggest downside is that we use MH.list_folders() and I
            have a feeling that this can be slow at times.
        """
        if len(await self.mailbox.alist_folders()) > 0:
            self.attributes.add(r"\HasChildren")
            if r"\HasNoChildren" in self.attributes:
                self.attributes.remove(r"\HasNoChildren")
        else:
            self.attributes.add(r"\HasNoChildren")
            if r"\HasChildren" in self.attributes:
                self.attributes.remove(r"\HasChildren")

    ##################################################################
    #
    async def get_and_cache_msg(
        self, msg_key: int, cache: bool = True
    ) -> MHMessage:
        """
        Get the message associated with the given message key in our mailbox.
        We check the cache first to see if it is there.
        If it is not we retrieve it from the MH folder and add it to the cache.

        Arguments:
        - `msg_key`: message key to look up the message by
        """
        msg = self.server.msg_cache.get(self.name, msg_key)
        if msg is None:
            msg = await self.mailbox.aget_message(msg_key)
            if cache:
                self.server.msg_cache.add(self.name, msg_key, msg)
        return msg

    ##################################################################
    #
    def selected(self, client):
        r"""
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

        if r"\Noselect" in self.attributes:
            raise No("You can not select the mailbox '%s'" % self.name)

        # A client has us selected. Turn of the expiry time.
        #
        self.expiry = None

        try:
            # self.mailbox.lock()
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
            mbox_keys = list(self.mailbox.keys())
            client.client.push("* %d EXISTS\r\n" % len(mbox_keys))
            if "Recent" in seq:
                client.client.push("* %d RECENT\r\n" % len(seq["Recent"]))
            else:
                client.client.push("* 0 RECENT\r\n")
            if "unseen" in seq:
                # Message id of the first message that is unseen.
                #
                first_unseen = sorted(seq["unseen"])[0]
                first_unseen = mbox_keys.index(first_unseen) + 1
                client.client.push("* OK [UNSEEN %d]\r\n" % first_unseen)
            client.client.push("* OK [UIDVALIDITY %d]\r\n" % self.uid_vv)
            client.client.push("* OK [UIDNEXT %d]\r\n" % self.next_uid)

            # Each sequence is a valid flag.. we send back to the client all
            # of the system flags and any other sequences that are defined on
            # this mailbox.
            #
            flags = list(SYSTEM_FLAGS)
            for k in list(seq.keys()):
                if k not in SYSTEM_FLAG_MAP:
                    flags.append(k)
            client.client.push("* FLAGS (%s)\r\n" % " ".join(flags))
            client.client.push(
                "* OK [PERMANENTFLAGS (%s)]\r\n" % " ".join(PERMANENT_FLAGS)
            )
        finally:
            # self.mailbox.unlock()
            pass

        return

    ##################################################################
    #
    def has_queued_commands(self, client=None):
        """
        Returns True if this client currently has commands in the command
        queue. Or if there are any queued commands if client is None

        Arguments:
        - `client`: The client we are checking for in the command queue.  If
                    client is None then we return True if there are any queued
                    commands.
        """
        if client:
            return any(
                x[0].client.port == client.client.port
                for x in self.command_queue
            )
        else:
            return len(self.command_queue) > 0

    ##################################################################
    #
    def unselected(self, client_name: str):
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
        if client_name not in self.clients:
            return

        del self.clients[client_name]

        if not self.clients:
            self.expiry = time.time() + MBOX_EXPIRY_TIME

    ##################################################################
    #
    def append(self, msg: MHMessage, flags=[], date_time=None):
        r"""
        Append the given message to this mailbox.
        Set the flags given. We also set the \Recent flag.
        If date_time is not given set it to 'now'.
        The internal date on the message is set to date_time.

        Arguments:
        - `message`: The email.message being appended to this mailbox
        - `flags`: A list of flags to set on this message
        - `date_time`: The internal date on this message
        """
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

            # if a date_time was supplied then set the mtime on the file to
            # that. We use mtime as our 'internal date' on messages.
            #
            if date_time is not None:
                c = time.mktime(date_time.timetuple())
                os.utime(mbox_msg_path(self.mailbox, key), (c, c))
        finally:
            self.mailbox.unlock()

        # We need to resync this mailbox so that we can get the UID of the
        # newly added message. This should be quick.
        #
        self.resync(optional=False)
        uid_vv, uid = self.get_uid_from_msg(key)
        self.log.debug(
            "append: message: %d, uid: %d, sequences: %s"
            % (key, uid, ", ".join(msg.get_sequences()))
        )
        return uid

    ##################################################################
    #
    def expunge(self, client=None):
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
            # self.mailbox.lock()
            # If there are no messages in the 'Deleted' sequence then we have
            # nothing to do.
            #
            seq = self.mailbox.get_sequences()
            if "Deleted" not in seq:
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
            clients_to_notify = {}
            clients_to_pend = []
            if client is not None:
                clients_to_notify[client.client.port] = client

            for port, c in self.clients.items():
                if c.idling:
                    clients_to_notify[port] = c
                elif port not in clients_to_notify:
                    clients_to_pend.append(c)

            # Now that we know who we are going to send expunges to immediately
            # and who we are going to record them for later sending, go through
            # the mailbox and delete the messages.
            #
            msgs = list(self.mailbox.keys())
            for msg in seq["Deleted"]:
                # Remove the message from the folder.. and also remove it from
                # our uids to message index mapping. (NOTE: 'which' is in IMAP
                # message sequence order, so its actual position in the array
                # is one less.
                #
                # Convert msg_key to IMAP seq num
                #
                which = msgs.index(msg) + 1
                msgs.remove(msg)

                # Remove UID from list of UID's in this folder. IMAP
                # sequence numbers start at 1.
                #
                self.uids.remove(self.uids[which - 1])
                self.mailbox.discard(msg)

                # Send EXPUNGE's to the clients we are allowed to notify
                # immediately.
                #
                for c in clients_to_notify.values():
                    c.client.push("* %d EXPUNGE\r\n" % which)

                # All other clients listening to this mailbox get the expunge
                # messages added to a list that will be checked before we
                # accept any other commands from them.
                #
                for c in clients_to_pend:
                    c.pending_expunges.append("* %d EXPUNGE\r\n" % which)

        finally:
            # self.mailbox.unlock()
            pass

        # Resync the mailbox, but send NO exists messages because the mailbox
        # has shrunk: 5.2.: "it is NOT permitted to send an EXISTS response
        # that would reduce the number of messages in the mailbox; only the
        # EXPUNGE response can do this.
        #
        # Unless a client is sitting in IDLE, then it is okay send them
        # exists/recents.
        #
        self.resync(notify=False)
        return

    ##################################################################
    #
    def search(self, search, cmd):
        """
        Take the given IMAP search object and apply it to all of the messages
        in the mailbox.

        Form a list (by message index) of the messages that match and return
        that list to our caller.

        Arguments:
        - `search`: An IMAPSearch object instance
        - `cmd`: The IMAP command. We need this in case this is a continuation
          command and this contains our continuation state, as well as whether
          or not this is a UID command.
        """
        # Before we do a search we do a resync to make sure that we have
        # attached uid's to all of our messages and various counts are up to
        # sync. But we do it with notify turned off because we can not send any
        # conflicting messages to this client (other clients that are idling do
        # get any updates though.)
        #
        self.resync(notify=False)
        if cmd.uid_command:
            self.log.debug("search(): Doing a UID SEARCH")

        results = []
        try:
            # self.mailbox.lock()
            # search_started = time.time()

            # We get the full list of keys instead of using an iterator because
            # we need the max id and max uuid.
            #
            msgs = list(self.mailbox.keys())

            if not msgs:
                return results

            seq_max = len(msgs)
            uid_vv, uid_max = self.get_uid_from_msg(msgs[-1])
            if uid_vv is None or uid_max is None:
                self.resync(notify=False)

            # If this is a continuation command than we skip over the number of
            # messages that we have alread processed.
            #
            if cmd.needs_continuation:
                # In the case of a search cmd.msg_idxs is an integer that
                # tells us how many messages to skip over.
                #
                msgs = msgs[cmd.msg_idxs :]

            # Go through the messages one by one and pass them to the search
            # object to see if they are or are not in the result set..
            #
            self.log.debug("Applying search to messages: %s" % str(search))

            for idx, msg in enumerate(msgs):
                # IMAP messages are numbered starting from 1.
                #
                i = idx + 1
                ctx = asimap.search.SearchContext(
                    self, msg, i, seq_max, uid_max, self.sequences
                )
                if search.match(ctx):
                    # The UID SEARCH command returns uid's of messages
                    #
                    if cmd.uid_command:
                        results.append(ctx.uid)
                    else:
                        results.append(i)

                # If after processing that message we have exceeded how much
                # time we may spend in a fetch we store how far we have gotten
                # and set the 'needs_continuation' flag and break out of the
                # loop. We will be invoked again and we will need to recognize
                # this and pick up where we left off.
                #
                # XXX Going to say... 1 second.
                #
                # now = time.time()
                # if now - search_started > 1:
                #     cmd.needs_continuation = True
                #     cmd.msg_idxs = i
                #     cmd.search_results.extend(results)
                #     self.log.debug("search: command took too long (%f), %d "
                #                    "processed out of %d. %d processed this "
                #                    "run. Marking as continuation and "
                #                    "returning." %
                #                    (now, len(cmd.search_results),
                #                     original_len, len(results)))
                #     return None
                # else:
                #     cmd.needs_continuation = False

        finally:
            # self.mailbox.unlock()
            pass

        # If our command has a non-empty list of search_results
        # then it was previously a continued command (and it is now
        # down..) Extend our results with what we have stashed in
        # the command.
        #
        if len(cmd.search_results) > 0:
            results.extend(cmd.search_results)

        return results

    #########################################################################
    #
    def fetch(self, msg_set, fetch_ops, cmd):
        """
        Go through the messages in the mailbox. For the messages that are
        within the indicated message set parse them and pull out the data
        indicated by 'fetch_ops'

        Return a list of tuples where the first element is the IMAP message
        sequence number and the second is the requested data.

        The requested data itself is a list of tuples. The first element is the
        name of the data item from 'fetch_ops' and the second is the
        requested data.

        Arguments:
        - `msg_set`: The set of messages we want to
        - `fetch_ops`: The things to fetch for the messags indiated in
          msg_set
        - `cmd`: The IMAP command. We need this in case this is a continuation
          command and this contains our continuation state, as well as whether
          or not this is a UID command.
        """
        start_time = time.time()
        seq_changed = False

        results = []
        try:
            # self.mailbox.lock()

            # We get the full list of keys instead of using an iterator because
            # we need the max id and max uuid.
            #
            msgs = list(self.mailbox.keys())

            # IF there are no messages in the mailbox there are no results.
            # XXX Should we return No in this case if the command is NOT a UID
            #     command?
            #
            if len(msgs) == 0:
                return ([], False)

            uid_vv, uid_max = self.get_uid_from_msg(msgs[-1])
            seq_max = len(self.mailbox)

            # Generate the set of indices in to our folder for this command
            #
            if cmd.needs_continuation:
                # If this command is a continuation then the list of indices
                # was already generated and we just need to know where in it we
                # were. Luckly this is stored cmd.msg_idxs.
                #
                # NOTE: We make a copy of the message sequence list because
                #       cmd.msg_idxs is modified as we process through
                #       them, removing indices that have been processed.
                #
                msg_idxs = cmd.msg_idxs[:]
            elif cmd.uid_command:
                # If we are doing a 'UID FETCH' command we need to use the max
                # uid for the sequence max.
                #
                uid_list = sequence_set_to_list(
                    msg_set, uid_max, cmd.uid_command
                )

                # We want to convert this list of UID's in to message indices
                # So for every uid we we got out of the msg_set we look up its
                # index in self.uids and from that construct the msg_idxs
                # list. Missing UID's are fine. They just do not get added to
                # the list.
                #
                msg_idxs = []
                for uid in uid_list:
                    if uid in self.uids:
                        mi = self.uids.index(uid) + 1
                        msg_idxs.append(mi)
                        # muid_vv, muid = self.get_uid_from_msg(msgs[mi-1])
                        # if muid != uid:
                        #     self.log.error("store: at index: %d, msg: %d, "
                        #                    "uid: %d, doees not match actual "
                        #                    "message uid: %d" % \
                        #                        (mi, msgs[mi-1],uid, muid))
                        #     self.resync(force = True, optional = False)

                # Also, if this is a UID FETCH then we MUST make sure UID is
                # one of the fields being fetched, and if it is not add it.
                #
                if not any([x.attribute == "uid" for x in fetch_ops]):
                    fetch_ops.insert(0, FetchAtt("uid"))

                # Store the set of mesage indices in the IMAP command in case
                # this becomes a continuation.
                #
                cmd.msg_idxs = msg_idxs[:]

            else:
                msg_idxs = sequence_set_to_list(msg_set, seq_max)

                # Store the set of mesage indices in the IMAP command in case
                # this becomes a continuation.
                #
                cmd.msg_idxs = msg_idxs[:]

            # Get a set of the fetch ops we are going to perform. This is
            # intended to let us optimize the fetch loop.
            #
            # fetch_ops = set([x.attribute for x in fetch_ops])

            # Go through each message and apply the fetch_ops.fetch() to
            # it building up a set of data to respond to the client with.
            #
            seq_changed = False
            fetch_started = time.time()
            for idx in msg_idxs:
                try:
                    msg_key = msgs[idx - 1]
                except IndexError:
                    log_msg = (
                        "fetch: Attempted to look up message index "
                        "%d, but msgs is only of length %d"
                        % (idx - 1, len(msgs))
                    )
                    self.log.warn(log_msg)
                    raise MailboxInconsistency(log_msg)

                ctx = asimap.search.SearchContext(
                    self, msg_key, idx, seq_max, uid_max, self.sequences
                )
                fetched_flags = False
                fetched_body = False
                iter_results = []

                for elt in fetch_ops:
                    iter_results.append(elt.fetch(ctx))
                    # If one of the FETCH ops gets the FLAGS we want to know
                    # and likewise if one of the FETCH ops gets the BODY (but
                    # NOT BODY.PEEK) we want to know. Both of these operations
                    # can potentially change the flags of the message.
                    #
                    if elt.attribute == "body" and elt.peek is False:
                        fetched_body = True
                    if elt.attribute == "flags":
                        fetched_flags = True

                results.append((idx, iter_results))

                # If we did a FETCH FLAGS and the message was in the 'Recent'
                # sequence then remove it from the 'Recent' sequence. Only one
                # client gets to actually see that a message is 'Recent.'
                #
                if (
                    fetched_flags
                    and "Recent" in self.sequences
                    and msg_key in self.sequences["Recent"]
                ):
                    self.sequences["Recent"].remove(msg_key)
                    seq_changed = True

                # If we dif a FETCH BODY (but NOT a BODY.PEEK) then the message
                # is removed from the 'unseen' sequence (if it was in it) and
                # added to the 'seen' sequence (if it was not in it.)
                #
                if fetched_body:
                    if (
                        "unseen" in self.sequences
                        and msg_key in self.sequences["unseen"]
                    ):
                        self.sequences["unseen"].remove(msg_key)
                        seq_changed = True
                    if "Seen" in self.sequences:
                        if msg_key not in self.sequences["Seen"]:
                            self.sequences["Seen"].append(msg_key)
                            seq_changed = True
                    else:
                        self.sequences["Seen"] = [msg_key]
                        seq_changed = True

                # Once we have processed a message, we pop one element off the
                # front of the imap command's 'message_sequence.' This way this
                # always remains up to date with respect to what message
                # indices are still left to be processed.
                #
                cmd.msg_idxs.pop(0)

                # If after processing that message we have exceeded how much
                # time we may spend in a fetch we store how far we have gotten
                # and set the 'needs_continuation' flag and break out of the
                # loop. We will be invoked again and we will need to recognize
                # this and pick up where we left off.
                #
                # XXX Going to say... 1.5 seconds.
                #
                now = time.time()
                if now - fetch_started > 1 and len(cmd.msg_idxs) > 0:
                    self.log.debug(
                        "fetch: command took too long (%f), %d "
                        "messages left to process. Marking as "
                        "continuation and returning."
                        % (now - fetch_started, len(cmd.msg_idxs))
                    )
                    cmd.needs_continuation = True
                    break
                else:
                    cmd.needs_continuation = False

            # Done applying FETCH to all of the indicated messages.
            # If the sequences changed we need to write them back out to disk.
            #
            if seq_changed:
                self.log.debug("FETCH: sequences were modified, saving")
                self.mailbox.set_sequences(self.sequences)
        finally:
            # self.mailbox.unlock()
            pass
        self.log.debug(
            "FETCH finished, duration: %f, num results: %d"
            % (time.time() - start_time, len(results))
        )
        return (results, seq_changed)

    ##################################################################
    #
    def store(self, msg_set, action, flags, cmd):
        r"""
        Update the flags (sequences) of the messages in msg_set.

        NOTE: No matter what flags are set/reset etc. \Recent is not affected.

        Arguments:
        - `msg_set`: The set of messages to modify the flags on
        - `action`: one of REMOVE_FLAGS, ADD_FLAGS, or REPLACE_FLAGS
        - `flags`: The flags to add/remove/replace
        - `cmd`: The IMAP command for this store. Used to determine if this is
          a uid command or not, and if this is a continuation or not (and if it
          is a continuation what the remaining message sequence is.)  we have
          to return not message sequence numbers but message UID's.
        """

        ####################################################################
        #
        def add_flags(flags, msgs, seqs, cmd):
            """
            Helpe function to add a flag to a sequence.

            Also handles removing a message from then Seen/unseen sequences if
            the flag being added is unseen/Seen.

            Arguments:
            - `flags`: The flags being added
            - `msgs`: The messages it is being added to
            - `seqs`: The dict of sequeneces
            - `cmd`: The IMAP command (for continuation purposes)
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
                    if (
                        flag == "Seen"
                        and "unseen" in seqs
                        and msg in seqs["unseen"]
                    ):
                        seqs["unseen"].remove(msg)

                    # Conversely, if we are adding a message to the unseen
                    # sequence be sure to remove it from the seen sequence.
                    #
                    if (
                        flag == "unseen"
                        and "Seen" in seqs
                        and msg in seqs["Seen"]
                    ):
                        seqs["Seen"].remove(msg)
            return

        ####################################################################
        #
        def remove_flags(flags, msgs, seqs, cmd):
            """
            Helper function to move a flag from a list of messages.

            Also handles adding a message from then Seen/unseen sequences if
            the flag being removed is unseen/Seen.

            Arguments:
            - `flags`: The flag being added
            - `msgs`: The messages it is being added to
            - `seqs`: The dict of sequeneces
            - `cmd`: The IMAP command (for continuation purposes)
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
        if r"\Recent" in flags:
            raise No(r"You can not add or remove the '\Recent' flag")
        try:
            # self.mailbox.lock()

            # Get the list of message keys that msg_set indicates.
            #
            msg_keys = list(self.mailbox.keys())
            seq_max = len(msg_keys)

            # Generate the set of indices in to our folder for this command
            #
            if cmd.needs_continuation:
                # If this command is a continuation then the list of indices
                # was already generated and we just need to know where in it we
                # were. Luckly this is stored cmd.msg_idxs.
                #
                # NOTE: We make a copy of the message sequence list because
                #       cmd.msg_idxs is modified as we process through
                #       them, removing indices that have been processed.
                #
                msg_idxs = cmd.msg_idxs[:]
            elif cmd.uid_command:
                # If we are doing a 'UID FETCH' command we need to use the max
                # uid for the sequence max.
                #
                uid_vv, uid_max = self.get_uid_from_msg(msg_keys[-1])
                uid_list = sequence_set_to_list(
                    msg_set, uid_max, cmd.uid_command
                )

                # We want to convert this list of UID's in to message indices
                # So for every uid we we got out of the msg_set we look up its
                # index in self.uids and from that construct the msg_idxs
                # list. Missing UID's are fine. They just do not get added to
                # the list.
                #
                msg_idxs = []
                for uid in uid_list:
                    if uid in self.uids:
                        mi = self.uids.index(uid) + 1
                        msg_idxs.append(mi)
                        # muid_vv, muid = self.get_uid_from_msg(msg_keys[mi-1])
                        # if muid != uid:
                        #     self.log.error("store: at index: %d, msg: %d,
                        #                    uid: %d, doees not match
                        #                    actual message uid: %d" %
                        #                    (mi, msg_keys[mi-1],uid, muid))
                        #     self.resync(force = True, optional = False)
                # Store the set of mesage indices in the IMAP command in case
                # this becomes a continuation.
                #
                cmd.msg_idxs = msg_idxs[:]
            else:
                msg_idxs = sequence_set_to_list(msg_set, seq_max)
                # Store the set of mesage indices in the IMAP command in case
                # this becomes a continuation.
                #
                cmd.msg_idxs = msg_idxs[:]

            # we are going to be primitive here for continuation commands. If
            # we are doing more than 100 messages we will only do 100 now and
            # make the rest a continuation.
            #
            if len(msg_idxs) > 100:
                cmd.needs_continuation = True
                msg_idxs = msg_idxs[:100]
                cmd.msg_idxs = cmd.msg_idxs[100:]
                self.log.debug(
                    "store: needs continuation. Process now: %d, "
                    "process later: %d" % (len(msg_idxs), len(cmd.msg_idxs))
                )
                if len(cmd.msg_idxs) == 0:
                    cmd.needs_continuation = False
            else:
                cmd.needs_continuation = False

            # Build a set of msg keys that are just the messages we want to
            # operate on.
            #
            msgs = [msg_keys[x - 1] for x in msg_idxs]

            # Convert the flags to MH sequence names..
            #
            flags = [flag_to_seq(x) for x in flags]
            seqs = self.mailbox.get_sequences()
            store_start = time.time()
            # Now for the type of operation involved do the necessasry
            #
            if action == ADD_FLAGS:
                # For every message add it to every sequence in the list.
                #
                add_flags(flags, msgs, seqs, cmd)
            elif action == REMOVE_FLAGS:
                # For every message remove it from every seq in flags.
                #
                remove_flags(flags, msgs, seqs, cmd)
            elif action == REPLACE_FLAGS:
                seqs_to_remove = set(seqs.keys()) - set(flags)
                seqs_to_remove.discard("Recent")

                # Add new flags to messages.
                #
                add_flags(flags, msgs, seqs, cmd)

                # Remove computed flags from messages...
                #
                remove_flags(seqs_to_remove, msgs, seqs, cmd)
            else:
                raise Bad("'%s' is an invalid STORE option" % action)

            # And when it is all done save our modified sequences back
            # to .mh_sequences
            #
            self.mailbox.set_sequences(seqs)
            self.log.debug(
                "store(): Completed, took %f seconds"
                % (time.time() - store_start)
            )
        finally:
            # self.mailbox.unlock()
            pass
        return

    ##################################################################
    #
    async def copy(
        self, msg_set: set, dest_mbox: "Mailbox", uid_command: bool = False
    ):
        r"""
        Copy the messages in msg_set to the destination mailbox.
        Flags (sequences), and internal date are preserved.
        Messages get the '\Recent' flag in the new mailbox.
        Arguments:
        - `msg_set`: Set of messages to copy.
        - `dest_mbox`: mailbox instance messages are being copied to
        - `uid_command`: True if this is for a UID SEARCH command, which means
          we have to return not message sequence numbers but message UID's.
        """

        async with self.lock.read_lock():
            # We get the full list of keys instead of using an iterator because
            # we need the max id and max uuid.
            #
            msgs = await self.mailbox.akeys()
            uid_vv, uid_max = self.get_uid_from_msg(msgs[-1], cache=True)
            if uid_vv is None or uid_max is None:
                self.resync()

            seq_max = len(msgs)

            if uid_command:
                # If we are doing a 'UID COPY' command we need to use the max
                # uid for the sequence max.
                #
                uid_list = sequence_set_to_list(msg_set, uid_max, uid_command)

                # We want to convert this list of UID's in to message indices
                # So for every uid we we got out of the msg_set we look up its
                # index in self.uids and from that construct the msg_idxs
                # list. Missing UID's are fine. They just do not get added to
                # the list.
                #
                msg_idxs = []
                for uid in uid_list:
                    if uid in self.uids:
                        mi = self.uids.index(uid) + 1
                        msg_idxs.append(mi)
            else:
                msg_idxs = sequence_set_to_list(msg_set, seq_max)

            src_uids = []
            dst_keys = []
            for idx in msg_idxs:
                key = msgs[idx - 1]  # NOTE: imap messages start from 1.

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
                mtime = os.path.getmtime(mbox_msg_path(self.mailbox, key))

                msg = self.get_and_cache_msg(key)
                uid_vv, uid = self.get_uid_from_msg(key)
                src_uids.append(uid)
                new_key = dest_mbox.mailbox.add(msg)
                dst_keys.append(new_key)

                # NOTE: We do NOT get and cache this new message. It has the
                #       uid_vv/uid from the source mailbox. It will need to
                #       re-written by resync() first.
                #
                # XXX Oh, we are getting the message and then using __setitem__
                #     so that we can add this message to the sequence `Recent`
                #     on the destination mailbox.
                #
                #     Since we want to avoid __setitem__ since it is not async
                #     we can instead do dest_mbox.mailbox.get_sequence(),
                #     update the sequence and then set_sequence().
                #
                # XXX This involves us getting a write lock on the destination
                #     mailbox. If a separate task is trying to copy a message
                #     to this mailbox we will hit a deadlock as both try to
                #     upgrade their read locks to write locks.  Maybe we should
                #     create a task that does the work to add this message to
                #     the other mailbox, and _outside of our mailbox read lock_
                #     we await that task.
                #
                #     we have been assuming that we would be getting the mbox
                #     readlock in the client command doing the copy. So we need
                #     to either pass the awaitable back to the caller or do the
                #     read lock in here. (I am leaning towards passing the
                #     awaitable back to the caller.)
                #
                #     Okay.. we split this command up in to the "get the
                #     messages to copy" and then "a task that writes those
                #     messages to the dest mailbox"
                #
                #     this command returns a task. That task will get the
                #     readlock and then write lock on the dest mailbox, write
                #     the messages to the new mailbox, add all the written
                #     messages to `Recent` sequence. then call resync on the
                #     destination mbox.
                #
                new_msg = dest_mbox.mailbox.get_message(new_key)
                new_msg.add_sequence("Recent")
                dest_mbox.mailbox[new_key] = new_msg

                os.utime(
                    mbox_msg_path(dest_mbox.mailbox, new_key), (mtime, mtime)
                )

                self.log.debug(
                    "copy: Copied message %d(%d) to %d" % (idx, key, new_key)
                )

        # Now we do a resync on the dest mailbox to assign all the new
        # messages UID's. Get all of the new UID's for the messages we
        # copied to the dest mailbox so we can return it for the
        # COPYUID response code.
        #
        # NOTE: If the destination mailbox can not be resync'd due to a mailbox
        #       lock do nothing. This will be handled in the normal resync
        #       process. We were being proactive so this is okay.
        #
        try:
            dest_mbox.resync(optional=False)
        except MailboxLock:
            pass

        dst_uids = []
        for k in dst_keys:
            uid_vv, uid = dest_mbox.get_uid_from_msg(k)
            dst_uids.append(uid)
        return src_uids, dst_uids

    ##################################################################
    #
    @classmethod
    async def get_actual_mtime(cls, mh: MH, name: str) -> int:
        """
        Get the max of the mtimes of the actual folder directory and its
        .mh_sequences file.

        If the .mh_sequences file does not exist create it.

        There were enough times we needed to check the mtime of the folder and
        the code is not a single line so I figured it was a place that started
        to needed to be DRY'.d

        Arguments:
        - `mh`: The top MH folder.
        - `name`: The name of the mbox we are checking
        """
        path = mbox_msg_path(mh, name)
        seq_path = path / ".mh_sequences"

        # Create the .mh_sequences file if it does not exist.
        #
        if not await aiofiles.os.path.exists(str(seq_path)):
            f = await aiofiles.open(str(seq_path), "w+")
            await f.close()

        path_mtime = await aiofiles.os.path.getmtime(str(path))
        seq_mtime = await aiofiles.os.path.getmtime(str(seq_path))

        return max(int(path_mtime), int(seq_mtime))

    #########################################################################
    #
    @classmethod
    async def create(cls, name: str, server: "IMAPUserServer"):
        """
        Creates a mailbox on disk that does not already exist and
        instantiates a Mailbox object for it.
        """
        # You can not create 'INBOX' nor, because of MH rules, create a mailbox
        # that is just the digits 0-9.
        #
        if name.lower() == "inbox":
            raise InvalidMailbox("Can not create a mailbox named 'inbox'")
        if DIGITS_RE.match(name):
            raise InvalidMailbox(
                "Due to MH restrictions you can not create a "
                "mailbox that is just digits: '%s'" % name
            )

        # If the mailbox already exists than it can not be created. One
        # exception is if the mailbox exists but with the "\Noselect"
        # flag.. this means that it was previously deleted and sitting in its
        # place is a phantom mailbox. In this case we remove the '\Noselect'
        # flag and return success.
        #
        try:
            mbox = await server.get_mailbox(name, expiry=0)
        except NoSuchMailbox:
            mbox = None

        # See if the mailbox exists but with the '\Noselect' attribute. This
        # will basically make the mailbox selectable again.
        #
        if mbox:
            if r"\Noselect" in mbox.attributes:
                mbox.attributes.remove(r"\Noselect")
                await mbox.check_set_haschildren_attr()
                await mbox.commit_to_db()
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

        mboxes_created = []
        mbox_chain.reverse()
        for m in mbox_chain:
            mbox = MH(m, create=True)
            mbox = await server.get_mailbox(m)
            mboxes_created.append(mbox)

        # And now go through all of those mboxes and update their children
        # attributes and make sure the underlying db is updated with this
        # information.
        #
        for m in mboxes_created:
            await m.check_set_haschildren_attr()
            await m.commit_to_db()

        return

    ####################################################################
    #
    @classmethod
    async def delete(cls, name: str, server: "IMAPUserServer"):
        r"""
        Delete the specified mailbox.

        Each of the non-permitted failure cases will raise MailboxException.

        You can not delete the mailbox named 'inbox'

        You can delete mailboxes that contain other mailboxes BUT what happens
        is that the mailbox is emptied of all messages and it then gets the
        '\Noselect' flag.

        You can NOT delete a mailbox that has the '\Noselect' flag AND
        contains sub-mailboxes.

        You can NOT delete a folder that is subscribed. It will get the
        '\Noselect' flag just like deleting a folder that has sub-folders.

        If the mailbox is selected by any client then what happens is the same
        as if the mailbox had an inferior mailbox: all the messages are empty
        and the mailbox gains the '\Noselect' flag.

        Arguments:
        - `name`: The name of the mailbox to delete
        - `server`: The user server object
        """
        log = logging.getLogger("%s.%s.delete()" % (__name__, cls.__name__))

        if name == "inbox":
            raise InvalidMailbox("You are not allowed to delete the inbox")

        mbox = server.get_mailbox(name)
        do_delete = False
        server.msg_cache.clear_mbox(name)
        async with (mbox.mailbox.lock_folder(), mbox.lock.read_lock()):
            inferior_mailboxes = await mbox.mailbox.alist_folders()

            # You can not delete a mailbox that has the '\Noselect' attribute
            # and has inferior mailboxes.
            #
            if r"\Noselect" in mbox.attributes and inferior_mailboxes:
                raise InvalidMailbox(
                    "The mailbox '%s' is already deleted" % name
                )
            # You can not delete a mailbox that has the '\Noselect' attribute
            # and is subscribed. (BTW: This means that this mailbox was already
            # deleted, but not removed because it still has subscribers.)
            #
            if r"\Noselect" in mbox.attributes and mbox.subscribed:
                raise InvalidMailbox(
                    "The mailbox '%s' is still subscribed" % name
                )

            async with mbox.lock.write_lock():
                # When deleting a mailbox every message in that mailbox will be
                # deleted.
                #
                await mbox.mailbox.aclear()

                # If the mailbox has any active clients we set their selected
                # mailbox to None. client.py will know if they try to do any
                # operations that require they have a mailbox selected that
                # this mailbox no longer exists and it will disconnect those
                # clients.
                #
                # A bit rude, but it is the simplest accepted action in this
                # case.
                #
                for client in mbox.clients.values():
                    client.mbox = None
                mbox.clients = {}

                # If the mailbox has inferior mailboxes then we do not actually
                # delete it. It gets the '\Noselect' flag though. It also gets
                # a new uid_vv so that if it is recreated before being fully
                # removed from the db no imap client will confuse it with the
                # existing mailbox.
                #
                if inferior_mailboxes or mbox.subscribed:
                    mbox.attributes.add(r"\Noselect")
                    mbox.uid_vv = server.get_next_uid_vv()
                    await mbox.commit_to_db()
                else:
                    # We have no inferior mailboxes. This mailbox is gone. If
                    # it is active we remove it from the list of active
                    # mailboxes and if it has any clients that have it selected
                    # they are moved back to the unauthenticated state.
                    #
                    async with server.active_mailboxes_lock.read_lock():
                        if name in server.active_mailboxes:
                            async with server.active_mailboxes_lock.write_lock():
                                del server.active_mailboxes[name]

                    # Delete all traces of the mailbox from our db.
                    #
                    await server.db.execute(
                        "DELETE FROM mailboxes WHERE id = ?", (mbox.id,)
                    )
                    await server.db.execute(
                        "DELETE FROM sequences WHERE mailbox_id = ?", (mbox.id,)
                    )
                    await server.db.commit()

                    # We need to delay the 'delete' of the actual mailbox until
                    # after we release the lock but we only delete the actual
                    # mailbox outside of the lock context manager if we are
                    # actually deleting it.
                    #
                    do_delete = True

        # if this mailbox was the child of another mailbox than we may need to
        # update that mailbox's 'has children' attributes.
        #
        parent_name = os.path.dirname(name)
        if parent_name != "":
            parent_mbox = server.get_mailbox(parent_name, expiry=0)
            async with (
                parent_mbox.lock.read_lock(),
                parent_mbox.lock.write_lock(),
            ):
                parent_mbox.check_set_haschildren_attr()
                parent_mbox.commit_to_db()

        # And remove the mailbox from the filesystem.
        #
        if do_delete:
            try:
                await server.mailbox.aremove_folder(name)
            except NotEmptyError as e:
                log.warn("mailbox %s 'not empty', %s" % (name, str(e)))
                path = mbox_msg_path(server.mailbox, name)
                log.info("using shutil to delete '%s'" % path)
                shutil.rmtree(path)

    ####################################################################
    #
    @classmethod
    def rename(cls, old_name: str, new_name: str, server: "IMAPUserServer"):
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
        mbox = await server.get_mailbox(old_name)

        # The mailbox we are moving to must not exist.
        #
        try:
            server.mailbox.get_folder(new_name)
        except NoSuchMailboxError:
            pass
        else:
            raise MailboxExists("Destination mailbox '%s' exists" % new_name)

        # Inbox is handled specially.
        #
        if mbox.name.lower() != "inbox":
            await _helper_rename_folder(mbox, new_name)
        else:
            await _helper_rename_inbox(mbox, new_name)

    ####################################################################
    #
    @classmethod
    async def list(
        cls: "Mailbox",
        ref_mbox_name: str,
        mbox_match: str,
        server: "IMAPUserServer",
        lsub: bool = False,
    ) -> List[Tuple[str, Set[str]]]:
        """
        This returns a list of tuples of mailbox names and those mailbox's
        attributes. The list is generated from the mailboxes db shelf.

        The `ref_mbox_name` is a string prefix for mailbox names to match.

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
        - `lsub`: If True this will only match folders that have their
          subscribed bit set.
        """
        # The mbox_match character can not begin with '/' because our mailboxes
        # are unrooted.
        #
        if len(mbox_match) > 0 and mbox_match[0] == "/":
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
        mbox_match = mbox_match.replace(r"\*", r".*").replace(r"\%", r"[^\/]*")
        results = []

        # NOTE: We do not present to the IMAP client any folders that
        #       have the flag 'ignored' set on them.
        #
        subscribed = "AND subscribed=1" if lsub else ""
        query = (
            "SELECT name,attributes FROM mailboxes WHERE name "
            f"regexp ? {subscribed} AND attributes NOT LIKE '%ignored%' "
            "ORDER BY name"
        )

        async for mbox_name, attributes in server.db.query(
            query, (mbox_match,)
        ):
            attributes = set(attributes.split(","))
            if mbox_name.lower() == "inbox":
                mbox_name = "INBOX"
            results.append((mbox_name, attributes))
        return results


####################################################################
#
async def _helper_rename_folder(mbox: Mailbox, new_name: str):
    """
    Breaking the logic for renaming a folder that is NOT the `inbox` out
    from the class method.

    Nothing about the logic needs to be in the class (and we can break out unit
    tests out a little bit as well.)

    This function will rename a mailbox, and rename all of its children
    mailboxes.

    The checks to see if this mailbox can be renamed have already been done.
    """
    # NOTE: Make it easier to refer to the server down below
    #
    srvr = mbox.server

    # Even more helpers to make code in our locks simpler.
    #
    def _do_rename_folder(old_mbox: Mailbox, old_id: int, mbox_new_name: str):
        """
        Helper routine for the helper routine that makes the changes to the
        db and active mailboxes once we have the mbox write lock.
        """
        mbox_old_name = old_mbox.name
        async with (
            srvr.active_mailboxes_lock.read_lock(),
            srvr.active_mailboxes_lock.write_lock(),
        ):
            await srvr.db.execute(
                "UPDATE mailboxes SET name=? WHERE id=?",
                (new_name, old_id),
            )

            mb = srvr.active_mailboxes[mbox_old_name]
            del srvr.active_mailboxes[mbox_old_name]
            mb.name = mbox_new_name
            mb.mailbox = srvr.mailbox.get_folder(mbox_new_name)
            srvr.active_mailboxes[mbox_new_name] = mb
        srvr.msg_cache.clear_mbox(mbox_old_name)

    # Make a hard link to where the new mbox is going to be. This way as we
    # move any subordinate folders if they get activity before we have finished
    # the entire move they will not just fail. When done all we need to do is
    # remove the old mbox dir.
    #
    old_name = mbox.name
    old_dir = mbox_msg_path(srvr.mailbox, old_name)
    new_dir = mbox_msg_path(srvr.mailbox, new_name)

    async with mbox.lock.read_lock():
        await aiofiles.os.link(old_dir, new_dir)

        # Get all the mailboxes we have to rename (this mbox may have children)
        #
        to_change = {}
        async for mbox_old_name, mbox_id in srvr.db.query(
            "SELECT name,id FROM mailboxes WHERE name=? " "OR name LIKE ?",
            (old_name, f"{old_name}/%"),
        ):
            mbox_new_name = new_name + mbox_old_name[len(old_name) :]
            to_change[mbox_old_name] = (mbox_new_name, mbox_id)

        for old, new_data in to_change.items():
            # If this is the mbox we were passed in, we already have a read
            # lock so we do not need to acquire it.
            #
            old_mbox = await srvr.get_mailbox(old, expiry=10)
            if old_mbox.name == mbox.name:
                async with old_mbox.lock.write_lock():
                    _do_rename_folder()
            else:
                async with (
                    old_mbox.lock.read_lock(),
                    old_mbox.lock.write_lock(),
                ):
                    _do_rename_folder()

        srvr.db.commit()

        # And now we can remove the link to the old directory.
        #
        await aiofiles.os.rmdir(old_dir)

    # If this mailbox we just renamed had a parent, that parent mailbox might
    # no longer have any children after this rename, so we have to update its
    # children flags.
    #
    old_p_name = os.path.dirname(old_name)
    if old_p_name != "":
        m = await srvr.get_mailbox(old_p_name, expiry=10)
        async with (m.lock.read_lock(), m.lock.write_lock()):
            await m.check_set_haschildren_attr()
            await m.commit_to_db()

    # See if the mailbox under its new name has a parent and if it does update
    # that parent's children flags.
    #
    new_p_name = os.path.dirname(new_name)
    if new_p_name != "":
        m = srvr.get_mailbox(new_p_name, expiry=0)
        async with (m.lock.read_lock(), m.lock.write_lock()):
            await m.check_set_haschildren_attr()
            await m.commit_to_db()


####################################################################
#
async def _helper_rename_inbox(mbox: Mailbox, new_name: str):
    """
    When the `inbox` gets renamed what happens is all messages in the inbox
    get moved to the new mailbox (and removed from the inbox.)

    Sub-folders of the inbox are not affected.
    """
    # when you rename 'inbox' what happens is you create a new mailbox
    # with the new name and all messages in 'inbox' are moved to this
    # new mailbox. Inferior mailboxes of 'inbox' are unchanged and not
    # copied.
    #
    server = mbox.server
    await Mailbox.create(new_name, server)

    # We will get all the keys, fetch them one by one. If they are not there
    # when we try to fetch them because something else deleted them.. do not
    # care. Just move on.
    #
    # NOTE: Message flags are preserved.
    #
    new_mbox = await server.get_mailbox(new_name)

    # NOTE: We are nesting locks of different mailboxes. Normally this would be
    #       a bad thing to do but it should be okay here because the dest
    #       mailbox is newly created and likely has nothing else poking it.
    #
    async with mbox.mailbox.lock_folder():
        async with (mbox.lock.read_lock(), mbox.lock.write_lock):
            async with (new_mbox.lock.read_lock(), new_mbox.lock.write_lock):
                for key in await mbox.mailbox.akeys():
                    try:
                        msg = await mbox.mailbox.aget_message(key)
                    except KeyError:
                        continue

                    # Replace the asimap uid since this is a new folder.
                    #
                    uid = f"{new_mbox.uid_vv:010d}.{new_mbox.new_uid:010d}"
                    new_mbox.next_uid += 1
                    del msg["X-asimapd-uid"]
                    msg["X-asimapd-uid"] = uid
                    await new_mbox.aadd(msg)
                    try:
                        await mbox.aremove(key)
                    except KeyError:
                        pass
            new_mbox.sequences = await new_mbox.mailbox.aget_sequences()
            await new_mbox.commit_to_db()

    async with mbox.lock.read_lock():
        await mbox.resync()

    async with new_mbox.lock.read_lock():
        await new_mbox.resync()
