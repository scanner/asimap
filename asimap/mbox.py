"""
The module that deals with the mailbox objects.

There will be a mailbox per MH folder (but not one for the top level
that holds all the folders.)
"""

# system imports
#
import asyncio
import logging
import os.path
import re
import shutil
import stat
import time
from collections import defaultdict
from copy import copy
from datetime import datetime
from mailbox import FormatError, MHMessage, NoSuchMailboxError, NotEmptyError
from pathlib import Path
from statistics import fmean, median, stdev
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple, Union

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
from .mh import MH, Sequences, aread_message, awrite_message
from .parse import (
    CONFLICTING_COMMANDS,
    IMAPClientCommand,
    IMAPCommand,
    StoreAction,
)
from .search import IMAPSearch, SearchContext
from .utils import MsgSet, compact_sequence, sequence_set_to_list, utime

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


####################################################################
#
def intersect(a: IMAPClientCommand, b: IMAPClientCommand) -> bool:
    """
    A helper function that determines if the msg_set_as_set for two
    IMAPClientCommands intersect or not.  If either set is None then it is
    considered the empty set.
    """
    set_a = a.msg_set_as_set if a.msg_set_as_set else set()
    set_b = b.msg_set_as_set if b.msg_set_as_set else set()
    return bool(set_a.intersection(set_b))


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
    def __init__(self, name: str, server: "IMAPUserServer", expiry: int = 900):
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
        self.logger = logging.getLogger(f"asimap.mbox.Mailbox:'{name}'")
        self.server = server
        self.name = name
        self.id = None
        self.uid_vv = 0
        self.mtime: int = 0
        self.next_uid = 1
        self.num_msgs = 0
        self.num_recent = 0
        self.folder_size_pack_limit = self.FOLDER_SIZE_PACK_LIMIT
        self.folder_ratio_pack_limit = self.FOLDER_RATIO_PACK_LIMIT

        # When an IMAP Command begins executing on the mailbox the management
        # task makes sure that the list of msg_keys and sequences are up to
        # date. So a command can assume at the start of its run that this is
        # the list of valid message keys for a folder.
        #
        self.msg_keys: List[int] = []

        # List of the UID's and mh msg keys of the messages in this
        # mailbox. They are in IMAP message sequence order (ie: first message
        # in the mailbox, its uid is in self.uids[0]) (note when converting
        # from IMAP message sequence numbers, you have to subtract one since
        # they are 1-ordered, not 0-ordered.)
        #
        self.uids: List[int] = []
        self.subscribed = False

        # Time in seconds since the unix epoch when a resync was last tried.
        #
        self.last_resync = 0.0

        # An in-memory copy of the .mh_sequences file.  Whenever it is changed
        # in memory the file on disk is updated at the same time while a lock
        # on the MH folder is held.
        #
        # The only time the .mh_sequences folder on disk is changed outside of
        # our control is when new messages are added to a folder and the unseen
        # sequence is updated.
        #
        self.sequences: Sequences = defaultdict(set)
        self.mh_sequences_lock = asyncio.Lock()

        # Since the db access is async we need to make sure only one task is
        # reading or writing this mbox's records in the db at a time.
        #
        self.db_lock = asyncio.Lock()

        # You can not instantiate a mailbox that does not exist in the
        # underlying file system.
        #
        try:
            self.mailbox: MH = server.mailbox.get_folder(name)
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

        # Every active mailbox has a management task. This task looks at the
        # task queue and tells invdividual imap commands that they can continue
        # (by signaling their event). It lets multiple read-only tasks run in
        # parallel. Before every imap command is allowed to run whether or not
        # to do a resync is evaluated (and then done)
        #
        self.task_queue: asyncio.Queue[IMAPClientCommand] = asyncio.Queue()
        self.mgmt_task: asyncio.Task
        self.executing_tasks: List[IMAPClientCommand] = []

        # If an imap command, when it finishes, wants a non-optional resync for
        # "reasons" before the next imap command gets processed it sets this
        # False. The management task will pass this to the resync method.
        #
        self.optional_resync: bool = True

        # It is possible for a mailbox to be deleted while there are commands
        # in the task_queue waiting their chance to be processed. We need a way
        # to tell these commands when they get to run that the mailbox they are
        # working on has been deleted and will know to give up and exit. If
        # this boolean is True then the mailbox has been deleted.
        #
        self.deleted: bool = False

    ####################################################################
    #
    def __del__(self):
        """
        Make sure that the management task is cancelled on the way out.
        """
        try:
            if hasattr(self, "mgmt_task") and not self.mgmt_task.done():
                self.mgmt_task.cancel()
        except RuntimeError:
            # Include runtime errors 'event loop is closed'. Happens when del
            # gets called after the event loop is gone.
            #
            pass

        if self.name in self.server.active_mailboxes:
            del self.server.active_mailboxes[self.name]

    ####################################################################
    #
    @classmethod
    async def new(cls, *args, **kwargs) -> "Mailbox":
        """
        We can not have __init__() be an async function, yet we need to do
        some async operations when we instantiate a mailbox. This code is for
        doing both. So this is the entry point to instantiate a mailbox.
        """
        # See if we need to a uid validity check.
        #
        # validity_check = kwargs.pop("validity_check", False)
        mbox = cls(*args, **kwargs)
        # If the .mh_sequence file does not exist create it.
        #
        mh_seq_fname = mbox_msg_path(mbox.mailbox, ".mh_sequences")
        if not await aiofiles.os.path.exists(mh_seq_fname):
            f = await aiofiles.open(mh_seq_fname, "a")
            await f.close()
            os.chmod(mh_seq_fname, stat.S_IRUSR | stat.S_IWUSR)

        # After initial setup fill in any persistent values from the
        # database (and if there are none, then create an entry in the
        # db for this mailbox.)
        #
        new_folder = await mbox._restore_from_db()

        # If this new mbox has `\Noselect` then it is essentially a deleted
        # mailbox. We will return it but we will not check for new messages and
        # we will not create a management task.
        #
        if r"\Noselect" not in mbox.attributes:
            optional = not (new_folder or r"\Marked" in mbox.attributes)
            async with mbox.mailbox.lock_folder():
                await mbox.check_new_msgs_and_flags(optional=optional)
            mbox.mgmt_task = asyncio.create_task(
                mbox.management_task(), name=f"mbox '{mbox.name}' mgmt task"
            )
        return mbox

    ####################################################################
    #
    async def shutdown(self, commit_db: bool = True):
        """
        Cancel the management task, wait for it to exit.
        Make sure any pending IMAP Tasks get told to go away.
        Make sure mbox is commited to db if commit_db is True.
        """
        self.deleted = True  # Causes any waiting IMAP Commands to exit.
        self.expiry = 0.0
        wait_for_mgmt_task = False
        if hasattr(self, "mgmt_task") and not self.mgmt_task.done():
            self.mgmt_task.cancel()
            wait_for_mgmt_task = True

        try:
            while True:
                imap_cmd = self.task_queue.get_nowait()
                imap_cmd.ready.set()
        except asyncio.QueueEmpty:
            pass

        if hasattr(self, "mgmt_task") and wait_for_mgmt_task:
            try:
                await self.mgmt_task
            except asyncio.CancelledError:
                pass

        if commit_db:
            await self.commit_to_db()

    ####################################################################
    #
    def would_conflict(self, imap_cmd: IMAPClientCommand) -> bool:
        r"""
        Returns True if the given imap_cmd would conflict with any other
        currently running command.

        For example:

        - CLOSE and EXPUNGE conflict with all commands IF THE `\Deleted`
          sequence is not empty. Otherwise they conflict with no other
          commands.

        - CLOSE [EXAMINE] does not conflict with any command

        - FETCH BODY.PEEK conflicts with STORE if they operate on the
          same messages.
        """
        # IF there are no other executing IMAP commands, nothing to conflict
        # with.
        #
        if not self.executing_tasks:
            return False

        # Certain commands block any other commands from being allowed to
        # execute. If any of these tasks are currently executing then
        # everything else would conflict.
        #
        if any(x.command in CONFLICTING_COMMANDS for x in self.executing_tasks):
            return True

        # NOTE: We get here if there are other executing tasks, so we do not
        #       need to check if there are any other executing tasks. We know
        #       there are.
        #
        match imap_cmd.command:
            case (
                IMAPCommand.APPEND
                | IMAPCommand.CHECK
                | IMAPCommand.DELETE
                | IMAPCommand.RENAME
            ):
                # Can only run when there are no other commands running
                #
                return True

            case IMAPCommand.CLOSE | IMAPCommand.EXPUNGE:
                # Non-conflicting if there are no messages to delete.
                #
                # NOTE: Once this command begins executing it will block any
                #       new task from executing until they finish.
                #
                if self.sequences.get("Deleted", []):
                    # If there are messages to be expunged, can only run when
                    # there are no other commands running
                    #
                    return True
                return False

            case IMAPCommand.COPY:
                # NOTE: We do not need to check for EXPUNGE. If an EXPUNGE is
                #       already operating we would have returned True in the
                #       CONFLICTING_COMMANDS check at the top of this method.
                #
                # Conflicts with STORE, and FETCH.BODY (no peek) that operate
                # on the same messages.
                #
                for cmd in self.executing_tasks:
                    match cmd.command:
                        case IMAPCommand.STORE:
                            # We conflict with a STORE command that is already
                            # running if the COPY is operating on the same
                            # messages.
                            #
                            if intersect(imap_cmd, cmd):
                                return True
                        case IMAPCommand.FETCH:
                            # If the FETCH command is NOT `peek` (ie: it can
                            # modify flags), and the msg set overlaps, it
                            # conflicts.
                            if not cmd.fetch_peek and intersect(imap_cmd, cmd):
                                return True
                return False

            case IMAPCommand.FETCH:
                # If the FETCH command is no_peek == False it will conflict
                # with any command that depends on overall message state of the
                # folder (since it might modify sequences)
                #
                for cmd in self.executing_tasks:
                    if not imap_cmd.fetch_peek:
                        match cmd.command:
                            case IMAPCommand.SEARCH:
                                # non-peek fetch's will conflict with any
                                # command that depends on overall message
                                # state.
                                #
                                return True
                            case (
                                IMAPCommand.COPY
                                | IMAPCommand.FETCH
                                | IMAPCommand.STORE
                            ):
                                # For COPY, FETCH, and STORE it will only
                                # conflict with those commands if they are
                                # operating on the same messages.
                                if intersect(imap_cmd, cmd):
                                    return True
                    else:
                        # A FETCH PEEK will still conflict with a STORE if they
                        # intersect (although maybe they should only conflict
                        # if the FETCH fetchees flags... but I suspect that
                        # doees not happen often enough to warrant the extra
                        # logic.)
                        #
                        if cmd.command == IMAPCommand.STORE and intersect(
                            imap_cmd, cmd
                        ):
                            return True

                return False

            case (
                IMAPCommand.NOOP
                | IMAPCommand.SELECT
                | IMAPCommand.STATUS
                | IMAPCommand.EXAMINE
            ):
                # These conflict with nothing except for the conflicting
                # commands.
                #
                return False

            case IMAPCommand.SEARCH:
                # SEARCH can not run if the other currently
                # executing commands do alter message state. This means:
                # - STORE
                # - FETCH, fetch_peek=False
                #
                for cmd in self.executing_tasks:
                    # NOTE: Commands like APPEND, EXPUNGE were handled above.
                    #
                    match cmd.command:
                        case IMAPCommand.FETCH:
                            if not cmd.fetch_peek:
                                return True
                        case IMAPCommand.STORE:
                            return True
                return False

            case IMAPCommand.STORE:
                # STORE can not run if other commands that depend on overall
                # mailbox state are running.
                #
                # It can run if the other command is STORE or FETCH and they
                # are not operating on the same messages.
                #
                for cmd in self.executing_tasks:
                    match cmd.command:
                        case IMAPCommand.SEARCH:
                            return True
                        case (
                            IMAPCommand.STORE
                            | IMAPCommand.FETCH
                            | IMAPCommand.COPY
                        ):
                            # Store can operate the same time as other FETCH
                            # and STORE's as long as they operate on different
                            # messages.
                            #
                            if intersect(imap_cmd, cmd):
                                return True
                return False
            case _:
                # We get any other IMAP command we have not specifically
                # accounted, we mark it as conflicting.
                #
                raise RuntimeError(
                    f"Uhandled conflict support for command '{imap_cmd.qstr()}'"
                )

    ####################################################################
    #
    async def command_can_proceed(self, imap_cmd: IMAPClientCommand) -> None:
        """
        This command will block until the specified command can run without
        conflicting with any of the currently running IMAP command tasks.

        It will update the set of running IMAP commands as it blocks.

        Keyword Arguments:
        imap_cmd: IMAPClientCommand --
        """
        start_time = time.monotonic()
        # Remove IMAP commands that have finished executing from the list of
        # executing tasks.
        #
        self._cleanup_executing_tasks()

        # And loop until the IMAP command that wants to execute does not
        # conflict with any of the currently executing IMAP commands.  (asyncio
        # sleeping between checks)
        #
        while self.would_conflict(imap_cmd):
            await asyncio.sleep(0.01)
            self._cleanup_executing_tasks()

        # Also if it has been more than 10 seconds since the last resync then
        # block until all executing tasks have finished. We need to make sure
        # that even if we are getting a non-stop stream of non-conflicting
        # commands we check the mailbox for updates.
        #
        duration = time.monotonic() - self.last_resync
        if duration >= 10:
            self.logger.debug(
                "mbox: '%s', IMAP Command %s: more than 10s since last resync, blocking",
                self.name,
                imap_cmd.qstr(),
            )
            while self.executing_tasks:
                await asyncio.sleep(0.1)
                self._cleanup_executing_tasks()

        duration = time.monotonic() - start_time
        if duration >= 0.1:
            self.logger.debug(
                "mbox: '%s', IMAP Command %s waited %.3fs before allowed to proceed",
                self.name,
                imap_cmd.qstr(),
                duration,
            )

    ####################################################################
    #
    def msg_set_to_msg_seq_set(
        self, msg_set: Optional[MsgSet], from_uids: bool = False
    ) -> Optional[Set[int]]:
        """
        Converts a MsgSet that may be a set of message sequence numbers or
        a set of message uid's in to a `set()` of message sequence numbers.
        """
        if msg_set is None:
            return None

        if from_uids:
            seq_max = self.uids[-1] if self.uids else 1
        else:
            seq_max = self.num_msgs

        msgs = sequence_set_to_list(msg_set, seq_max, uid_cmd=from_uids)

        # The msg_set is in UID's and we need to convert that to msg sequence
        # numbers. The list `self.uids` is this mapping. If a UID is NOT in
        # self.uids, we drop it from our sequence set.
        #
        if from_uids:
            # NOTE: IMAP Message Sequence numbers start at 1. Our array starts
            # at 0.
            #
            # XXX self.uids.index(uid) is a nasty search. Maybe we should keep
            #     a dict of uid's that may to imap message sequence numbers?
            #     Granted that is one more thing to keep track of.
            #     I guess we should consider timing this function.
            #     Luckily we will be doing this only once per imap commmand
            #
            msgs = [
                self.uids.index(uid) + 1 for uid in msgs if uid in self.uids
            ]
        return set(msgs)

    ####################################################################
    #
    def _maybe_extend_timeout(
        self, timeout_cm: Optional[asyncio.Timeout], extend: float = 10.0
    ):
        """
        If we have a timeout context manager and we are close to timing out,
        extend the timeout to now + extend seconds.

        If there currently is no timeout set, then set one for now + extend
        seconds.

        NOTE: So far we always yield multiple results within 1 second, so this
              seems reasonable.
        """
        if timeout_cm is None or timeout_cm.expired():
            return

        now = asyncio.get_running_loop().time()
        when = timeout_cm.when()
        when = when if when is not None else now
        if when - now < extend:
            timeout_cm.reschedule(when + extend)
            logger.info(
                "Mailbox: '%s': Extended timeout to %f",
                self.name,
                timeout_cm.when(),
            )

    ####################################################################
    #
    def _cleanup_executing_tasks(self) -> None:
        """
        Go through the `self.excuting_tasks` list and remove from it all
        IMAP commands that have `.completed` == True. Also mark a formerly
        enqueued task is done on the task_queue.
        """
        num_exec_tasks = len(self.executing_tasks)
        self.executing_tasks = [
            x for x in self.executing_tasks if not x.completed
        ]
        num_completed = len(self.executing_tasks) - num_exec_tasks
        for _ in range(num_completed):
            self.task_queue.task_done()

    ####################################################################
    #
    async def management_task(self):
        """
        This task will loop until it is canceled. It will pull tasks as
        many tasks from the task queue that do not conflict with each other
        (basically read only, or read mostly tasks, vs tasks that will change
        the state of the mailbox.)

        It will then perform an optional resync.

        Then it will signal all of those tasks it pulled that they may continue.

        Once they have finished the loop repeat.
        """
        # Opportunistically pack before we start processing IMAP Commands.
        #
        async with self.mailbox.lock_folder():
            await self._pack_if_necessary()

        # List of tasks currently acting on this mailbox.
        # (will only be one for conflicting commands)
        #
        self.executing_tasks = []
        while True:
            try:
                # Block until we have an IMAP Command that wants to run on this
                # mailbox
                #
                try:
                    async with asyncio.timeout(10):
                        imap_cmd = await self.task_queue.get()
                except asyncio.TimeoutError:
                    self._cleanup_executing_tasks()
                    if not self.executing_tasks:
                        async with self.mailbox.lock_folder():
                            changed = await self.check_new_msgs_and_flags()
                            if not changed:
                                # We will take the mailbox not having changed
                                # and ther being no executing commands as a
                                # good opportunity to conditionally pack it.
                                #
                                await self._pack_if_necessary()
                    continue

                # Compute the set() of imap message sequence numbers this
                # command is being applied to. This lets us check for conflicts
                # between different IMAP Commands that are allowed to run at
                # the same time if they do not operate on the same messages.
                #
                try:
                    imap_cmd.msg_set_as_set = self.msg_set_to_msg_seq_set(
                        imap_cmd.msg_set, imap_cmd.uid_command
                    )
                except Bad as e:
                    # XXX This is a hack for now. We are getting some FETCH
                    # commands where the msg seq being asked for is clearly a
                    # UID command, yet the `imap_cmd.uid_command` field appears
                    # to be Falsem, so quickly retry it and log the
                    # command. Maybe this is an error in the parser.
                    #
                    if not imap_cmd.uid_command:
                        logger.exception(
                            "Mailbox: '%s', converting msg set to set. "
                            "Re-attempging as a UID command: '%s': %s",
                            self.name,
                            str(imap_cmd),
                            e,
                        )
                        imap_cmd.msg_set_as_set = self.msg_set_to_msg_seq_set(
                            imap_cmd.msg_set, True
                        )
                    else:
                        raise

                # Block until the new IMAP command would not conflict with any
                # of the currently executing IMAP commands.
                #
                await self.command_can_proceed(imap_cmd)

                # If there are no tasks, do a resync. Also potentially pack the
                # folder (doing it while there are no commands running to
                # prevent any sort of sync between client and server errors.)
                #
                if not self.executing_tasks:
                    async with self.mailbox.lock_folder():
                        changed = await self.check_new_msgs_and_flags()

                    # Need to update this command's msg_set_as_set before we
                    # add it to the list of executing commands (the list is
                    # empty so we only need to update this one command)
                    #
                    if changed:
                        imap_cmd.msg_set_as_set = self.msg_set_to_msg_seq_set(
                            imap_cmd.msg_set, imap_cmd.uid_command
                        )

                self.executing_tasks.append(imap_cmd)
                imap_cmd.ready.set()

            except NoSuchMailboxError:
                self.logger.info(
                    "mbox: '%s', mailbox deleted exiting management task",
                    self.name,
                )
                self.expiry = 0.0
                return
            except RuntimeError as e:
                self.logger.exception(
                    "mbox: '%s', exception in management task: %s",
                    self.name,
                    e,
                )
                return
            except asyncio.CancelledError:
                self.expiry = 0.0
                return
            except Exception as e:
                # We ignore all other exceptions because otherwise the
                # management task would exit and no mbox commands would be
                # processed.
                #
                self.logger.exception(
                    "mbox: '%s', Management task got exception: %s, Ignoring!",
                    self.name,
                    e,
                )

    ####################################################################
    #
    def __str__(self):
        return (
            f"<Mailbox: '{self.name}', num clients: {len(self.clients)}, "
            f"num msgs: {self.num_msgs}>"
        )

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
    def _msg_sequences(self, msg_key: int) -> List[str]:
        """
        Returns a list of all the sequences that the msg key is in.
        """
        seqs = []
        for sequence in self.sequences.keys():
            if msg_key in self.sequences[sequence]:
                seqs.append(seq_to_flag(sequence))
        return seqs

    ####################################################################
    #
    async def _get_sequences_update_seen(
        self, recent_msg_keys: Optional[List[int]] = None
    ) -> Sequences:
        """
        Get the sequences from the MH folder.

        Update the `Seen` and `unseen` sequences. Basically `unseen` is used
        when messages are added to a folder. However, the IMAP protocol is all
        about `Seen` flags. Thus we need to make sure we properly update the
        `Seen` and `unseen` sequences so that they remain in sync. ie: all
        mesages NOT marked `unseen` are `Seen`.

        If the `recent_msg_keys` parameter is not empty then update the
        `recent` sequence with these message keys.

        Returns the sequences for this folder.

        Raises a MailboxInconsistency exception if we are unable to read the
        .mh_sequences file.
        """
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
            new_seen = set(self.msg_keys) - seq["unseen"]
            if new_seen != seq["Seen"]:
                seq["Seen"] = set(new_seen)
                modified = True
        else:
            # There are no unseen messages in the mailbox thus the Seen
            # sequence mirrors the set of all messages.
            #
            if seq["Seen"] != set(self.msg_keys):
                modified = True
                seq["Seen"] = set(self.msg_keys)

        if recent_msg_keys:
            modified = True
            if "Recent" in seq:
                seq["Recent"].update(recent_msg_keys)
            else:
                seq["Recent"] = set(recent_msg_keys)

        # A mailbox gets '\Marked' if it has any unseen messages or
        # '\Recent' messages.
        #
        marked = True if seq["unseen"] or seq["Recent"] else False
        self.marked(marked)

        if modified:
            await self.mailbox.aset_sequences(seq)
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
                # IF they are in IDLE, then we can send the notifications
                # immediately.
                #
                await c.client.push(*notifications)
            else:
                # Otherwise stick the notifications on a pending list and the
                # client module will handle sending these to the client when it
                # is allowed to.
                #
                c.pending_notifications.extend(notifications)

    ##################################################################
    #
    async def check_new_msgs_and_flags(
        self,
        dont_notify: Optional["Authenticated"] = None,
        optional: bool = True,
    ) -> bool:
        """
        This method checks the folder for new messages and updated
        sequences.  It is only called between by the mailbox management task
        before a set of commands are allowed to run against the mailbox.

        - dont_notify: Optional["Authentitcated"]: If we have FETCH, EXISTS,
                       and RECENT notifications for clients, this client is
                       *not* notified. This is typically because we are
                       executing a FETCH, STORE, or SEARCH command.

        NOTE: We only allow non-conflicting commands to run at the same time
              against the mailbox. Non-conflicting commands are supposed to not
              modify the sequences (thus FETCH BODY's are considered
              conflicting. FETCH BODY.PEEK are not)

        NOTE: At the end of this method call
              - self.msg_keys will be up to date
              - self.uids will be up to date
              - self.sequences will be up to date

        This way if some other process appends messages to this mailbox those
        commands running will not see these messages and proceed as if they did
        exist.
        """
        # Maybe we should lock the mailbox during this so that we get no
        # conflicts from possible COPY commands. COPY commands and inc's from
        # the mail system are the only time messages will be added to a mailbox
        # outside of this function.
        #
        # This may result in the mailbox being locked for extended periods of
        # time during huge resyncs. But, no other task is allowed to call this
        # function except the management task. So the only thing it would
        # conflict with is the destitnation of a COPY command.
        #
        start_time = time.monotonic()
        self.last_resync = time.time()

        # We set the expiry to None while we are resync'ing to make sure that
        # nothing comes along and arbitrarily kills this mailbox due to expiry
        # logic until our resync has finished.
        #
        expiry = self.expiry
        self.expiry = None
        try:
            # We do NOT resync mailboxes marked '\Noselect'. These mailboxes
            # essentially do not exist as far as any IMAP client can really
            # tell.
            #
            if r"\Noselect" in self.attributes:
                self.mtime = await self.get_actual_mtime(
                    self.server.mailbox, self.name
                )
                await self.commit_to_db()
                return False

            # Get the mtime of the folder at the start so when we need to check
            # to
            #
            start_mtime = await self.get_actual_mtime(
                self.server.mailbox, self.name
            )

            # If the mtime we got from the folder/.mh_sequences is older the
            # stored mtime for the mailbox then we can assume nothing has
            # touched it and return immediately.
            #
            # However if optional is False, then we will do our scan regardless
            # of the mtime.
            #
            if start_mtime <= self.mtime and self.optional_resync and optional:
                await self.commit_to_db()
                return False

            # We always reset `optional_resync` once we begin a non-optional
            # resync
            #
            self.optional_resync = True

            # The heart of the resync is to see if there are new messages in
            # the folder. If there are, then those messages are the ones we
            # care about.
            #
            # NOTE: This is all based on the logic that the only thing that
            #       will happen to a mailbox between checks is if an external
            #       services added messages to it.
            #
            #       The mailbox will not be re-packed, re-ordered, or have
            #       messages removed from it outside of the control of asimap.
            #
            #       What is more we only care about sequences that any new
            #       messages were added to.
            #
            msg_keys = await self.mailbox.akeys()

            # If the list of new_msg_keys matches the existing list of
            # message keys then there have been no changes to the folder
            # and this resync is done.
            #
            if msg_keys == self.msg_keys:
                self.mtime = start_mtime
                marked = (
                    True
                    if self.sequences["unseen"] or self.sequences["Recent"]
                    else False
                )
                self.marked(marked)
                await self.commit_to_db()
                return False

            # If the number of messages in the mailbox is less than the
            # last recorded number of messages then we treat this as a new
            # mailbox.
            #
            if len(msg_keys) < self.num_msgs:
                logger.warning(
                    "Mailbox: '%s' has shrunk, from %d messages to %d. "
                    "Treating it as a new mailbox.",
                    self.name,
                    self.num_msgs,
                    len(msg_keys),
                )
                self.msg_keys = []
                self.uids = []
                self.num_msgs = 0
                self.num_recent = 0
                self.sequences = defaultdict(set)
                self.mtime = start_mtime

            # If we reach here we know that we have new messages. Find out
            # the lowest numbered new message and consider that message and
            # everything after it a new message.
            #
            new_msg_keys = sorted(set(msg_keys) - set(self.msg_keys))
            num_new_msgs = len(new_msg_keys)

            self.msg_keys.extend(new_msg_keys)
            self.uids.extend(range(self.next_uid, self.next_uid + num_new_msgs))
            self.next_uid = self.uids[-1] + 1

            # Determine the sequences for the new messages so we know what
            # FETCH messages to send (and to upate our internal sequences
            # representations)
            #
            self.marked(True)
            for key in new_msg_keys:
                msg = await self.mailbox.aget_message(key)
                self.sequences["Recent"].add(key)
                # msg.add_sequence("Recent")
                msg_sequences = msg.get_sequences()
                msg_sequences.append("Recent")
                if "unseen" not in msg_sequences:
                    # If it is _not_ 'unseen', therefore it must be 'Seen'
                    #
                    if "Seen" not in msg_sequences:
                        # msg.add_sequence("Seen")
                        msg_sequences.append("Seen")
                else:
                    # If it is 'unseen' then it must _not_ be 'Seen'
                    #
                    if "Seen" in msg_sequences:
                        # msg.remove_sequence("Seen")
                        msg_sequences.remove("Seen")

                # If it is 'Seen' then must not be 'unseen'
                #
                if "Seen" in msg_sequences and "unseen" in msg_sequences:
                    # msg.remove_sequence("unseen")
                    msg_sequences.remove("unseen")

                # For all the sequences this message was in, add it to
                # those in our `self.sequences` defaultdict(set), and
                # remove it from sequences that it is not in.
                #
                for sequence in msg_sequences:
                    self.sequences[sequence].add(key)
                for sequence in self.sequences.keys():
                    if sequence not in msg_sequences:
                        self.sequences[sequence].discard(key)

            # Make the folder's .mh_sequences reflect our current state
            # of the universe.
            #
            await self.mailbox.aset_sequences(self.sequences)

            num_recent = len(self.sequences["Recent"])
            num_msgs = len(msg_keys)

            logger.info(
                "Mailbox: '%s', num msgs: %d, num new msgs: %d, "
                "first new msg: %d, last new msg: %d",
                self.name,
                self.num_msgs,
                len(new_msg_keys),
                new_msg_keys[0],
                new_msg_keys[-1],
            )

            # RECENT and EXISTS are sent as the rest of a SELECT or EXAMINE
            # request _AND_ if the size of the mailbox changes. These can be
            # sent to any client, idling, executing a command, or otherwise.
            #
            notifications = []
            notifications.append(f"* {num_msgs} EXISTS\r\n")
            notifications.append(f"* {num_recent} RECENT\r\n")
            for c in self.clients.values():
                await c.client.push(*notifications)

            self.num_msgs = num_msgs
            self.num_recent = num_recent

            # For all of our new messages we need to issue untagged FETCH FLAG
            # mesages to all of our active clients.
            #
            notifications = []
            for key in new_msg_keys:
                msg = await self.mailbox.aget_message(key)
                fetch, _ = self._generate_fetch_msg_for(key)
                notifications.append(fetch)

            await self._dispatch_or_pend_notifications(
                notifications, dont_notify=dont_notify
            )

            # Update counts and commit state of the mailbox to the db.
            #
            self.mtime = await Mailbox.get_actual_mtime(
                self.server.mailbox, self.name
            )
            await self.check_set_haschildren_attr()
            await self.commit_to_db()

            end_time = time.monotonic()
            duration = end_time - start_time
            if duration > 0.01:
                logger.debug(
                    "Finished. Mailbox '%s', duration: %.3fs, num messages: "
                    "%d, num recent: %d",
                    self.name,
                    duration,
                    self.num_msgs,
                    self.num_recent,
                )
            return True
        finally:
            self.expiry = expiry

    ####################################################################
    #
    def _generate_fetch_msg_for(
        self, msg_key: int, publish_uid: bool = False
    ) -> Tuple[str, str]:
        """
        Generate a `FETCH` message for sending to a client for the flags of
        the specified msg key.

        NOTE: We need to generate both UID and non-UID fetch messages because
        or caller may need both and I would rather not call this function
        twice.

        XXX It is a bit annoying that we are generating two responses even if
            publish_uid is not set. Should clean this up. Either return a
            better response or always return the uid version and not even
            bother with needing a `publish_uid` parameter.

        Keyword Arguments:
        msg_key: int  --
        uid_cmd: bool -- (default False)
        """
        flags = self._msg_sequences(msg_key)

        # for sequence in msg.get_sequences():
        #     flags.append(seq_to_flag(sequence))
        flags_str = " ".join(flags)
        msg_seq_number = self.msg_keys.index(msg_key) + 1

        uidstr = ""
        if publish_uid:
            try:
                uidstr = f" UID {self.uids[msg_seq_number-1]}"
            except IndexError:
                logger.error(
                    "Mailbox '%s': UID command but msg seq number: %d is not "
                    "inside list of UIDs, whose length is: %d",
                    self.name,
                    msg_seq_number,
                    len(self.uids),
                )
        fetch = f"* {msg_seq_number} FETCH (FLAGS ({flags_str}))\r\n"
        fetch_uid = (
            f"* {msg_seq_number} FETCH (FLAGS ({flags_str}){uidstr})\r\n"
        )
        return fetch, fetch_uid

    ##################################################################
    #
    async def _pack_if_necessary(self) -> bool:
        """
        We use the array of message keys from the folder to determine if it is
        time to pack the folder.

        The key is if there is more than a 20% difference between the number of
        messages in the folder and the highest number in the folder and the
        folder is larger than 20. This tells us it has a considerable number
        of gaps and we then call pack on the folder.

        This is expected to only be called when no imap tasks are running
        against this mailbox to prevent sync problems between server and
        client.
        """
        if self.num_msgs < self.folder_size_pack_limit:
            return False

        if self.num_msgs / self.msg_keys[-1] > self.folder_ratio_pack_limit:
            return False

        logger.info(
            "Packing mailbox '%s', num msgs: %d, max msg key: %d",
            self.name,
            self.num_msgs,
            self.msg_keys[-1],
        )

        # NOTE: Because we take care to write the sequences whenever we do
        #       commands on the mailbox that update the in-memory sequences,
        #       and this function can only be called when no command is running
        #       we expect the sequences on disk to be up to date with respect
        #       to self.sequences. It may also have new unseen messages and we
        #       wish to make sure we do not lose those. So we read the
        #       sequences back in after the pack.
        #
        await self.mailbox.aset_sequences(self.sequences)
        await self.mailbox.apack()
        self.msg_keys = await self.mailbox.akeys()
        self.sequences = await self.mailbox.aget_sequences()

        self.mtime = await Mailbox.get_actual_mtime(
            self.server.mailbox, self.name
        )
        await self.commit_to_db()
        return True

    ##################################################################
    #
    def get_uid_from_msg(
        self, msg_key: int
    ) -> Tuple[Optional[int], Optional[int]]:
        """
        Get the uid from the given message (where msg_key is the integer
        key into the folder.)

        We return the tuple of (uid_vv,uid)

        If the msg key is not in our list of msg keys, we return a uid of None
        indicating that this message is not one this mailbox knows about yet.

        Arguments:
        - `msg_key`: the message key in the folder we want the uid_vv/uid for.
        """
        try:
            idx = self.msg_keys.index(msg_key)
            return (self.uid_vv, self.uids[idx])
        except ValueError:
            return (self.uid_vv, None)

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
        async with self.db_lock:
            results = await self.server.db.fetchone(
                "select id, uid_vv,attributes,mtime,next_uid,num_msgs,"
                "num_recent,uids,msg_keys,last_resync,subscribed from mailboxes "
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

                # After we insert the record we pull it out again because we
                # need the mailbox id to relate the mailbox to its sequences.
                #
                results = await self.server.db.fetchone(
                    "SELECT id FROM mailboxes WHERE name=?", (self.name,)
                )
                self.id = results[0]

                # For every sequence we store it in the db also so we can later
                # on do smart diffs of sequence changes between mailbox
                # resyncs.
                #
                async with self.mailbox.lock_folder():
                    self.sequences = await self._get_sequences_update_seen()

                for name, values in self.sequences.items():
                    await self.server.db.execute(
                        "INSERT INTO sequences (id,name,mailbox_id,sequence)"
                        " VALUES (NULL,?,?,?)",
                        (
                            name,
                            self.id,
                            ",".join([str(x) for x in sorted(values)]),
                        ),
                    )
                await self.server.db.commit()
                return True

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
                msg_keys,
                self.last_resync,
                self.subscribed,
            ) = results
            self.subscribed = bool(self.subscribed)
            self.attributes = set(attributes.split(","))
            self.uids = [int(x) for x in uids.split(",")] if uids else []
            self.msg_keys = (
                [int(x) for x in msg_keys.split(",")] if msg_keys else []
            )
            # To handle the initial migration for when we start storing all the
            # message keys. `msg_keys` in the db will be an empty list, but
            # uids will not be empty. Based on the rule that messages are only
            # added to the mailbox by external systems, we can just read the
            # msg keys from the folder, truncated to the length of the list of
            # uid's.
            #
            if not self.msg_keys and self.uids:
                msg_keys = await self.mailbox.akeys()
                self.msg_keys = msg_keys[: len(self.uids)]
                self.num_msgs = len(self.msg_keys)

            # And fill in the sequences we find for this mailbox.
            #
            async for row in self.server.db.query(
                "SELECT name, sequence FROM sequences WHERE mailbox_id=?",
                (self.id,),
            ):
                name, sequence = row
                sequence = sequence.strip()
                if sequence:
                    self.sequences[name] = set(
                        int(x) for x in sequence.split(",")
                    )

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
            ",".join([str(x) for x in self.msg_keys]),
            self.last_resync,
            self.subscribed,
            self.id,
        )
        async with self.db_lock:
            await self.server.db.execute(
                "UPDATE mailboxes SET uid_vv=?, attributes=?, next_uid=?,"
                "mtime=?, num_msgs=?, num_recent=?, uids=?, msg_keys=?, "
                "last_resync=?, subscribed=? WHERE id=?",
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

            new_names = set(
                mbox for mbox in self.sequences.keys() if self.sequences[mbox]
            )

            names_to_delete = old_names - new_names
            if names_to_delete:
                # Need to build the a string of comma separated '?''s for the
                # sqlite binding to work with a variable number of names.
                #
                qms = ",".join(["?"] * len(names_to_delete))
                await self.server.db.execute(
                    "DELETE FROM sequences"
                    f"  WHERE mailbox_id=? AND name in ({qms})",
                    (self.id, *(list(names_to_delete))),
                )
                await self.server.db.commit()
            for name in new_names:
                sequence = ",".join(
                    str(x) for x in sorted(self.sequences[name])
                )
                await self.server.db.execute(
                    "INSERT INTO sequences(name,mailbox_id,sequence) "
                    "  VALUES (?,?,?)"
                    "  ON CONFLICT DO UPDATE SET"
                    "    sequence=?"
                    "  WHERE mailbox_id=? AND name=?",
                    (
                        name,  # values 0
                        self.id,  # values 1
                        sequence,  # values 2
                        sequence,  # set sequence=?
                        self.id,  # mailbox_id=?
                        name,  # name=?
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
        checking and updating this flag and parent folders during folder
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
    async def selected(self, client: "Authenticated") -> List[str]:
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
        if r"\Noselect" in self.attributes:
            raise No(f"You can not select the mailbox '{self.name}'")

        # A client has us selected. Turn of the expiry time.
        #
        self.expiry = None

        if client.name in self.clients and self.clients[client.name] != client:
            logger.warning(
                "Mailbox: '%s': client %s already in clients, but it is a "
                "different client: %s",
                self.name,
                client.name,
                self.clients[client.name],
            )

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
        push_data = []
        push_data.append(f"* {len(self.msg_keys)} EXISTS\r\n")
        push_data.append(f"* {len(self.sequences.get('Recent',[]))} RECENT\r\n")
        if self.sequences.get("unseen", []):
            # IMAP message sequence # of the first message that is unseen.
            # Convert from MH msg key to IMAP message sequence number
            first_unseen = sorted(self.sequences["unseen"])[0]
            try:
                first_unseen = self.msg_keys.index(first_unseen) + 1
                push_data.append(f"* OK [UNSEEN {first_unseen}]\r\n")
            except ValueError as exc:
                logger.error(
                    "Mailbox '%s': '%s', first unseen msg key: %d, "
                    "msg keys: %s, unseen: %s",
                    self.name,
                    exc,
                    first_unseen,
                    self.msg_keys,
                    self.sequences["unseen"],
                )
        push_data.append(f"* OK [UIDVALIDITY {self.uid_vv}]\r\n")
        push_data.append(f"* OK [UIDNEXT {self.next_uid}]\r\n")

        # Each sequence is a valid flag.. we send back to the client all
        # of the system flags and any other sequences that are defined on
        # this mailbox.
        #
        flags = list(SYSTEM_FLAGS)
        for k in list(self.sequences.keys()):
            if self.sequences[k] and k not in SYSTEM_FLAG_MAP:
                flags.append(k)
        push_data.append(f"* FLAGS ({' '.join(flags)})\r\n")
        push_data.append(
            f"* OK [PERMANENTFLAGS ({' '.join(PERMANENT_FLAGS)})]\r\n"
        )
        return push_data

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

        Arguments:
        - `message`: The email.message being appended to this mailbox
        - `flags`: A list of flags to set on this message
        - `date_time`: The internal date on this message
        """
        # Whatever sequences the message we are passed has, clear them.
        # and then add sequences based on the flags passed in.
        #
        msg.set_sequences([])
        flags = [] if flags is None else flags

        msg_key = await self.mailbox.aadd(msg)

        # Update the message and internal sequences.
        #
        self.sequences["Recent"].add(msg_key)
        # msg.add_sequence("Recent")
        for flag in flags:
            if flag in REVERSE_SYSTEM_FLAG_MAP:
                flag = REVERSE_SYSTEM_FLAG_MAP[flag]
            # msg.add_sequence(flag)
            self.sequences[flag].add(msg_key)

        # Keep the .mh_sequences up to date.
        #
        async with self.mailbox.lock_folder():
            await self.mailbox.aset_sequences(self.sequences)

        # if a date_time was supplied then set the mtime on the file to
        # that. We use mtime as our 'internal date' on messages.
        #
        if date_time:
            mtime = date_time.timestamp()
            await utime(mbox_msg_path(self.mailbox, msg_key), (mtime, mtime))

        # We expect it to issue EXISTS and RECENT since there is a new
        # message. We also want it to send a 'FETCH FLAGS' for every new
        # message.
        #
        async with self.mailbox.lock_folder():
            await self.check_new_msgs_and_flags(optional=False)

        uid_vv, uid = self.get_uid_from_msg(msg_key)
        if uid is None:
            raise Bad(
                f"Mailbox: '{self.name}', unable to fetch UID for "
                f"message {msg_key}"
            )
        logger.debug(
            # "Mailbox: '%s', append message: %d, uid: %d, sequences: %s",
            "Mailbox: '%s', append message: %d, uid: %d",
            self.name,
            msg_key,
            uid,
            # ", ".join(msg.get_sequences()),
        )
        return uid

    ##################################################################
    #
    async def expunge(self):
        """
        Perform an expunge. All messages in the 'Deleted' sequence are removed.

        We will send untagged EXPUNGE messages to all clients on this mbox that
        are idling.

        For clients that have this mailbox selected but are NOT idling, we will
        put the EXPUNGE messages on the notifications list for delivery to
        those clients when possible.
        """
        # If there are no messages in the 'Deleted' sequence then we have
        # nothing to do.
        #
        if not self.sequences["Deleted"]:
            return

        msg_keys_to_delete = self.sequences["Deleted"]

        # We go through the to be deleted messages in reverse order so that
        # the expunges are "EXPUNGE <n>" "EXPUNGE <n-1>" etc. This is
        # mostly a nicety making the expunge messages a bit easier to read.
        #
        to_delete = sorted(msg_keys_to_delete, reverse=True)
        logger.debug(
            "Mailbox: '%s', msg keys to delete: %s", self.name, to_delete
        )

        for msg_key in to_delete:
            # Remove the message from the folder.. and also remove it from our
            # uids to message index mapping. NOTE: To convert which to the IMAP
            # message sequence order we must increment it by one (because they
            # are 1-based)
            #
            # NOTE: num_recent and num_msgs will be updated on the next
            #       resync. Since the expunge must operate alone that means a
            #       resync will happen before the next IMAP command begins
            #       executing.
            #
            if msg_key not in self.msg_keys:
                logger.error(
                    "Mailbox: '%s': msg key %d not in msg_keys: %s",
                    self.name,
                    msg_key,
                    self.msg_keys,
                )
                continue
            which = self.msg_keys.index(msg_key)
            uid = self.uids[which]
            self.msg_keys.remove(msg_key)
            self.uids.remove(uid)
            self.num_msgs -= 1
            await self.mailbox.aremove(msg_key)
            expunge_msg = f"* {which+1} EXPUNGE\r\n"
            await self._dispatch_or_pend_notifications(expunge_msg)

        # Remove all deleted msg keys from all sequences
        #
        for seq in self.sequences.keys():
            for msg_key in to_delete:
                self.sequences[seq].discard(msg_key)
        self.num_recent = len(self.sequences["Recent"])

        self.optional_resync = False

    ##################################################################
    #
    async def search(
        self,
        search: IMAPSearch,
        uid_cmd: bool = False,
        timeout_cm: Optional[asyncio.Timeout] = None,
    ) -> List[int]:
        """
        Take the given IMAP search object and apply it to all of the messages
        in the mailbox.

        Form a list (by message index) of the messages that match and return
        that list to our caller.

        NOTE: We are using the in-memory self.msg_keys for what is in the
              mailbox. Since the only thing an exteranl system will do is add
              messages to the end of the mailbox if any messages have been
              added since the last resync and while this command is running
              they simply will not be seen by this search. They do not yet
              "exist" in the mailbox as far as we are concerned.

        Arguments:
        - `search`: An IMAPSearch object instance
        - `uid_cmd`: whether or not this is a UID command.
        - `timeout_cm`: Timeout context manager. If passed in, when looping
                        over messages, if we are approaching the timeout
                        deadline, reschedule it.
        """
        if not self.num_msgs:
            return []

        results: List[int] = []
        seq_max = self.num_msgs
        uid_max = self.uids[-1]

        # Go through the messages one by one and pass them to the search
        # object to see if they are or are not in the result set..
        #
        logger.debug(
            "Mailbox: '%s', applying search to messages: %s",
            self.name,
            str(search),
        )

        for idx, msg_key in enumerate(self.msg_keys):
            # IMAP messages are numbered starting from 1.
            #
            msg_seq_num = idx + 1
            ctx = SearchContext(
                self, msg_key, msg_seq_num, seq_max, uid_max, self.sequences
            )
            if await search.match(ctx):
                # The UID SEARCH command returns uid's of messages
                #
                if uid_cmd:
                    uid = await ctx.uid()
                    assert uid
                    results.append(uid)
                else:
                    results.append(msg_seq_num)

            await asyncio.sleep(0)
            self._maybe_extend_timeout(timeout_cm)

        return results

    #########################################################################
    #
    async def fetch(
        self,
        msg_set: List[int],
        fetch_ops: List[FetchAtt],
        uid_cmd: bool = False,
        timeout_cm: Optional[asyncio.Timeout] = None,
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
        - `msg_set`: The list of IMAP message sequence numbers to apply fetch ops on
        - `fetch_ops`: The things to fetch for the messags indiated in
          msg_set
        - `uid_cmd`: whether or not this is a UID command.
        """

        if not self.msg_keys:
            # If there are no messages but they asked for some messages
            # (that are not there), return NO.. can not fetch that data.
            if msg_set and not uid_cmd:
                raise No("Mailbox empty.")
            return

        start_time = time.time()
        num_results = 0
        # As we do our fetch's some operations will mark a message as
        # seen. Also getting the flags for a message will also remove the
        # `\Recent` flag. We apply all these changes at the end of the fetch
        # run and just record which messages this change applied to while the
        # fetch is running.
        #
        no_longer_unseen_msgs: Set[int] = set()
        no_longer_recent_msgs: Set[int] = set()

        # msgs = await self.mailbox.akeys()
        # seqs = await self.mailbox.aget_sequences()
        fetch_started = time.time()
        fetch_finished_time = None
        fetch_yield_times = []
        yield_times = []

        try:
            seq_max = self.num_msgs
            uid_max = self.uids[-1] if self.uids else 1

            # Go through each message and apply the fetch_ops.fetch() to it
            # building up a set of data to respond to the client with. Remember
            # IMAP message sequence number `1` refers to the first message in
            # the folder, ie: msgs[0].
            #
            fetch_started = time.time()
            for msg_seq_num in msg_set:
                single_fetch_started = time.time()
                try:
                    msg_key = self.msg_keys[msg_seq_num - 1]
                except IndexError:
                    # Every key in msg_idx should be in the folder. If it is
                    # not then something is off between our state and the
                    # folder's state.
                    #
                    log_msg = (
                        f"fetch: Attempted to look up msg key "
                        f"{msg_seq_num - 1}, but msgs is only of length {self.num_msgs}"
                    )
                    logger.warning(log_msg)
                    self.optional_resync = False
                    raise MailboxInconsistency(log_msg)

                ctx = SearchContext(
                    self, msg_key, msg_seq_num, seq_max, uid_max, self.sequences
                )
                fetched_flags = False
                fetched_body_seen = False
                iter_results = []

                # If this is a uid_cmd add the UID to the fetch atts we need to
                # return. ie: a fetch response that would have been:
                # * 23 FETCH (FLAGS (\Seen))
                # is now going to be:
                # * 23 FETCH (FLAGS (\Seen) UID 4827313)
                #
                fo = (
                    fetch_ops
                    if not uid_cmd
                    else fetch_ops + [FetchAtt(FetchOp.UID)]
                )
                for elt in fo:
                    iter_results.append(await elt.fetch(ctx))
                    # If one of the FETCH ops gets the FLAGS we want to
                    # know and likewise if one of the FETCH ops gets the
                    # BODY (but NOT BODY.PEEK) we want to know. Both of
                    # these operations can potentially change the flags of
                    # the message.
                    #
                    if elt.attribute == "body" and elt.peek is False:
                        fetched_body_seen = True
                    if elt.attribute == "flags":
                        fetched_flags = True

                # If we did a FETCH FLAGS and the message was in the
                # 'Recent' sequence then remove it from the 'Recent'
                # sequence. Only one client gets to actually see that a
                # message is 'Recent.'
                #
                if fetched_flags:
                    if msg_key in self.sequences["Recent"]:
                        no_longer_recent_msgs.add(msg_key)

                # If we dif a FETCH BODY (but NOT a BODY.PEEK) then the
                # message is removed from the 'unseen' sequence (if it was
                # in it) and added to the 'Seen' sequence (if it was not in
                # it.)
                #
                if fetched_body_seen:
                    if msg_key in self.sequences["unseen"]:
                        no_longer_unseen_msgs.add(msg_key)

                fetch_yield_times.append(time.time() - single_fetch_started)
                num_results += 1
                yield_start = time.time()
                yield (msg_seq_num, iter_results)
                yield_times.append(time.time() - yield_start)

                self._maybe_extend_timeout(timeout_cm)

            fetch_finished_time = time.time()

            # A FETCH BODY with no peek means we have to send FETCH messages to
            # all other clients (that do not have dont_notify set)
            #
            # NOTE: The mailbox management task should have made sure no other
            #       imap command was executing on the message at least for the
            #       unseen sequence.
            #
            if no_longer_unseen_msgs or no_longer_recent_msgs:
                notifies_for = no_longer_unseen_msgs | no_longer_recent_msgs
                async with self.mh_sequences_lock, self.mailbox.lock_folder():
                    seqs = await self.mailbox.aget_sequences()
                    for msg_key in no_longer_recent_msgs:
                        self.sequences["Recent"].discard(msg_key)
                        seqs["Recent"].discard(msg_key)
                    for msg_key in no_longer_unseen_msgs:
                        self.sequences["unseen"].discard(msg_key)
                        seqs["unseen"].discard(msg_key)
                        if msg_key not in self.sequences["Seen"]:
                            self.sequences["Seen"].add(msg_key)
                            seqs["Seen"].add(msg_key)

                    await self.mailbox.aset_sequences(seqs)

                # XXX Move this into a helper function?
                #
                # Send a FETCH to all other clients for the flags of the
                # messages that have had their flags changed.
                #
                notifies: List[str] = []
                for msg_key in notifies_for:
                    flags = []
                    for sequence in self.sequences.keys():
                        if msg_key in self.sequences[sequence]:
                            flags.append(seq_to_flag(sequence))
                    flags_str = " ".join(flags)
                    msg_seq_number = self.msg_keys.index(msg_key) + 1
                    notifies.append(
                        f"* {msg_seq_number} FETCH "
                        f"(FLAGS ({flags_str}))\r\n"
                    )
                await self._dispatch_or_pend_notifications(notifies)

        finally:
            now = time.time()
            total_time = now - start_time
            if total_time > 1.0:
                fetch_time = (
                    fetch_finished_time - fetch_started
                    if fetch_finished_time is not None
                    else 9999999.9
                )
                if len(yield_times) > 1:
                    # Only bother to calculate more statistics if there was
                    # more than one message fetched.
                    #
                    mean_fetch_yield_time = (
                        fmean(fetch_yield_times) if fetch_yield_times else 0.0
                    )
                    median_yield_time = (
                        median(fetch_yield_times) if fetch_yield_times else 0.0
                    )
                    stdev_yield_time = (
                        stdev(fetch_yield_times, mean_fetch_yield_time)
                        if len(fetch_yield_times) > 2
                        else 0.0
                    )

                    logger.debug(
                        "FETCH finished, mailbox: '%s', msg_set: %s, num "
                        "results: %d, total duration: %.3fs, fetch duration: "
                        "%.3fs, mean time per fetch: %.3fs, median: %.3fs, "
                        "stdev: %.3fs",
                        self.name,
                        compact_sequence(msg_set),
                        num_results,
                        total_time,
                        fetch_time,
                        mean_fetch_yield_time,
                        median_yield_time,
                        stdev_yield_time,
                    )
                else:
                    logger.debug(
                        "FETCH finished, mailbox: '%s', msg_set: %s, num "
                        "results: %d, total duration: %.3fs, fetch duration: "
                        "%.3fs",
                        self.name,
                        compact_sequence(msg_set),
                        num_results,
                        total_time,
                        fetch_time,
                    )

    ####################################################################
    #
    def _help_add_flag(self, key: int, flag: str):
        """
        Helper function for the logic to add a message to a sequence. Updating
        both the sequences associated with the MHMessage and the sequences dict.
        """
        # msg.add_sequence(flag)
        self.sequences[flag].add(key)

        # Make sure that the Seen and unseen sequences are updated properly.
        #
        match flag:
            case "Seen":
                # msg.remove_sequence("unseen")
                self.sequences["unseen"].discard(key)
            case "unseen":
                # msg.remove_sequence("Seen")
                self.sequences["Seen"].discard(key)

    ####################################################################
    #
    def _help_remove_flag(self, key: int, flag: str):
        """
        Helper function for the logic to remove a message to a
        sequence. Updating both the sequences associated with the MHMessage and
        the sequences dict.
        """
        # msg.remove_sequence(flag)
        self.sequences[flag].discard(key)

        # Make sure that the Seen and unseen sequences are updated properly.
        #
        match flag:
            case "Seen":
                # msg.add_sequence("unseen")
                self.sequences["unseen"].add(key)
            case "unseen":
                # msg.add_sequence("Seen")
                self.sequences["Seen"].add(key)

    ####################################################################
    #
    def _help_replace_flags(self, key: int, flags: List[str]):
        r"""
        Replace the flags on the message.
        The flag `\Recent` if present is not affected.
        The flag `unseen` if present is not affected unless `\Seen` is in flags.
        """
        cur_msg_seqs = set(self._msg_sequences(key))
        new_msg_seqs = set(flags)
        if "Seen" not in new_msg_seqs:
            new_msg_seqs.add("unseen")
        if "Recent" in cur_msg_seqs:
            new_msg_seqs.add("Recent")

        to_remove = cur_msg_seqs - new_msg_seqs

        for seq in new_msg_seqs:
            self.sequences[seq].add(key)
        for seq in to_remove:
            self.sequences[seq].discard(key)

    ##################################################################
    #
    async def store(
        self,
        msg_set: List[int],
        action: StoreAction,
        flags: List[str],
        uid_cmd: bool = False,
        dont_notify: Optional["Authenticated"] = None,
    ) -> List[str]:
        r"""
        Update the flags (sequences) of the messages in msg_set.

        Arguments:
        - `msg_set`: The set of messages to modify the flags on as
                     IMAP message sequence numbers
        - `action`: one of REMOVE_FLAGS, ADD_FLAGS, or REPLACE_FLAGS
        - `flags`: The flags to add/remove/replace
        - `uid_cmd`: Used to determine if this is a uid command or not

        Returns the list of `FETCH *` mssages generated by this store.
        """

        if r"\Recent" in flags:
            raise No(r"You can not add or remove the '\Recent' flag")

        if action not in StoreAction:
            raise Bad(f"'{action}' is an invalid STORE action")

        # Build a set of msg keys that are just the messages we want to
        # operate on.
        #
        msg_keys = [self.msg_keys[x - 1] for x in msg_set]

        # Convert the flags to MH sequence names..
        #
        flags = [flag_to_seq(x) for x in flags]
        store_start = time.monotonic()

        notifications: List[str] = []
        response: List[str] = []
        for key in msg_keys:
            async with self.mh_sequences_lock:
                match action:
                    case StoreAction.ADD_FLAGS | StoreAction.REMOVE_FLAGS:
                        for flag in flags:
                            match action:
                                case StoreAction.ADD_FLAGS:
                                    self._help_add_flag(key, flag)
                                case StoreAction.REMOVE_FLAGS:
                                    self._help_remove_flag(key, flag)
                    case StoreAction.REPLACE_FLAGS:
                        self._help_replace_flags(key, flags)

            fetch, fetch_uid = self._generate_fetch_msg_for(
                key, publish_uid=uid_cmd
            )
            notifications.append(fetch)
            if uid_cmd:
                response.append(fetch_uid)
            else:
                response.append(fetch)

        async with self.mailbox.lock_folder():
            # XXX hm.. we should do what append does.. get a local copy of the
            #     sequences under lock folder, and update that in parallel with
            #     the above code.
            #
            await self.mailbox.aset_sequences(copy(self.sequences))

        await self._dispatch_or_pend_notifications(
            notifications, dont_notify=dont_notify
        )
        duration = time.monotonic() - store_start
        if duration > 0.5:
            self.logger.debug(
                "mbox: '%s', completed, took %.3f seconds",
                self.name,
                duration,
            )
        return response

    ##################################################################
    #
    async def copy(
        self,
        msg_set: MsgSet,
        dst_mbox: "Mailbox",
        uid_command: bool = False,
        imap_cmd: Optional[IMAPClientCommand] = None,
    ):
        r"""
        Copy the messages in msg_set to the destination mailbox.  Flags
        (sequences), and internal date are preserved.  Messages get the
        '\Recent' flag in the new mailbox.

        Arguments:
        - `msg_set`: Set of messages to copy.
        - `dst_mbox`: mailbox instance messages are being copied to
        - `uid_command`: True if this is for a UID SEARCH command, which means
          we have to return not message sequence numbers but message UID's.

        - `imap_cmd`: The IMAP Command for this operation. If provided it lets
          us signal the management task for this COPY when it is done with the
          src folder. Also it lets us access the IMAP Command's timeout context
          manager so we can extend the timeout for this command as it copies
          messages and waits for access to the destination mailbox.

        The messages are copied into temporary file storage, and then copied in
        to the destination folder. This lets us copy very large numbers of
        messages at once, albeit more slowly then only reading and writing
        once.
        """
        timeout_cm = imap_cmd.timeout_cm if imap_cmd else None
        copy_msgs: List[Tuple[str, List[str], float]] = []
        start_time = time.monotonic()

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            self.logger.debug(
                "mbox: '%s', TemporaryDirectory create: %.3f",
                self.name,
                (time.monotonic() - start_time),
            )
            try:
                max_msg_key = self.msg_keys[-1]
                uid_vv, uid_max = self.get_uid_from_msg(max_msg_key)
                if uid_vv is None or uid_vv != self.uid_vv or uid_max is None:
                    raise MailboxInconsistency(
                        f"Mailbox '{self.name}': uid_vv: {self.uid_vv}, msg "
                        f"key: {max_msg_key}, uid_vv:uid: {uid_vv}:{uid_vv}"
                    )

                seq_max = len(self.msg_keys)

                if uid_command:
                    # If we are doing a 'UID COPY' command we need to use the
                    # max uid for the sequence max.
                    #
                    uid_list = sequence_set_to_list(
                        msg_set, uid_max, uid_command
                    )

                    # We want to convert this list of UID's in to message
                    # indices So for every uid we we got out of the msg_set we
                    # look up its index in self.uids and from that construct
                    # the msg_idxs list. Missing UID's are fine. They just do
                    # not get added to the list.
                    #
                    msg_idxs = []
                    for uid in uid_list:
                        if uid in self.uids:
                            msg_idx = self.uids.index(uid) + 1
                            msg_idxs.append(msg_idx)
                else:
                    msg_idxs = sequence_set_to_list(msg_set, seq_max)

                src_uids = []
                # NOTE: msg_idxs are IMAP message sequence numbers.
                #
                msg_copy_start = time.monotonic()
                for idx in msg_idxs:
                    # To convert from an IMAP message sequence number to a
                    # MHMailbox msg key we subtract one and look it up in or
                    # list of msg keys. This is because IMAP message sequence
                    # numbers start at 1.
                    #
                    msg_key = self.msg_keys[idx - 1]

                    # Copy the messages from the src mbox to our temporary
                    # directory.
                    #
                    # Record the mtime and sequences for each message so that
                    # we can preserve them when we add them to the dst mbox.
                    # (mtime is used for `internal-date`)
                    #
                    mtime = await aiofiles.os.path.getmtime(
                        mbox_msg_path(self.mailbox, msg_key)
                    )
                    msg = await self.mailbox.aget_message(msg_key)
                    self._maybe_extend_timeout(timeout_cm)
                    msg_path = os.path.join(tmp_dir, str(msg_key))
                    msg_seqs = self._msg_sequences(msg_key)
                    copy_msgs.append((msg_path, msg_seqs, mtime))
                    await awrite_message(msg, msg_path)
                    uid_vv, uid = self.get_uid_from_msg(msg_key)
                    src_uids.append(uid)
            finally:
                # The part of the IMAP Command that has any relation to the src
                # mbox is now over. This tells the management task that this
                # command is done with accessing the mbox.
                #
                if imap_cmd:
                    imap_cmd.completed = True

            if imap_cmd:
                self.logger.debug(
                    "mbox: '%s', IMAP Command '%s' read messages took %.3fs",
                    self.name,
                    imap_cmd.qstr(),
                    time.monotonic() - msg_copy_start,
                )
            # We have now read all the messages we are copying. Write them to
            # the dest folder. Sequences are preserved since we are reading and
            # writing MHMessages (furthermore we added all the messages to the
            # sequence 'Recent' after we read it in.)
            #
            # We coordinate with the destination mailbox's management task by
            # creating a phony IMAPClientCommand that is an APPEND and wait for
            # the destination mailbox to tell us when we have exclusive access
            # to this mailbox.
            #
            append_imap_cmd = IMAPClientCommand("A001 APPEND")
            append_imap_cmd.command = IMAPCommand.APPEND

            if dst_mbox.deleted:
                raise Bad(f"'{dst_mbox.name}' has been deleted")

            try:
                wait_start = time.monotonic()
                self._maybe_extend_timeout(timeout_cm, extend=30.0)
                async with append_imap_cmd.ready_and_okay(dst_mbox):
                    wait_duration = time.monotonic() - wait_start
                    if imap_cmd:
                        self.logger.debug(
                            "mbox: '%s', %s: Took %.3fs before `write` part of the copy "
                            "command got permission to run on mailbox '%s'",
                            self.name,
                            imap_cmd.qstr(),
                            wait_duration,
                            dst_mbox.name,
                        )
                    dst_uids = []
                    dst_msg_keys = []
                    msg_copy_time_start = time.monotonic()
                    for msg_path, sequences, mtime in copy_msgs:
                        msg = await aread_message(msg_path)
                        msg.set_sequences(sequences)
                        msg_key = await dst_mbox.mailbox.aadd(msg)
                        dst_msg_keys.append(msg_key)
                        await utime(
                            mbox_msg_path(dst_mbox.mailbox, msg_key),
                            (mtime, mtime),
                        )
                        self._maybe_extend_timeout(timeout_cm)

                    msg_copy_duration = time.monotonic() - msg_copy_time_start
                    if imap_cmd:
                        self.logger.debug(
                            "mbox: '%s', %s: Took %.3fs to write %d messages to mailbox '%s'",
                            self.name,
                            imap_cmd.qstr(),
                            msg_copy_duration,
                            len(copy_msgs),
                            dst_mbox.name,
                        )

                    # We are going to do a resync so we need to now wait for no
                    # other tasks to be executing on this folder.
                    #
                    # NOTE: This dictates that APPEND is a conflicting command
                    #       and no other command can run while it is running.
                    #       This is because it needs to do the resync to get
                    #       the UID's of the new messages.
                    #
                    #       Since APPEND is a conflicting command this loop
                    #       should complete immediately. There should be only
                    #       one running command: This APPEND.
                    #
                    wait_start = time.monotonic()
                    while len(dst_mbox.executing_tasks) > 1:
                        await asyncio.sleep(0)
                        self._cleanup_executing_tasks()
                        self._maybe_extend_timeout(timeout_cm)
                    duration = time.monotonic() - wait_start
                    if duration > 0.1:
                        imap_cmd_str = imap_cmd.qstr() if imap_cmd else "none"
                        self.logger.debug(
                            "mbox: '%s', IMAP Command '%s': On mailbox '%s', waited for "
                            "%.3fs before we could begin resync",
                            self.name,
                            imap_cmd_str,
                            dst_mbox.name,
                            duration,
                        )

                    # Done copying messages to dest folder.. Resync to give all
                    # the messages proper uids for their new mailbox, update
                    # mailbox sequences, etc.
                    #
                    async with self.mailbox.lock_folder():
                        await dst_mbox.check_new_msgs_and_flags(optional=False)

                    # Now get the uid's for all the newly copied messages.
                    # NOTE: Since we added the messages in the same order
                    # they were copied we know that our src_uids and
                    # dst_uids refer to the correct messages.
                    #
                    for k in dst_msg_keys:
                        uid_vv, uid = dst_mbox.get_uid_from_msg(k)
                        dst_uids.append(uid)
            finally:
                append_imap_cmd.completed = True
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
    async def create(cls, name: str, server: "IMAPUserServer") -> None:
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
                async with mbox.mailbox.lock_folder():
                    await mbox.check_new_msgs_and_flags(optional=False)
                mbox.mgmt_task = asyncio.create_task(
                    mbox.management_task(), name=f"mbox '{mbox.name}' mgmt task"
                )
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
            mbox = await server.get_mailbox(mbox_name, expiry=0)
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
            raise InvalidMailbox(f"The mailbox '{name}' is still subscribed")

        # When deleting a mailbox every message in that mailbox will be
        # deleted.
        #
        await mbox.mailbox.aclear()
        mbox.num_msgs = 0
        mbox.num_recent = 0
        mbox.uids = []
        mbox.sequences = defaultdict(set)

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
            mbox.deleted = False
            mbox.attributes.add(r"\Noselect")
            mbox.uid_vv = await server.get_next_uid_vv()
            await mbox.commit_to_db()
        else:
            # We have no inferior mailboxes. This mailbox is gone. If
            # it is active we remove it from the list of active
            # mailboxes and if it has any clients that have it selected
            # they are moved back to the unauthenticated state.
            #
            async with server.active_mailboxes_lock:
                if name in server.active_mailboxes:
                    del server.active_mailboxes[name]

            # Set the mbox deleted flag to true. Cancel the management
            # task. Go through the task queue and signal all waiting
            # commands to proceed. They will check the mbox.deleted flag
            # and exit immediately.
            #
            await mbox.shutdown(commit_db=False)

            # Delete all traces of the mailbox from our db.
            #
            async with mbox.db_lock:
                await server.db.execute(
                    "DELETE FROM mailboxes WHERE id = ?", (mbox.id,)
                )
                await server.db.execute(
                    "DELETE FROM sequences WHERE mailbox_id = ?", (mbox.id,)
                )
            await server.db.commit()

            do_delete = True

        # if this mailbox was the child of another mailbox than we may need to
        # update that mailbox's 'has children' attributes.
        #
        parent_name = os.path.dirname(name)
        if parent_name:
            parent_mbox = await server.get_mailbox(parent_name)
            await parent_mbox.check_set_haschildren_attr()
            await parent_mbox.commit_to_db()

        # await mbox.shutdown(commit_db=False)

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
        # mbox_match = os.path.join(ref_mbox_name, mbox_match)
        mbox_match = ref_mbox_name + mbox_match

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
        async with srvr.active_mailboxes_lock:
            await srvr.db.execute(
                "UPDATE mailboxes SET name=? WHERE id=?",
                (mbox_new_name, old_id),
            )

            mb = srvr.active_mailboxes[mbox_old_name]
            del srvr.active_mailboxes[mbox_old_name]
            mb.name = mbox_new_name
            mb.mailbox = srvr.mailbox.get_folder(mbox_new_name)
            srvr.active_mailboxes[mbox_new_name] = mb

    # Make a sym link to where the new mbox is going to be. This way as we move
    # any subordinate folders if they get activity before we have finished the
    # entire move they will not just fail. When done we need to remove the
    # symlink and rename the dir from the old name to the new name.
    #
    old_name = mbox.name
    old_dir = mbox_msg_path(srvr.mailbox, old_name)
    new_dir = mbox_msg_path(srvr.mailbox, new_name)

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
            await _do_rename_folder(old_mbox, old_id, new_mbox_name)
        else:
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
        await m.check_set_haschildren_attr()
        await m.commit_to_db()

    # See if the mailbox under its new name has a parent and if it does update
    # that parent's children flags.
    #
    new_p_name = os.path.dirname(new_name)
    if new_p_name != "":
        m = await srvr.get_mailbox(new_p_name, expiry=0)
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
    uids = []

    for key in await mbox.mailbox.akeys():
        try:
            msg = await mbox.mailbox.aget_message(key)
        except KeyError:
            continue

        # Replace the asimap uid since this is a new folder.
        #
        uids.append(new_mbox.next_uid)
        uid = f"{new_mbox.uid_vv:010d}.{new_mbox.next_uid:010d}"
        new_mbox.next_uid += 1
        del msg["X-asimapd-uid"]
        msg["X-asimapd-uid"] = uid
        await new_mbox.mailbox.aadd(msg)
        try:
            await mbox.mailbox.aremove(key)
        except KeyError:
            pass

    new_mbox.uids = uids
    new_mbox.sequences = await new_mbox.mailbox.aget_sequences()
    new_mbox.msg_keys = await new_mbox.mailbox.akeys()
    new_mbox.optional_resync = False
    await new_mbox.commit_to_db()

    mbox.optional_resync = False
    mbox.sequences = await mbox.mailbox.aget_sequences()

    # We need to send EXPUNGES to all the other clients
    #
    notifications = []
    for msg_seq_num in range(len(mbox.msg_keys), 0, -1):
        notifications.append(f"* {msg_seq_num} EXPUNGE\r\n")
    await mbox._dispatch_or_pend_notifications(notifications)
    mbox.msg_keys = []
    mbox.uids = []
    await mbox.commit_to_db()
