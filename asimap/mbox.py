"""
The module that deals with the mailbox objects.

There will be a mailbox per MH folder (but not one for the top level
that holds all the folders.)
"""
# system imports
#
import asyncio
import errno
import logging
import os.path
import re
import shutil
import stat
import time
from collections import defaultdict
from datetime import datetime
from mailbox import FormatError, MHMessage, NoSuchMailboxError, NotEmptyError
from pathlib import Path
from statistics import fmean, median, stdev
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union

# 3rd party imports
#
import aiofiles

# Project imports
#
from .constants import (
    PERMANENT_FLAGS,
    REVERSE_SYSTEM_FLAG_MAP,
    SYSTEM_FLAG_MAP,
    SYSTEM_FLAGS,
    flag_to_seq,
    seq_to_flag,
)
from .exceptions import Bad, MailboxInconsistency, No
from .fetch import FetchAtt, FetchOp
from .message_cache import MessageCache
from .mh import MH, Sequences, update_message_sequences
from .parse import StoreAction
from .search import IMAPSearch, SearchContext
from .utils import (
    UID_HDR,
    MsgSet,
    UpgradeableReadWriteLock,
    get_uidvv_uid,
    sequence_set_to_list,
    utime,
    with_timeout,
)

# Allow circular imports for annotations
#
if TYPE_CHECKING:
    from .client import Authenticated
    from .user_server import IMAPUserServer

logger = logging.getLogger("asimap.mbox")

# How many seconds after a mailbox instance has no clients before we
# expire it from the list of active mailboxes
#
MBOX_EXPIRY_TIME = 900


####################################################################
#
def mbox_msg_path(mbox: MH, x: Optional[Union[int, str]] = None) -> Path:
    """
    Helper function for the common operation of getting the path to a
    file inside a mbox.

    Keyword Arguments:
    mbox -- the mailbox object we are getting a path into
    x -- the thing inside the mailbox we are referencing.. typically an
         integer representing a message by number inside the mailbox.
         default: '' (ie: nothing.. just return the path to the mailbox.)
    """
    msg_key = "" if x is None else str(x)
    return Path(mbox._path) / msg_key


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

    # Only pack when the folder is over this size
    #
    FOLDER_SIZE_PACK_LIMIT = 100

    # Only pack when the folder's message key span is under this ratio
    #
    FOLDER_RATIO_PACK_LIMIT = 0.8

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
        self.folder_size_pack_limit = self.FOLDER_SIZE_PACK_LIMIT
        self.folder_ratio_pack_limit = self.FOLDER_RATIO_PACK_LIMIT

        # List of the UID's of the messages in this mailbox. They are in IMAP
        # message sequence order (ie: first message in the mailbox, its uid is
        # in self.uids[0]) (note when converting from IMAP message sequence
        # numbers, you have to subtract one since they are 1-ordered, not
        # 0-ordered.)
        #
        self.uids: List[int] = []
        self.subscribed = False
        self.lock = UpgradeableReadWriteLock()

        # Time in seconds since the unix epoch when a resync was last tried.
        #
        self.last_resync = 0

        # XXX I think I need to do away with this distinction. We can still
        #     store sequences in the db, but the mbox code will not rely on
        #     that. Every time we load the mbox we load the sequences from the
        #     .mh_sequences file. every time we get get the file lock we
        #     re-read the sequences. If we write it, we must write inside a
        #     file lock.
        #
        # NOTE: It is important to note that self.sequences is the value of the
        # sequences we stored in the db from the end of the last resync. As
        # such they are useful for measuring what has changed in various
        # sequences between resync() runs. NOT as a definitive set of what the
        # current sequences in the mailbox are.
        #
        # These are basically updated at the end of each resync() cycle.
        #
        self.sequences: Sequences = defaultdict(list)

        # You can not instantiate a mailbox that does not exist in the
        # underlying file system.
        #
        try:
            self.mailbox = server.mailbox.get_folder(name)
        except NoSuchMailboxError:
            raise NoSuchMailbox(f"No such mailbox: '{name}'")

        # The list of attributes on this mailbox (this is things such as
        # '\Noselect'
        #
        self.attributes: set[str] = {r"\Unmarked"}

        # When a mailbox is no longer selected by _any_ client, then after a
        # period of time we remove this mailbox from the list of active
        # mailboxes held by the server.
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
        self.expiry: Optional[float] = time.time() + expiry

        # The dict of clients that currently have this mailbox selected.
        # This includes clients that used 'EXAMINE' instead of 'SELECT'
        #
        # Key is the local port this client's IMAPClientProxy is connected to.
        # (maybe it should be the client's name?)
        #
        self.clients: Dict[str, "Authenticated"] = {}

    ####################################################################
    #
    @classmethod
    async def new(cls, *args, **kwargs) -> "Mailbox":
        """
        We can not have __init__() be an async function, yet we need to do
        some async operations when we instantiate a mailbox. This code is for
        doing both. So this is the entry point to instantiate a mailbox.
        """
        mbox = cls(*args, **kwargs)

        # If the .mh_sequence file does not exist create it.
        #
        mh_seq_fname = mbox_msg_path(mbox.mailbox, ".mh_sequences")
        if not await aiofiles.os.path.exists(mh_seq_fname):
            f = await aiofiles.open(mh_seq_fname, "a")
            await f.close()
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
            await mbox.resync(force=force_resync, optional=False)
        return mbox

    ####################################################################
    #
    def __str__(self):
        return f"<Mailbox: {self.name}, num clients: {len(self.clients)}, num msgs: {self.num_msgs}>"

    ##################################################################
    #
    def marked(self, mark: bool):
        r"""
        A helper function that toggles the '\Marked' or '\Unmarked' flags on a
        folder (another one of those annoying things in the RFC you really only
        need one of these flags.)

        Arguments:
        - `bool`: if True the \Marked attribute is added to the folder. If
                  False the \Unmarked attribute is added to the folder.
        """
        if mark:
            self.attributes.add(r"\Marked")
            if r"\Unmarked" in self.attributes:
                self.attributes.remove(r"\Unmarked")
        else:
            self.attributes.add(r"\Unmarked")
            if r"\Marked" in self.attributes:
                self.attributes.remove(r"\Marked")
        return

    ####################################################################
    #
    async def _get_sequences_update_seen(
        self, msg_keys: List[int]
    ) -> Sequences:
        """
        Get the sequences from the MH folder.

        Update the `Seen` and `unseen` sequences. Basically `unseen` is used
        when messages are added to a folder. However, the IMAP protocol is all
        about `Seen` flags. Thus we need to make sure we properly update the
        `Seen` and `unseen` sequences so that they remain in sync. ie: all
        mesages NOT marked `unseen` are `Seen`.

        Returns the sequences for this folder.

        Presumes that the the folder lock has been acquired.

        Raises a MailboxInconsistency exception if we are unable to read the
        .mh_sequences file.

        XXX we may be updating the sequences.. but any messages in the message
            cache will not have their sequence information updated. B-/ we
            could empty the cache but that would be annoying. Maybe we should
            loop through all the messages in the cache and update their
            sequence information.
        """
        assert self.lock.this_task_has_write_lock()  # XXX Mostly for debugging.
        try:
            seq = await self.mailbox.aget_sequences()
        except FormatError as exc:
            logger.exception(
                "Bad `.mh_sequences` for mailbox %s: %s",
                self.mailbox._path,
                exc,
            )
            raise MailboxInconsistency(str(exc))

        modified = False
        if seq["unseen"]:
            # Create the 'Seen' sequence by the difference between all
            # the messages in the mailbox and the unseen ones.
            #
            new_seen = list(set(msg_keys) - set(seq["unseen"]))
            if new_seen != seq["Seen"]:
                seq["Seen"] = new_seen
                modified = True
        else:
            # There are no unseen messages in the mailbox thus the Seen
            # sequence mirrors the set of all messages.
            #
            if seq["Seen"] != msg_keys:
                modified = True
                seq["Seen"] = msg_keys

        # A mailbox gets '\Marked' if it has any unseen messages or
        # '\Recent' messages.
        #
        if "unseen" in seq or "Recent" in seq:
            self.marked(True)
        else:
            self.marked(False)

        if modified:
            await self.mailbox.aset_sequences(seq)
            # Make sure any sequence information on messages in the message
            # cache is updated.
            #
            _help_update_msg_sequences_in_cache(
                self.server.msg_cache, self.name, msg_keys, seq
            )
        return seq

    ####################################################################
    #
    async def _dispatch_or_pend_notifications(
        self,
        notifications: Union[str, List[str]],
        dont_notify: Optional["Authenticated"] = None,
    ):
        """
        A helper function for sending out notifications to clients.

        At various times mailbox state updates and the server is permitted to
        send messages to the clients that have that mailbox selected or are
        listening to it.

        If they are listening to it we can send the message immediately to that
        client.

        If they are not listening, then we need to set the message so that when
        the next command that can have untagged responses is running we can
        send all these notifications.

        NOTE: We are not allowed to send the untagged and unrequested
              notifications to a client when a FETCH, STORE, or SEARCH command
              is in progress for that client.

              *UNLESS!* that command is a UID command. In that case we are
              allowed to send untagged messages to the client, but they need to
              be UID messages.

        NOTE: When this is called as part of a `resync()` being done as part of
        a `SELECT` command, the code processing the `SELECT` comamnd will
        generate and send the necessary notifications, so we should NOT include
        that client, indicated by the `dont_notify` parameter, in the clients
        we are sending notifcations to.

        in any case we were doing this logic in several places this collects it
        in one place.
        """
        if not notifications:
            return
        if isinstance(notifications, str):
            notifications = [notifications]

        for c in self.clients.values():
            # Skip over the client we are not going to send notifications to.
            #
            if c == dont_notify:
                continue
            if c.idling:
                await c.client.push(*notifications)
            else:
                c.pending_notifications.extend(notifications)

    ##################################################################
    #
    async def resync(
        self,
        force: bool = False,
        notify: bool = True,
        only_notify: Optional["Authenticated"] = None,
        dont_notify: Optional["Authenticated"] = None,
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
        start_time = time.time()
        self.last_resync = int(start_time)

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

        # If `optional` is set (the default) and the mtime is the same as what
        # is on disk then we can totally skip this resync run.
        #
        if not force and optional and start_mtime <= self.mtime:
            return
        logger.debug(
            "mailbox: %s, force: %s, optional: %s, start mtime: %d, self.mtime: %d",
            self.name,
            force,
            optional,
            start_mtime,
            self.mtime,
        )

        async with (self.mailbox.lock_folder(), self.lock.write_lock()):
            # Whenever we resync the mailbox we update the sequence for
            # 'seen' based on 'seen' are all the messages that are NOT in
            # the 'unseen' sequence.
            #
            msg_keys = await self.mailbox.akeys()

            # NOTE: This returns the current sequence info from .mh_sequces
            #       (in addition to updating seen from unseen)
            #
            seq = await self._get_sequences_update_seen(msg_keys)

            # If the list of uids is empty but the list of messages is not then
            # force a full resync of the mailbox.. likely this is just an
            # initial data problem for when a `Mailbox` instance is first
            # instantiated and does not require rewriting every message (but
            # requires reading every message)
            #
            if not self.uids and msg_keys:
                logger.debug(
                    "mailbox: %s, len uids: %d, len msgs: %d, forcing resync",
                    self.name,
                    len(self.uids),
                    len(msg_keys),
                )
                force = True

            # If the folder is NOT empty scan for messages that have been added
            # to it by a third party and do not have UID's or the case where
            # the folder's contents have been re-bobbled (eg: `sortm`) and the
            # UID's are no longer in strictly ascending order.
            #
            found_uids = self.uids
            start_idx = 0
            if msg_keys:
                # NOTE: We handle a special case where the db was reset.. if
                #       the last message in the folder has a uid greater than
                #       what is stored in the folder then set that uid +1 to be
                #       the next_uid, and force a resync of the folder.
                #
                uid_vv, uid = await self.get_uid_from_msg(
                    msg_keys[-1], cache=False
                )
                if (
                    uid is not None
                    and uid_vv is not None
                    and uid_vv == self.uid_vv
                    and uid >= self.next_uid
                ):
                    logger.warning(
                        "Mailbox %s: last message uid: %d, next_uid: %d - "
                        "mismatch forcing full resync",
                        self.name,
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
                if len(msg_keys) < len(self.uids):
                    logger.warning(
                        "Mailbox %s: number of messages in folder (%d) "
                        "is less than list of cached uids: %d. "
                        "Forcing resync.",
                        self.name,
                        len(msg_keys),
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
                        "Mailbox %s: forced rescanning all %d messages",
                        self.name,
                        len(msg_keys),
                    )
                    self.server.msg_cache.clear_mbox(self.name)
                    found_uids = await self._update_msg_uids(msg_keys, seq)

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
                    # async with asyncio.TaskGroup() as tg:
                    #     first_new_task = tg.create_task(
                    #         self._find_first_new_message(msg_keys, horizon=30)
                    #     )
                    #     first_wo_uid_task = tg.create_task(
                    #         self._find_msg_without_uidvv(msg_keys)
                    #     )
                    # first_new_msg = first_new_task.result()
                    # first_msg_wo_uid = first_wo_uid_task.result()
                    first_new_msg = await self._find_first_new_message(
                        msg_keys, horizon=30
                    )
                    first_msg_wo_uid = await self._find_msg_without_uidvv(
                        msg_keys
                    )

                    # If either of these is NOT None then we have some subset
                    # of messages we need to scan. If both of these ARE None
                    # then we have determined that there are no new messages to
                    # deal with in the mailbox.
                    #
                    if first_new_msg or first_msg_wo_uid:
                        logger.debug(
                            "first new message: %s, first msg wo uid: %s",
                            first_new_msg,
                            first_msg_wo_uid,
                        )

                        # Start at the lower of these two message keys.
                        # 'start' is they MH message key. 'start_idx' index in
                        # to the list of message keys for 'start'
                        #
                        start = min(
                            x
                            for x in [first_new_msg, first_msg_wo_uid]
                            if x is not None
                        )
                        start_idx = msg_keys.index(start)
                        logger.debug(
                            "Mailbox %s: rescanning from %d to %d",
                            self.name,
                            start,
                            msg_keys[-1],
                        )

                        # Now make 'found_uids' be all the assumed known uid's
                        # _before_ start_index, and all the now newly
                        # discovered uid's at start_index to the end of the
                        # list of messages.
                        #
                        found_uids = await self._update_msg_uids(
                            msg_keys[start_idx:], seq
                        )
                        found_uids = self.uids[:start_idx] + found_uids

                        # Calculate what UID's were deleted and what order they
                        # were deleted in and send expunges as necessary to all
                        # connected clients.
                        #
                        await self.send_expunges(found_uids)
            else:
                # number of messages in the mailbox is zero.. make sure our
                # list of uid's for this mailbox is also empty.
                #
                self.server.msg_cache.clear_mbox(self.name)
                if self.uids:
                    logger.info(
                        "Mailbox %s: List of msgs is empty, but "
                        "list of uid's was not. Emptying.",
                        self.name,
                    )

                    # Calculate what UID's were deleted and what order they
                    # were deleted in and send expunges as necessary to all
                    # connected clients.
                    #
                    await self.send_expunges([])

            # Before we finish if the number of messages in the folder or the
            # number of messages in the Recent sequence is different than the
            # last time we did a resync then this folder is interesting
            # (\Marked) and we need to tell all clients listening to this
            # folder about its new sizes.
            #
            num_recent = len(seq["Recent"])
            num_msgs = len(msg_keys)

            # NOTE: Only send EXISTS messages if notify is True and the client
            # is not idling and the client is not the one passed in via
            # 'only_notify'
            #
            if num_msgs != self.num_msgs or num_recent != self.num_recent:
                notifications = []
                if num_msgs != self.num_msgs:
                    notifications.append(f"* {num_msgs} EXISTS\r\n")
                if num_recent != self.num_recent:
                    notifications.append(f"* {num_recent} RECENT\r\n")
                await self._dispatch_or_pend_notifications(
                    notifications, dont_notify=dont_notify
                )

            # Make sure to update our mailbox object with the new counts.
            #
            self.num_msgs = num_msgs
            self.num_recent = num_recent

            # Now if any messages have changed which sequences they are from
            # the last time we did this we need to issue untagged FETCH %d
            # (FLAG (..)) to all of our active clients. This does not suffer
            # the same restriction as EXISTS, RECENT, and EXPUNGE.
            #
            await self._compute_and_publish_fetches(
                msg_keys, seq, dont_notify, publish_uids=publish_uids
            )
            self.sequences = seq

            # And see if the folder is getting kinda 'gappy' with spaces
            # between message keys. If it is, pack it.
            #
            await self._pack_if_necessary(msg_keys)

        # And update the mtime before we leave..
        #
        self.mtime = await Mailbox.get_actual_mtime(
            self.server.mailbox, self.name
        )
        # Update the attributes seeing if this folder has children or not.
        #
        await self.check_set_haschildren_attr()
        await self.commit_to_db()
        end_time = time.time()
        logger.debug(
            "non-trivial resync finished. Duration: %f, num messages: %d, num recent: %d",
            (end_time - start_time),
            self.num_msgs,
            self.num_recent,
        )

    ##################################################################
    #
    async def _compute_and_publish_fetches(
        self, msg_keys, seqs, dont_notify=None, publish_uids=False
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
        - `msg_keys`: A list of all of the message keys in this folder
        - `seqs`: The latest representation of what the on disk sequences are
        - `dont_notify`: The client to NOT send "FETCH" notices to
        - `publish_uids`: If this is true then ALSO include the messages UID in
          the FETCH response
        """
        # We build up the set of messages that have changed flags
        #
        changed_msg_keys = set()
        # If any sequence exists now that did not exist before, or does not
        # exist now but did exist before then all of those messages in those
        # sequences have changed flags.
        #
        for seq in set(seqs.keys()) ^ set(self.sequences.keys()):
            if seqs[seq]:
                changed_msg_keys |= set(seqs[seq])
            if self.sequences[seq]:
                changed_msg_keys |= set(self.sequences[seq])

        # Now that we have handled the messages that were in sequences that do
        # not exist in one of seqs or self.sequences go through the sequences
        # in seqs. For every sequence if it is in self.sequences find out what
        # messages have either been added or removed from these sequences and
        # add it to the set of changed messages.
        #
        for seq in list(seqs.keys()):
            if seq not in self.sequences:
                continue
            changed_msg_keys |= set(seqs[seq]) ^ set(self.sequences[seq])

        # Now eliminate all entries in our changed_msg_keys set that are NOT in
        # msg_keys. We can not send FETCH's for messages that are no longer in
        # the folder.
        #
        # NOTE: XXX a 'pack' of a folder is going to cause us to send out many
        #       many FETCH's and most of these will be meaningless and
        #       basically noops. My plan is that pack's will rarely be done
        #       outside of asimapd, and asimapd will have a strategy for doing
        #       occasional packs at the end of a resync and when it does it
        #       will immediately update the in-memory copy of the list of
        #       sequences so that the next time a resync() is done it will not
        #       think all these messages have had their flags changed.
        #
        changed_msg_keys = changed_msg_keys & set(msg_keys)

        # And go through each message and publish a FETCH to every client with
        # all the flags that this message has.
        #
        for msg_key in sorted(list(changed_msg_keys)):
            flags = []
            for seq in list(seqs.keys()):
                if msg_key in seqs[seq]:
                    flags.append(seq_to_flag(seq))

            # Publish to every listening client except the one we are supposed
            # to ignore.
            #
            flags_str = " ".join(flags)
            msg_idx = msg_keys.index(msg_key) + 1

            uidstr = ""
            if publish_uids:
                try:
                    uidstr = f" UID {self.uids[msg_idx - 1]}"
                except IndexError:
                    logger.error(
                        "Mailbox %s: UID command but "
                        "message index: %d is not inside list "
                        "of UIDs, whose length is: %d",
                        self.name,
                        (msg_idx - 1),
                        len(self.uids),
                    )
            msg = f"* {msg_idx} FETCH (FLAGS ({flags_str}){uidstr})\r\n"
            await self._dispatch_or_pend_notifications(
                [msg], dont_notify=dont_notify
            )

    ##################################################################
    #
    async def _pack_if_necessary(self, msg_keys):
        """
        We use the array of message keys from the folder to determine if it is
        time to pack the folder.

        The key is if there is more than a 20% difference between the number of
        messages in the folder and the highest number in the folder and the
        folder is larger than 20. This tells us it has a considerable number
        of gaps and we then call pack on the folder.

        NOTE: Immediately after calling 'pack' we update the in-memory copy of
              the sequences with what is on the disk so that we do not generate
              spurious 'FETCH' messages on the next folder resync().

        Arguments:
        - `msgs`: The list of all message keys in the folder.
        """
        num_msgs = len(msg_keys)
        if num_msgs < self.folder_size_pack_limit:
            return

        if num_msgs / msg_keys[-1] > self.folder_ratio_pack_limit:
            return
        logger.debug(
            "Packing mailbox %s, num msgs: %d, max msg key: %d",
            self.name,
            num_msgs,
            msg_keys[-1],
        )
        await self.mailbox.apack()
        self.server.msg_cache.clear_mbox(self.name)
        self.sequences = await self.mailbox.aget_sequences()

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
        uids = list(uids)
        missing_uids = set(self.uids) - set(uids)

        # If none are missing then update the list of all uid's with what was
        # passed in and return. No EXPUNGE's need to be sent. At most we only
        # have new UID's added to our list.
        #
        if not missing_uids:
            self.uids = uids
            return

        logger.debug(
            "Mailbox %s, %d UID's missing. Sending EXPUNGEs.",
            self.name,
            len(missing_uids),
        )

        # Go through the UID's that are missing and send an expunge for each
        # one taking into account its position in the folder as we delete them.
        #
        notifications = []
        for uid in sorted(missing_uids, reverse=True):
            # NOTE: The expunge is the _message index_ of the message being
            #       deleted.
            which = self.uids.index(uid) + 1
            self.uids.remove(uid)
            notifications.append(f"* {which} EXPUNGE\r\n")
        self.uids = uids
        await self._dispatch_or_pend_notifications(notifications)

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
            uid_vv, uid = get_uidvv_uid(msg[UID_HDR])
            return (uid_vv, uid)
        except ValueError:
            logger.warning(
                "Mailbox %s: msg %d had malformed uid header: %s",
                self.name,
                msg_key,
                msg[UID_HDR],
            )
            return (None, None)

    ##################################################################
    #
    async def set_uid_in_msg(
        self, msg_key: int, new_uid: int, cache=True
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
                raise KeyError(f"No message with key: {msg_key}")
            else:
                raise

        msg = await self.get_and_cache_msg(msg_key, cache=cache)
        del msg[UID_HDR]
        msg[UID_HDR] = f"{self.uid_vv:010d}.{new_uid:010d}"

        # NOTE: The message size has changed! If the message is in the cache
        #       update the cached message size.
        #
        _ = self.server.msg_cache.get(self.name, msg_key, update_size=True)

        # NOTE: The following call will write the .mh_sequences file for this
        #       folder. We may want to check to make sure that the sequences we
        #       have for this message and the folder are in agreement (they
        #       should be, so the point is, if they are not, this is something
        #       we need to know is happening.)
        #
        await self.mailbox.asetitem(msg_key, msg)

        # Set its mtime to the mtime of the old file.
        #
        await utime(path, (mtime, mtime))
        return (self.uid_vv, new_uid)

    ##################################################################
    #
    async def _find_first_new_message(
        self, msg_keys: List[int], horizon: int = 0
    ) -> Optional[int]:
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
        # Maybe we should scan from the end of the mailbox backwards and look
        # for the first message with an mtime less than the folder/horizon.
        #
        if not msg_keys:
            return None
        horizon_mtime = self.mtime - horizon
        for msg_key in sorted(msg_keys):
            try:
                msg_path = mbox_msg_path(self.mailbox, msg_key)
                mtime = await aiofiles.os.path.getmtime(str(msg_path))
                if int(mtime) > horizon_mtime:
                    return msg_key
            except OSError as e:
                if e.errno == errno.ENOENT:
                    self.log.error(
                        "find_first_new_msg: Message %d no longer "
                        "exists, errno: %s" % (msg_key, str(e))
                    )
                raise
        return None

    ##################################################################
    #
    async def _find_msg_without_uidvv(
        self, msg_keys: List[int]
    ) -> Optional[int]:
        """
        This is a helper function for 'resync()'

        It looks through the folder from the highest numbered message down to
        find for the first message with a valid uid_vv.

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
        msg_keys = sorted(msg_keys, reverse=True)
        found = None
        for msg_key in msg_keys:
            uid_vv, uid = await self.get_uid_from_msg(msg_key, cache=False)
            if uid_vv == self.uid_vv:
                return found
            else:
                found = msg_key
        return found

    ##################################################################
    #
    async def _update_msg_uids(self, msg_keys: List[int], seq: Sequences):
        """
        This will loop through all of the msg_keys whose keys were passed
        in. We assume these keys are in order. We see if they have UID_VV.UID's
        in them. If they do not or it is out of sequence (UID's must be
        monotonically increasing at all times) then we have to generate new
        UID's for every message after the out-of-sequence one we encountered.

        NOTE: The important thing to note is that we store the uid_vv / uid for
              message _in the message_ itself. This way if the message is moved
              around we will know if it is out of sequence, a totally new
              message, or from a different mailbox.

              The downside is that we need to pick at every message in the
              msg_keys list to find this header information.

        Arguments:
        - `msg_keys`: A list of the message keys that we need to check.
          NOTE: This will frequently be a subset of all messages in the folder.
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
        # If we are not looking at too many messages (1000?), then be sure to
        # try to cache them in the message cache.
        #
        num_msgs = len(msg_keys)
        for i, msg_key in enumerate(msg_keys):
            if i % 200 == 0:
                logger.debug(
                    "mailbox: %s, check/update uids, at count %d, "
                    "msg: %d out of %d",
                    self.name,
                    i,
                    msg_key,
                    num_msgs,
                )
            msg = None
            if not redoing_rest_of_folder:
                # If the uid_vv is different or the uid is NOT
                # monotonically increasing from the previous uid then
                # we have to redo the rest of the folder.
                #
                msg = await self.mailbox.aget_message(msg_key)
                if UID_HDR not in msg:
                    uid_vv = (None,)
                    uid = None
                else:
                    uid_vv, uid = get_uidvv_uid(msg[UID_HDR])

                if (
                    uid_vv is None
                    or uid_vv != self.uid_vv
                    or uid is None
                    or uid <= prev_uid
                ):
                    redoing_rest_of_folder = True
                    logger.debug(
                        "mailbox: %s, Found msg %d uid_vv/uid "
                        "%s.%s out of sequence. Redoing rest of folder.",
                        self.name,
                        msg_key,
                        uid_vv,
                        uid,
                    )
                else:
                    uids_found.append(uid)
                    prev_uid = uid

            if redoing_rest_of_folder:
                # We are either replacing or adding a new UID header to this
                # message no matter what so do that.
                #
                # NOTE: Every message we set a uid on, whether it had one
                #       before or not, is added to the `Recent` sequence.
                #
                if msg is None:
                    msg = await self.mailbox.aget_message(msg_key)
                del msg[UID_HDR]
                msg[UID_HDR] = f"{self.uid_vv:010d}.{self.next_uid:010d}"
                await self.mailbox.asetitem(msg_key, msg)
                uids_found.append(self.next_uid)
                self.next_uid += 1

                # If the message is in the cache remove it.
                #
                self.server.msg_cache.remove(self.name, msg_key)

                # If the msg is not already in the Recent sequence add it.
                #
                if msg_key not in seq["Recent"]:
                    seq_changed = True
                    seq["Recent"].append(msg_key)

        # If we had to redo the folder then we believe it is indeed now
        # interesting so set the \Marked attribute on it.
        #
        if redoing_rest_of_folder:
            self.marked(True)

            # If seq_changed is True then we modified the sequencees too
            # so we need to re-write the sequences file.
            #
            if seq_changed is True:
                await self.mailbox.aset_sequences(seq)

        # And we are done.. we return the list of the uid's of all of the
        # messages we looked at or re-wrote (in order in which we encountered
        # them.)
        #
        return uids_found

    ##################################################################
    #
    async def _restore_from_db(self) -> bool:
        """
        Restores this mailbox's persistent state from the database.  If this
        mailbox does not exist in the db we create an entry for it with
        defaults.

        We return False if we restored the data from the db.

        We return True if we had to create the record for this mailbox in the
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
            await self.check_set_haschildren_attr()
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
            return True
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
            self.subscribed = bool(self.subscribed)
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
                name, sequence = row
                self.sequences[name] = [int(x) for x in sequence.split(",")]
        return False

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

        # Remove any empty sequences from our list of sequences
        #
        empty_seqs = [k for k, v in self.sequences.items() if not v]
        for seq in empty_seqs:
            del self.sequences[seq]

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
    async def selected(self, client: "Authenticated"):
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
        assert self.lock.this_task_has_read_lock()  # XXX remove when confident
        if client.name in self.clients:
            raise No(f"Mailbox '{self.name}' is already selected")

        if r"\Noselect" in self.attributes:
            raise No(f"You can not select the mailbox '{self.name}'")

        # A client has us selected. Turn of the expiry time.
        #
        self.expiry = None

        # When a client selects a mailbox we do a resync to make sure we
        # give it up to date information.
        #
        await self.resync()

        # Add the client to the mailbox _after_ we do the resync. This way
        # we will not potentially send EXISTS and RECENT messages to the
        # client twice.
        #
        self.clients[client.name] = client

        # Now send back messages to this client that it expects upon
        # selecting a mailbox.
        #
        # Flags on messages are represented by being in an MH sequence.
        # The sequence name == the flag on the message.
        #
        # NOTE: '\' is not a permitted character in an MH sequence name so
        #       we translate "Recent" to '\Recent'
        #
        # XXX We just did a resync.. we should be able to use `self.sequences`
        #     right? we are in a read lock, nothing else should have changed
        #     these..
        #
        seq = await self.mailbox.aget_sequences()
        msg_keys = await self.mailbox.akeys()
        push_data = []
        push_data.append(f"* {len(msg_keys)} EXISTS\r\n")
        push_data.append(f"* {len(seq['Recent'])} RECENT\r\n")
        if seq["unseen"]:
            # Message id of the first message that is unseen.
            #
            first_unseen = seq["unseen"][0]
            first_unseen = msg_keys.index(first_unseen) + 1
            push_data.append(f"* OK [UNSEEN {first_unseen}]\r\n")
        push_data.append(f"* OK [UIDVALIDITY {self.uid_vv}]\r\n")
        push_data.append(f"* OK [UIDNEXT {self.next_uid}]\r\n")

        # Each sequence is a valid flag.. we send back to the client all
        # of the system flags and any other sequences that are defined on
        # this mailbox.
        #
        flags = list(SYSTEM_FLAGS)
        for k in list(seq.keys()):
            if seq[k] and k not in SYSTEM_FLAG_MAP:
                flags.append(k)
        push_data.append(f"* FLAGS ({' '.join(flags)})\r\n")
        push_data.append(
            f"* OK [PERMANENTFLAGS ({' '.join(PERMANENT_FLAGS)})]\r\n"
        )
        await client.client.push(*push_data)

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
    async def append(
        self,
        msg: MHMessage,
        flags: Optional[List[str]] = None,
        date_time: Optional[datetime] = None,
    ) -> int:
        r"""
        Append the given message to this mailbox.
        Set the flags given. We also set the \Recent flag.
        If date_time is not given set it to 'now'.
        The internal date on the message is set to date_time.

        NOTE: Must be called with the mailbox read lock acquired.

        Arguments:
        - `message`: The email.message being appended to this mailbox
        - `flags`: A list of flags to set on this message
        - `date_time`: The internal date on this message
        """
        assert self.lock.this_task_has_read_lock()  # XXX remove when confident

        flags = [] if flags is None else flags
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
        async with (self.mailbox.lock_folder(), self.lock.write_lock()):
            # NOTE: This updates the .mh_sequences folder
            #
            key = await self.mailbox.aadd(msg)

            # if a date_time was supplied then set the mtime on the file to
            # that. We use mtime as our 'internal date' on messages.
            #
            if date_time:
                mtime = date_time.timestamp()
                await utime(mbox_msg_path(self.mailbox, key), (mtime, mtime))

        # We need to resync this mailbox so that we can get the UID of the
        # newly added message. This should be quick.
        #
        await self.resync(optional=False)
        uid_vv, uid = await self.get_uid_from_msg(key)
        if uid is None:
            raise Bad(
                f"Mailbox: {self.name}, unable to fetch UID for message {key}"
            )
        logger.debug(
            "Mailbox: %s, append message: %d, uid: %d, " "sequences: %s",
            self.name,
            key,
            uid,
            ", ".join(msg.get_sequences()),
        )
        return uid

    ##################################################################
    #
    async def expunge(self):
        """
        Perform an expunge. All messages in the 'Deleted' sequence are removed.

        We will sending untagged EXPUNGE messages to all clients on this mbox
        that are idling.

        For clients that have this mailbox selected but are NOT idling, we will
        put the EXPUNGE messages on the notifications list for delivery to
        those clients when possible.
        """
        assert self.lock.this_task_has_read_lock()  # XXX remove when confident

        # If there are no messages in the 'Deleted' sequence then we have
        # nothing to do.
        #
        seqs = await self.mailbox.aget_sequences()
        if len(seqs["Deleted"]) == 0:
            return

        async with (self.mailbox.lock_folder(), self.lock.write_lock()):
            # Remove the msg keys being deleted from the message cache.
            #
            del_keys = set(self.server.msg_cache.msg_keys_for_mbox(self.name))
            purge_keys = set(seqs["Deleted"]) - del_keys
            for msg_key in purge_keys:
                self.server.msg_cache.remove(self.name, msg_key)

            msg_keys = await self.mailbox.akeys()

            # We go through the to be deleted messages in reverse order so that
            # the expunges are "EXPUNGE <n>" "EXPUNGE <n-1>" etc. This is
            # mostly a nicety making the expunge messages a bit easier to read.
            #
            to_delete = sorted(seqs["Deleted"], reverse=True)

            for msg_key in to_delete:
                # Remove the message from the folder.. and also remove it from
                # our uids to message index mapping. (NOTE: 'which' is in IMAP
                # message sequence order, so its actual position in the array
                # is one less.
                #
                # Convert msg_key to IMAP seq num
                #
                which = msg_keys.index(msg_key) + 1
                msg_keys.remove(msg_key)

                # Remove UID from list of UID's in this folder. IMAP
                # sequence numbers start at 1.
                #
                self.uids.remove(self.uids[which - 1])
                await self.mailbox.aremove(msg_key)

                # Remove from all sequences.
                #
                for seq in seqs.keys():
                    if msg_key in seqs[seq]:
                        seqs[seq].remove(msg_key)

                expunge_msg = f"* {which} EXPUNGE\r\n"
                await self._dispatch_or_pend_notifications([expunge_msg])

            await self.mailbox.aset_sequences(seqs)

        # Resync the mailbox, but send NO exists messages because the mailbox
        # has shrunk: 5.2.: "it is NOT permitted to send an EXISTS response
        # that would reduce the number of messages in the mailbox; only the
        # EXPUNGE response can do this.
        #
        # Unless a client is sitting in IDLE, then it is okay send them
        # exists/recents.
        #
        await self.resync(optional=False)

    ##################################################################
    #
    async def search(
        self, search: IMAPSearch, uid_cmd: bool = False
    ) -> List[int]:
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
        assert self.lock.this_task_has_read_lock()  # XXX remove when confident

        # We get the folder lock to make sure any external systems that obey
        # the folder lock do not muck with this folder while we are doing a
        # search.
        #
        async with self.mailbox.lock_folder():
            if uid_cmd:
                logger.debug("Mailbox: %s, doing a UID SEARCH", self.name)

            # We get the full list of keys instead of using an iterator because
            # we need the max id and max uuid.
            #
            msg_keys = await self.mailbox.akeys()
            if not msg_keys:
                return []

            results: List[int] = []

            seq_max = len(msg_keys)
            # Before we do a search we do a resync to make sure that we have
            # attached uid's to all of our messages and various counts are up
            # to sync. But we do it with notify turned off because we can not
            # send any conflicting messages to this client (other clients that
            # are idling do get any updates though.)
            #
            await self.resync(notify=False)

            uid_vv, uid_max = await self.get_uid_from_msg(msg_keys[-1])
            if uid_vv is None or uid_max is None:
                # Nothing should be able to modify the folder. If something has
                # then let the upper level give the client the bad news.
                #
                raise Bad(f"Mailbox {self.name}: During SEARCH folder modified")

            # Go through the messages one by one and pass them to the search
            # object to see if they are or are not in the result set..
            #
            logger.debug(
                "Mailbox: %s, applying search to messages: %s",
                self.name,
                str(search),
            )

            for idx, msg_key in enumerate(msg_keys):
                # IMAP messages are numbered starting from 1.
                #
                i = idx + 1
                ctx = SearchContext(
                    self, msg_key, i, seq_max, uid_max, self.sequences
                )
                if await search.match(ctx):
                    # The UID SEARCH command returns uid's of messages
                    #
                    if uid_cmd:
                        uid = await ctx.uid()
                        assert uid
                        results.append(uid)
                    else:
                        results.append(i)
                await asyncio.sleep(0)
        return results

    #########################################################################
    #
    async def fetch(
        self, msg_set: MsgSet, fetch_ops: List[FetchAtt], uid_cmd: bool = False
    ):
        """
        Go through the messages in the mailbox. For the messages that are
        within the indicated message set parse them and pull out the data
        indicated by 'fetch_ops'

        Return a list of tuples where the first element is the IMAP message
        sequence number and the second is the requested data.

        The requested data itself is a list of tuples. The first element is the
        name of the data item from 'fetch_ops' and the second is the
        requested data.

        NOTE: You must call this with the Mailbox read lock acquired.  Since
              FETCH'ing messages can change their flags it may need to acquire
              the write lock.

        NOTE: Upon entering one of the assumptions we have is that the caller
              has completed a `resync` and that self.sequences, self.uids are
              up to date and correct.

              If, while fetching messages, an inconsistency shows up a
              `MailboxIconsistency` exception raised. (We expect the caller to
              send back a NO to the IMAP client.)

        Arguments:
        - `msg_set`: The set of messages we want to
        - `fetch_ops`: The things to fetch for the messags indiated in
          msg_set
        - `cmd`: The IMAP command. We need this to know whether or not this
                 is a UID command.
        """
        assert self.lock.this_task_has_read_lock()  # XXX remove when confident

        start_time = time.time()
        seq_changed = False
        num_results = 0
        async with self.mailbox.lock_folder():
            msgs = await self.mailbox.akeys()
            seqs = await self.mailbox.aget_sequences()
            fetch_started = time.time()

            try:
                # IF there are no messages in the mailbox there are no results.
                #
                if not msgs:
                    # If there are no messages but they asked for some messages
                    # (that are not there), return NO.. can not fetch that data.
                    if msg_set and not uid_cmd:
                        raise No("Mailbox empty.")
                    return

                uid_vv, uid_max = await self.get_uid_from_msg(msgs[-1])
                assert uid_max  # XXX If this is None should we force a resync?
                seq_max = len(msgs)

                # Generate the set of indices in to our folder for this
                # command.
                #
                # NOTE: `msg_idxs` is a list of integers. The integer represent
                #       an IMAP message sequence number, ie: It is the message
                #       at position <n> in the mailbox, starting from position
                #       1 (it is NOT 0-based). So message sequence number `1`
                #       referes to the first message in a box. That is msgs[0]
                #       from the list of message keys.
                #
                # NOTE: We map from message sequence number to index in to the
                #       msgs list when we are getting the value from the
                #       message list.
                #
                if uid_cmd:
                    # If we are doing a UID command we need to translate the
                    # values in `msg_set` to IMAP message sequence numbers.
                    #
                    # We to use the max uid for the sequence max.
                    #
                    uid_list = sequence_set_to_list(msg_set, uid_max, uid_cmd)

                    # We want to convert this list of UID's in to message
                    # indices So for every uid we we got out of the msg_set we
                    # look up its index in self.uids and from that construct
                    # the msg_idxs list. Missing UID's are fine. They just do
                    # not get added to the list. From rfc3501:
                    #
                    #    "A non-existent unique identifier is ignored without
                    #     any error message generated.  Thus, it is possible
                    #     for a UID FETCH command to return an OK without any
                    #     data ..."
                    #
                    msg_idxs = []
                    for uid in uid_list:
                        if uid in self.uids:
                            mi = self.uids.index(uid) + 1
                            msg_idxs.append(mi)

                    # Also, if this is a UID FETCH then we MUST make sure UID is
                    # one of the fields being fetched, and if it is not add it.
                    #
                    if not any([x.attribute == "uid" for x in fetch_ops]):
                        fetch_ops.insert(0, FetchAtt(FetchOp.UID))

                else:
                    msg_idxs = sequence_set_to_list(msg_set, seq_max)

                # msg_idx is a list of IMAP message sequence numbers.  Go
                # through each message and apply the fetch_ops.fetch() to it
                # building up a set of data to respond to the client
                # with. Remember IMAP message sequence number `1` refers to the
                # first message in the folder, ie: msgs[0].
                #
                fetch_started = time.time()
                fetch_yield_times = []
                for idx in msg_idxs:
                    single_fetch_started = time.time()
                    try:
                        msg_key = msgs[idx - 1]
                    except IndexError:
                        # Every key in msg_idx should be in the folder. If it is
                        # not then something is off between our state and the
                        # folder's state.
                        #
                        log_msg = (
                            f"fetch: Attempted to look up message index "
                            f"{idx - 1}, but msgs is only of length {len(msgs)}"
                        )
                        logger.warning(log_msg)
                        raise MailboxInconsistency(log_msg)

                    ctx = SearchContext(
                        self, msg_key, idx, seq_max, uid_max, seqs
                    )
                    fetched_flags = False
                    fetched_body = False
                    iter_results = []

                    for elt in fetch_ops:
                        iter_results.append(await elt.fetch(ctx))
                        # If one of the FETCH ops gets the FLAGS we want to
                        # know and likewise if one of the FETCH ops gets the
                        # BODY (but NOT BODY.PEEK) we want to know. Both of
                        # these operations can potentially change the flags of
                        # the message.
                        #
                        if elt.attribute == "body" and elt.peek is False:
                            fetched_body = True
                        if elt.attribute == "flags":
                            fetched_flags = True

                    # If we did a FETCH FLAGS and the message was in the
                    # 'Recent' sequence then remove it from the 'Recent'
                    # sequence. Only one client gets to actually see that a
                    # message is 'Recent.'
                    #
                    cached_msg = self.server.msg_cache.get(self.name, msg_key)
                    if fetched_flags:
                        if cached_msg:
                            cached_msg.remove_sequence("Recent")
                        if msg_key in seqs["Recent"]:
                            seqs["Recent"].remove(msg_key)
                            seq_changed = True

                    # If we dif a FETCH BODY (but NOT a BODY.PEEK) then the
                    # message is removed from the 'unseen' sequence (if it was
                    # in it) and added to the 'Seen' sequence (if it was not in
                    # it.)
                    #
                    if fetched_body:
                        if cached_msg:
                            cached_msg.remove_sequence("unseen")
                            cached_msg.add_sequence("Seen")
                        if msg_key in seqs["unseen"]:
                            seqs["unseen"].remove(msg_key)
                            seq_changed = True
                        if msg_key not in seqs["Seen"]:
                            seqs["Seen"].append(msg_key)
                            seq_changed = True

                    # Done applying FETCH to all of the indicated messages.  If
                    # the sequences changed we need to write them back out to
                    # disk.
                    #
                    if seq_changed:
                        async with self.lock.write_lock():
                            await self.mailbox.aset_sequences(seqs)

                    fetch_yield_times.append(time.time() - single_fetch_started)
                    yield (idx, iter_results)
                    num_results += 1
                    await asyncio.sleep(0)

                if seq_changed:
                    await self.resync(optional=False)

            finally:
                now = time.time()
                total_time = now - start_time
                fetch_time = now - fetch_started
                mean_yield_time = (
                    fmean(fetch_yield_times) if fetch_yield_times else 0.0
                )
                median_yield_time = (
                    median(fetch_yield_times) if fetch_yield_times else 0.0
                )
                stdev_yield_time = (
                    stdev(fetch_yield_times, mean_yield_time)
                    if len(fetch_yield_times) > 2
                    else 0.0
                )

                logger.debug(
                    "FETCH finished, mailbox: '%s', num results: %d, total duration: %f, "
                    "fetch duration: %f, mean time per fetch: %f, median: "
                    "%f, stdev: %f",
                    self.name,
                    num_results,
                    total_time,
                    fetch_time,
                    mean_yield_time,
                    median_yield_time,
                    stdev_yield_time,
                )

    ##################################################################
    #
    async def store(
        self,
        msg_set: MsgSet,
        action: StoreAction,
        flags: List[str],
        uid_cmd: bool = False,
    ):
        r"""
        Update the flags (sequences) of the messages in msg_set.

        Arguments:
        - `msg_set`: The set of messages to modify the flags on
        - `action`: one of REMOVE_FLAGS, ADD_FLAGS, or REPLACE_FLAGS
        - `flags`: The flags to add/remove/replace
        - `uid_cmd`: Used to determine if this is a uid command or not
        """
        assert self.lock.this_task_has_read_lock()  # XXX remove when confident

        if r"\Recent" in flags:
            raise No(r"You can not add or remove the '\Recent' flag")

        if action not in StoreAction:
            raise Bad(f"'{action}' is an invalid STORE action")

        async with self.mailbox.lock_folder():
            # Get the list of message keys that msg_set indicates.
            #
            all_msg_keys = await self.mailbox.akeys()
            seq_max = len(all_msg_keys)

            if uid_cmd:
                # If we are doing a 'UID FETCH' command we need to use the max
                # uid for the sequence max.
                #
                uid_vv, uid_max = await self.get_uid_from_msg(all_msg_keys[-1])
                assert uid_max
                uid_list = sequence_set_to_list(msg_set, uid_max, uid_cmd)

                # We want to convert this list of UID's in to message indices
                # So for every uid we we got out of the msg_set we look up its
                # index in self.uids and from that construct the msg_idxs
                # list. Missing UID's are fine. They just do not get added to
                # the list.
                #
                msg_idxs = []
                for uid in uid_list:
                    if uid in self.uids:
                        mi = self.uids.index(uid) + 1  # msg keys are 1-based
                        msg_idxs.append(mi)

            else:
                msg_idxs = sequence_set_to_list(msg_set, seq_max)

            # Build a set of msg keys that are just the messages we want to
            # operate on.
            #
            msg_keys = [all_msg_keys[x - 1] for x in msg_idxs]

            # Convert the flags to MH sequence names..
            #
            flags = [flag_to_seq(x) for x in flags]
            store_start = time.time()

            async with self.lock.write_lock():
                seqs = await self.mailbox.aget_sequences()
                for key in msg_keys:
                    msg = await self.get_and_cache_msg(key)
                    # XXX We should make sure that the message's sequences
                    #     match what we loaded above just in case it was a
                    #     cached message and its sequence info was not updated.
                    match action:
                        case StoreAction.ADD_FLAGS | StoreAction.REMOVE_FLAGS:
                            for flag in flags:
                                # Make sure a sequence exists for every flag
                                # (even if removing flags)
                                # XXX seqs is defaultdict, do not need this
                                if flag not in seqs:
                                    seqs[flag] = []
                                match action:
                                    case StoreAction.ADD_FLAGS:
                                        _help_add_flag(key, seqs, msg, flag)
                                    case StoreAction.REMOVE_FLAGS:
                                        _help_remove_flag(key, seqs, msg, flag)
                        case StoreAction.REPLACE_FLAGS:
                            _help_replace_flags(key, seqs, msg, flags)
                await self.mailbox.aset_sequences(seqs)
        logger.debug("Completed, took %f seconds", time.time() - store_start)

    ##################################################################
    #
    @with_timeout(15)
    async def copy(
        self, msg_set: MsgSet, dst_mbox: "Mailbox", uid_command: bool = False
    ):
        r"""
        Copy the messages in msg_set to the destination mailbox.
        Flags (sequences), and internal date are preserved.
        Messages get the '\Recent' flag in the new mailbox.
        Arguments:
        - `msg_set`: Set of messages to copy.
        - `dst_mbox`: mailbox instance messages are being copied to
        - `uid_command`: True if this is for a UID SEARCH command, which means
          we have to return not message sequence numbers but message UID's.

        NOTE: Since this has to copy messages between mailboxes and ensure that
              things happen properly this method will get both and write locks
              on the source and destination mailboxes.

              THUS: You *must not* have a readlock on either Mailbox when
              calling this method (because you can not nest read locks if you
              also want to get a write lock.)

        NOTE: This method is called with a timeout to catch possible deadlock
              bugs (it will at least alert us to their existence)

        XXX we read all the messages we are copying into memory, mainly to make
            sure we do not try to hold read locks/write locks on more than one
            mailbox at a time in this process.  This means it is possible to
            run out of memory. If this happens we should consider using a
            tmpdir to store the messages between the read and write
            operations. Be alot slower but would almost never run out of room.
        """
        copy_msgs: List[Tuple[MHMessage, float]] = []

        async with self.lock.read_lock():
            # We get the full list of keys instead of using an iterator because
            # we need the max id and max uuid.
            #
            msgs = await self.mailbox.akeys()
            uid_vv, uid_max = await self.get_uid_from_msg(msgs[-1], cache=True)
            if uid_vv is None or uid_max is None:
                await self.resync()
                uid_vv, uid_max = await self.get_uid_from_msg(
                    msgs[-1], cache=True
                )
            assert uid_max  # Makes mypy happy.

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
                        msg_idx = self.uids.index(uid) + 1
                        msg_idxs.append(msg_idx)
            else:
                msg_idxs = sequence_set_to_list(msg_set, seq_max)

            src_uids = []
            for idx in msg_idxs:
                key = msgs[idx - 1]  # NOTE: imap messages start from 1.

                # We are going to read all messages into memory that we are
                # copying, and then write them out to the destination mbox
                # outside of the mbox lock (to avoid possible deadlocks.)

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
                mtime = await aiofiles.os.path.getmtime(
                    mbox_msg_path(self.mailbox, key)
                )
                msg = await self.get_and_cache_msg(key)
                msg.add_sequence("Recent")
                copy_msgs.append((msg, mtime))
                uid_vv, uid = await self.get_uid_from_msg(key)
                src_uids.append(uid)
                await asyncio.sleep(0)

        # We have now read all the messages we are copying. Write them to the
        # dest folder. Sequences are preserved since we are reading and writing
        # MHMessages (furthermore we added all the messages to the sequence
        # 'Recent' after we read it in.)
        #
        dst_keys = []
        async with (dst_mbox.mailbox.lock_folder(), dst_mbox.lock.read_lock()):
            async with dst_mbox.lock.write_lock():
                for msg, mtime in copy_msgs:
                    key = await dst_mbox.mailbox.aadd(msg)
                    dst_keys.append(key)
                    await utime(
                        mbox_msg_path(dst_mbox.mailbox, key), (mtime, mtime)
                    )

            # Done copying.. resync to give all the messages proper uids for
            # their new mailbox, update mailbox sequences, etc.
            #
            del copy_msgs
            await dst_mbox.resync(optional=False)

            # Now get the uid's for all the newly copied messages.  NOTE: Since
            # we added the messages in the same order they were copied we know
            # that our src_uids and dst_uids refer to the correct messages.
            #
            dst_uids = []
            for k in dst_keys:
                uid_vv, uid = await dst_mbox.get_uid_from_msg(k)
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
        return int(max(path_mtime, seq_mtime))

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
        if name.isdigit():
            raise InvalidMailbox(
                "Due to MH restrictions you can not create a "
                f"mailbox that is just digits: '{name}'"
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
                raise MailboxExists(f"Mailbox '{name}' already exists")

        # The mailbox does not exist, we can create it.
        #
        # NOTE: We need to create any intermediate path elements, and what is
        #       more those intermediate path elements are not actually created
        #       mailboxes until they have been explicitly 'created'. But that
        #       is annoying. I will just create the intermediary directories.
        #
        mbox_chain: List[str] = []
        mboxes: List["Mailbox"] = []
        for chain_name in name.split("/"):
            mbox_chain.append(chain_name)
            mbox_name = "/".join(mbox_chain)
            MH(server.maildir / mbox_name)
            mbox = await server.get_mailbox(mbox_name)
            mboxes.append(mbox)

        # And now go through all of those mboxes and update their children
        # attributes and make sure the underlying db is updated with this
        # information.
        #
        for mbox in mboxes:
            await mbox.check_set_haschildren_attr()
            await mbox.commit_to_db()

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
        if name == "inbox":
            raise InvalidMailbox("You are not allowed to delete the inbox")

        mbox = await server.get_mailbox(name)
        do_delete = False
        server.msg_cache.clear_mbox(name)
        async with (mbox.mailbox.lock_folder(), mbox.lock.read_lock()):
            inferior_mailboxes = await mbox.mailbox.alist_folders()

            # You can not delete a mailbox that has the '\Noselect' attribute
            # and has inferior mailboxes.
            #
            if r"\Noselect" in mbox.attributes and inferior_mailboxes:
                raise InvalidMailbox(f"The mailbox '{name}' is already deleted")

            # You can not delete a mailbox that has the '\Noselect' attribute
            # and is subscribed. (BTW: This means that this mailbox was already
            # deleted, but not removed because it still has subscribers.)
            #
            if r"\Noselect" in mbox.attributes and mbox.subscribed:
                raise InvalidMailbox(
                    f"The mailbox '{name}' is still subscribed"
                )

            async with mbox.lock.write_lock():
                # When deleting a mailbox every message in that mailbox will be
                # deleted.
                #
                await mbox.mailbox.aclear()
                mbox.num_msgs = 0
                mbox.num_recent = 0
                mbox.uids = []
                mbox.sequences = defaultdict(list)

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
                    mbox.uid_vv = await server.get_next_uid_vv()
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
        if parent_name:
            parent_mbox = await server.get_mailbox(parent_name, expiry=10)
            async with (
                parent_mbox.lock.read_lock(),
                parent_mbox.lock.write_lock(),
            ):
                await parent_mbox.check_set_haschildren_attr()
                await parent_mbox.commit_to_db()

        # And remove the mailbox from the filesystem.
        #
        if do_delete:
            try:
                await server.mailbox.aremove_folder(name)
            except NotEmptyError as e:
                logger.warning("mailbox %s 'not empty', %s", name, str(e))
                path = mbox_msg_path(server.mailbox, name)
                logger.info("using shutil to delete '%s'", path)
                shutil.rmtree(path)

    ####################################################################
    #
    @classmethod
    async def rename(
        cls, old_name: str, new_name: str, server: "IMAPUserServer"
    ):
        """
        Rename a mailbox from old_name to new_name.

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
            raise MailboxExists(f"Destination mailbox '{new_name}' exists")

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
        cls,
        ref_mbox_name: str,
        mbox_match: str,
        server: "IMAPUserServer",
        lsub: bool = False,
    ):
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

        # Every '*' becomes '.*' and every % becomes [^/]
        #
        mbox_match = mbox_match.replace(r"\*", r".*").replace(r"%", r"[^\/]*")

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
            yield (mbox_name, attributes)


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
    async def _do_rename_folder(
        old_mbox: Mailbox, old_id: int, mbox_new_name: str
    ):
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
                (mbox_new_name, old_id),
            )

            mb = srvr.active_mailboxes[mbox_old_name]
            del srvr.active_mailboxes[mbox_old_name]
            mb.name = mbox_new_name
            mb.mailbox = srvr.mailbox.get_folder(mbox_new_name)
            srvr.active_mailboxes[mbox_new_name] = mb
        srvr.msg_cache.clear_mbox(mbox_old_name)

    # Make a sym link to where the new mbox is going to be. This way as we move
    # any subordinate folders if they get activity before we have finished the
    # entire move they will not just fail. When done we need to remove the
    # symlink and rename the dir from the old name to the new name.
    #
    old_name = mbox.name
    old_dir = mbox_msg_path(srvr.mailbox, old_name)
    new_dir = mbox_msg_path(srvr.mailbox, new_name)

    async with mbox.lock.read_lock():
        await aiofiles.os.symlink(old_dir, new_dir)

        # Get all the mailboxes we have to rename (this mbox may have children)
        #
        to_change = {}
        async for mbox_old_name, mbox_id in srvr.db.query(
            "SELECT name,id FROM mailboxes WHERE name=? OR name LIKE ?",
            (old_name, f"{old_name}/%"),
        ):
            mbox_new_name = new_name + mbox_old_name[len(old_name) :]
            to_change[mbox_old_name] = (mbox_new_name, mbox_id)

        for old, (new_mbox_name, old_id) in to_change.items():
            # If this is the mbox we were passed in, we already have a read
            # lock so we do not need to acquire it.
            #
            old_mbox = await srvr.get_mailbox(old, expiry=10)
            if old_mbox.name == mbox.name:
                async with old_mbox.lock.write_lock():
                    await _do_rename_folder(old_mbox, old_id, new_mbox_name)
            else:
                async with (
                    old_mbox.lock.read_lock(),
                    old_mbox.lock.write_lock(),
                ):
                    await _do_rename_folder(old_mbox, old_id, new_mbox_name)

        await srvr.db.commit()

        # and now we remove the symlink and rename the old dir to the new dir
        #
        await aiofiles.os.remove(new_dir)
        await aiofiles.os.rename(old_dir, new_dir)

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
        async with (mbox.lock.read_lock(), mbox.lock.write_lock()):
            async with (new_mbox.lock.read_lock(), new_mbox.lock.write_lock()):
                for key in await mbox.mailbox.akeys():
                    try:
                        msg = await mbox.mailbox.aget_message(key)
                    except KeyError:
                        continue

                    # Replace the asimap uid since this is a new folder.
                    #
                    uid = f"{new_mbox.uid_vv:010d}.{new_mbox.next_uid:010d}"
                    new_mbox.next_uid += 1
                    del msg["X-asimapd-uid"]
                    msg["X-asimapd-uid"] = uid
                    await new_mbox.mailbox.aadd(msg)
                    try:
                        await mbox.mailbox.aremove(key)
                    except KeyError:
                        pass
            new_mbox.sequences = await new_mbox.mailbox.aget_sequences()
            await new_mbox.commit_to_db()

    async with mbox.lock.read_lock():
        await mbox.resync(force=True)
    async with new_mbox.lock.read_lock():
        await new_mbox.resync(force=True)


####################################################################
#
def _help_add_flag(key: int, seqs: Sequences, msg: MHMessage, flag: str):
    """
    Helper function for the logic to add a message to a sequence. Updating
    both the sequences associated with the MHMessage and the sequences dict.
    """
    msg.add_sequence(flag)
    if key not in seqs[flag]:
        seqs[flag].append(key)

    # Make sure that the Seen and unseen sequences are updated properly.
    #
    match flag:
        case "Seen":
            msg.remove_sequence("unseen")
            if key in seqs["unseen"]:
                seqs["unseen"].remove(key)
        case "unseen":
            msg.remove_sequence("Seen")
            if key in seqs["Seen"]:
                seqs["Seen"].remove(key)


####################################################################
#
def _help_remove_flag(key: int, seqs: Sequences, msg: MHMessage, flag: str):
    """
    Helper function for the logic to remove a message to a sequence. Updating
    both the sequences associated with the MHMessage and the sequences dict.
    """
    msg.remove_sequence(flag)
    if key in seqs[flag]:
        seqs[flag].remove(key)

    # Make sure that the Seen and unseen sequences are updated properly.
    #
    match flag:
        case "Seen":
            msg.add_sequence("unseen")
            if key not in seqs["unseen"]:
                seqs["unseen"].append(key)
        case "unseen":
            msg.add_sequence("Seen")
            if key not in seqs["Seen"]:
                seqs["Seen"].append(key)


####################################################################
#
def _help_replace_flags(
    key: int, seqs: Sequences, msg: MHMessage, flags: List[str]
):
    r"""
    Replace the flags on the message.
    The flag `\Recent` if present is not affected.
    The flag `unseen` if present is not affected unless `\Seen` is in flags.
    """
    msg_seqs = set(msg.get_sequences())
    msg.set_sequences(flags)

    # If `\Recent` was present, then it is added back.
    #
    if "Recent" in msg_seqs:
        msg.add_sequence("Recent")

    # If `\Seen` is not set in flags, then `unseen` is added.
    #
    if "Seen" not in flags:
        msg.add_sequence("unseen")

    new_msg_seqs = set(msg.get_sequences())
    to_remove = msg_seqs - new_msg_seqs

    for flag in flags:
        if key not in seqs[flag]:
            seqs[flag].append(key)
    for flag in to_remove:
        if key in seqs[flag]:
            seqs[flag].remove(key)


####################################################################
#
def _help_update_msg_sequences_in_cache(
    msg_cache: MessageCache,
    mbox_name: str,
    msg_keys: List[int],
    sequences: Sequences,
):
    """
    A helper routine used in the Mailbox where we are modifying sequences
    and storing them back to the folder's .mh_sequences file. We need to make
    sure that all of the messages in the cache for this mbox have their
    sequence information udpated.

    `msg_keys` is the list of all message keys in the MH folder.
    `sequences` is the set of sequence data from .mh_sequences in the MH folder.

    XXX maybe this routine should be a MessageCache method?
    """
    cached_msg_keys = msg_cache.msg_keys_for_mbox(mbox_name)
    for key in cached_msg_keys:
        # Since we are going through all keys for this mbox in the cache we do
        # not want to mess up the LRU for this mbox so do not update the
        # entries as we retrieve them.
        #
        msg = msg_cache.get(mbox_name, key, do_not_update=True)
        assert msg  # it was in the keys, it better be true.

        # If we encounter a msg_key that is in the cache but not in the
        # mailbox, remove it.
        #
        if key not in msg_keys:
            msg_cache.remove(mbox_name, key)

        update_message_sequences(key, msg, sequences)
