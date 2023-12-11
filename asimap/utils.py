#!/usr/bin/env python
#
# File: $Id$
#
"""
This module contains utility functions that do not properly belong to any
class or module. This started with the utilities to pass an fd betwene
processes. If we build a decently sized set of messaging routines many of these
may move over in to a module dedicated for that.
"""
# system imports
#
import asyncio
import atexit
import email.utils
import logging
import logging.handlers
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from queue import SimpleQueue
from typing import TYPE_CHECKING, List, Optional, Set, Tuple, TypeAlias, Union

# 3rd party module imports
#
from aiofiles.ospath import wrap as aiofiles_wrap
from async_timeout import timeout

# Project imports
#
from .exceptions import Bad

if TYPE_CHECKING:
    from _typeshed import StrPath

MsgSet: TypeAlias = Union[List | Set | Tuple]

# RE used to suss out the digits of the uid_vv/uid header in an email
# message
#
UID_RE = re.compile(r"(\d+)\s*\.\s*(\d+)")

# The MHmessage header that is used for holding a messages uid.
#
UID_HDR = "X-asimapd-uid"

####################################################################
#
# Provide os.utime as an asyncio function via aiosfiles `wrap` async decorator
#
utime = aiofiles_wrap(os.utime)


##################################################################
##################################################################
#
class UpgradeableReadWriteLock:
    """
    A lock object that allows many simultaneous 'read locks', but only
    allows one 'write lock.' Furthermore the 'write lock' can only be acquired
    when a read lock has already been acquired.

    NOTE: This does not support nesting of locks! You could support nesting
          read locks as long as none of those read locks attempts to upgrade to
          a write lock.
    """

    ##################################################################
    #
    def __init__(self):
        self._read_ready = asyncio.Condition()

        # How many read locks are currently held.
        #
        self._readers = 0

        # A list of the task names that are holding read locks. Whenever a task
        # gets a read lock it is prepended to this list. Whenever a task
        # releases a read lock it is removed from this list.
        #
        self._readers_tasks: List[asyncio.Task] = []

        # How many read locks want to upgrade to a write lock
        #
        self._want_write = 0

        # Track which task has the write lock, if any. This is how we can query
        # to see if the currently running task is the one that has the write
        # lock.
        #
        # This potentially could be leveraged for allowing nestable write locks.
        #
        self._write_lock_task: Optional[asyncio.Task] = None

    ####################################################################
    #
    def is_write_locked(self) -> bool:
        """
        Returns True if the write lock has been acquired.
        """
        # The read ready is only held in two states: when we are
        # incrementing/decrementing the `self._readers` attribute and when the
        # write lock has been acquired.
        #
        # Since no other task can be running and the incr/decr only happens
        # inside the code in the class the only time `self._read_ready` will be
        # locked outside of this class is when the write lock has been
        # acquired.
        #
        return self._read_ready.locked()

    ####################################################################
    #
    def this_task_has_write_lock(self) -> bool:
        """
        Returns True if the current task is the task that has the write
        lock.  If no one has the write lock it raises a RuntimeError.  So the
        correct sequence is test if the write lock is held by someone. Then
        test if this task is the one that holds the write lock.
        """
        if not self._read_ready.locked():
            raise RuntimeError("{self}: No one holds the write lock")
        return self._write_lock_task == asyncio.current_task()

    ####################################################################
    #
    def this_task_has_read_lock(self) -> bool:
        """
        Returns True if the current task has a read lock. Due to the power
        of asyncio being cooperative multitasking we do not need to acquire
        _ready_ready to make this check.
        """
        return asyncio.current_task() in self._readers_tasks

    ####################################################################
    #
    @asynccontextmanager
    async def read_lock(self):
        cur_task = asyncio.current_task()
        assert cur_task  # Can not use the lock outside of asyncio.

        async with self._read_ready:
            self._readers += 1
            self._readers_tasks.insert(0, cur_task)
        try:
            yield
        finally:
            async with self._read_ready:
                # The key thing is that you must have a read lock to upgrade to
                # a write lock. We keep track of how many read locks want to
                # acquire write locks. When this number is the same then we
                # know that only entities desiring a write lock are around (and
                # what is more they are all in wait_for on this same condition
                # -- num read locks == num waiting for a write lock.)
                #
                self._readers -= 1
                self._readers_tasks.remove(cur_task)
                if self._readers == self._want_write:
                    self._read_ready.notify()

    ####################################################################
    #
    @asynccontextmanager
    async def write_lock(self):
        """
        Upgrade a read lock to a read/write lock. This MUST ONLY be called
        when you already have a readlock, otherwise the logic will not work.
        """
        # we do not support nesting of write locks. Luckily since we know who
        # has the write lock and through the power of asyncio's cooperative
        # tasking we can raise an exception if someone tries to acquire a write
        # lock more than once.
        #
        if self.is_write_locked() and self.this_task_has_write_lock():
            raise RuntimeError("attempt to acquire write-lock multiple times")
        async with self._read_ready:
            # While we have the ready_ready lock increment the number of read
            # lock holders that want to upgrade to a write lock.
            #
            self._want_write += 1
            if self._readers == 0:
                raise RuntimeError(
                    "Must already have a read lock to upgrade to a write lock"
                )

            # We will now wait to be notified when the only read locks that are
            # held are ones that want to upgrade to a write lock.
            #
            await self._read_ready.wait_for(
                lambda: self._readers == self._want_write
            )
            # We can decrement that the number of read locks wanting a write
            # lock has gone down. No one else can execute until we exit this
            # `async with`
            #
            self._want_write -= 1

            # And now we have the write lock. Since we still are holding the
            # read_ready condition, no other read lock will be able to do
            # anything until we release our read lock (and send another
            # notify.)
            #
            try:
                self._write_lock_task = asyncio.current_task()
                yield
            finally:
                self._write_lock_task = None


##################################################################
##################################################################
#
class LocalQueueHandler(logging.handlers.QueueHandler):
    """
    Customise the QueueHandler class a little, but only minimally so: there
    is no need to prepare records that go into a local, in-process queue, we
    can skip that process and minimise the cost of logging further.

    This is cribbed from:
         https://www.zopatista.com/python/2019/05/11/asyncio-logging/
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Removed the call to self.prepare(), handle task cancellation
        try:
            self.enqueue(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.handleError(record)


############################################################################
#
# Tried to use aiologger but it raised a bunch of problems (like how to setup
# of formatters, exceptions in the main logging module due to non-awaited
# coroutines) and that it is no longer backwards compatible with the standard
# logging call format.
#
def setup_asyncio_logging() -> None:
    """
    Call this after you have configured all of your log handlers.

    This moves all log handlers to a separate thread.

    The QueueListener enqueues log messages with no-wait so it is safe to call
    from asyncio and will not block.

    Replace handlers on the root logger with a LocalQueueHandler,
    and start a logging.QueueListener holding the original
    handlers.

    This is cribbed from:
         https://www.zopatista.com/python/2019/05/11/asyncio-logging/
    """
    queue: SimpleQueue = SimpleQueue()
    root = logging.getLogger()

    handlers: List[logging.Handler] = []

    handler = LocalQueueHandler(queue)
    root.addHandler(handler)
    for h in root.handlers[:]:
        if h is not handler:
            root.removeHandler(h)
            handlers.append(h)

    listener = logging.handlers.QueueListener(
        queue, *handlers, respect_handler_level=True
    )
    listener.start()

    # NOTE: to make sure that all queued records get logged on program exit
    #       stop the listener.
    #
    atexit.register(lambda: listener.stop())


####################################################################
#
def setup_logging(
    logdir: "StrPath", debug: bool, username: Optional[str] = None
):
    """
    Set up the logger. We log either to files in 'logdir'
    or to stderr.

    NOTE: It does not make sense to log to stderr if we are running in
          daemon mode.. maybe we should exit with a warning before we
          try to enter daemon mode if logdir == 'stderr'

    XXX Move this to use a logging config file if one is provided.
        ie: log to stderr normally, but if there is a logging config file
        use that.
    """
    if debug:
        level = logging.DEBUG
    else:
        level = logging.INFO

    # We define our logging config on the root loggger.
    #
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    h: Union[logging.StreamHandler, logging.handlers.RotatingFileHandler]
    if logdir == "stderr":
        # Do not log to a file, log to stderr.
        #
        h = logging.StreamHandler()
    else:
        # Rotate on every 10mb, keep 5 files.
        #
        logdir = Path(logdir)
        log_file_name = f"{username}-asimapd.log" if username else "asimapd.log"
        log_file_basename = logdir / log_file_name
        h = logging.handlers.RotatingFileHandler(
            log_file_basename, maxBytes=10485760, backupCount=5
        )
    h.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(process)d %(module)s.%(funcName)s %(levelname)s: "
        "%(message)s"
    )
    h.setFormatter(formatter)
    root_logger.addHandler(h)


############################################################################
#
def parsedate(datetime_str: str) -> datetime:
    """
    All date time data is stored as a datetime object in UTC.
    This routine uses common routines provided by python to parse a rfc822
    formatted date time in to a datetime object.

    It is pretty simple, but makes the code a lot shorter and easier to read.
    """
    # email.utils.parsedate_to_datetime creates a naive datetime if the tz
    # string is UTC (ie: "+0000") .. so in that case we set the timezone to be
    # UTC.
    #
    dt = email.utils.parsedate_to_datetime(datetime_str)
    if datetime_str[-5:] == "+0000":
        dt.replace(tzinfo=timezone.utc)
    return dt


####################################################################
#
# XXX Need to validate how we treat `uid_cmd`. RFC3501 says:
#
#       The server should respond with a tagged BAD response to a command that
#       uses a message sequence number greater than the number of messages in
#       the selected mailbox.  This includes "*" if the selected mailbox is
#       empty.
#
def sequence_set_to_list(
    seq_set: MsgSet,
    seq_max: int,
    uid_cmd: bool = False,
):
    """
    Convert a squence set in to a list of numbers.

    We collapse any overlaps and return the list sorted.

    NOTE: Using '*' in a mailbox that has no messages raises the Bad
          exception. If any sequence number is greater than the size
          of the mailbox actually.

    Arguments:
    - `seq_set`: The sequence set we want to convert to a list of numbers.
    - `seq_max`: The largest possible number in the sequence. We
                 replace '*' with this value.
    - `uid_cmd`: This is a UID command sequence and the sequence set can include
                 numbers larger than seq_max.
    """
    result = []
    for elt in seq_set:
        # Any occurences of '*' we can just swap in the sequence max value.
        #
        if elt == "*":
            if seq_max == 0 and not uid_cmd:
                raise Bad(
                    "Message index '*' is greater than the size of the mailbox"
                )
            result.append(seq_max)
        elif isinstance(elt, int):
            if elt > seq_max and not uid_cmd:
                raise Bad(
                    f"Message index '{elt}' is greater than the size of the mailbox"
                )
            result.append(elt)
        elif isinstance(elt, tuple):
            start, end = elt
            if start == "*":
                start = seq_max
            if end == "*":
                end = seq_max
            assert isinstance(start, int)  # These are really here for mypy
            assert isinstance(end, int)  # so it knows that they are ints now
            if (
                start == 0 or end == 0 or start > seq_max or end > seq_max
            ) and not uid_cmd:
                raise Bad(
                    f"Message sequence '{elt}' is greater than the size of "
                    f"the mailbox, start: {start}, end: {end}, "
                    f"seq_max: {seq_max}"
                )
            if start > end:
                result.extend(list(range(end, start + 1)))
            else:
                result.extend(list(range(start, end + 1)))
    return sorted(set(result))


####################################################################
#
def get_uidvv_uid(hdr: str) -> tuple:
    """
    Given a string that is supposedly the value of the 'x-asimapd-uid'
    header from an email message return a tuple comprised of the
    uid_vv, and uid parsed out of that header's contents.

    This deals with the case where we get a malformed header that
    actually has a continuation of the next line mangled into it. It
    does not happen often but some historical messages look like this.

    If we can not parse the uid_vv, uid then we return (None, None)
    which is supposed to be a signal to our caller that this message
    does not have a valid uid_vv, uid.

    Arguments:
    - `hdr`: A string that is the contents of the 'x-asimapd-uid' header from
             an email message.
    """
    s = UID_RE.search(hdr)
    if s:
        return tuple((int(x) for x in s.groups()))
    return (None, None)


####################################################################
#
def with_timeout(t: int):
    """
    A decorator that makes sure that the wrapped async function times out
    after the specified delay in seconds. Raises the asyncio.TimeoutError
    exception.
    """

    def wrapper(corofunc):
        async def run(*args, **kwargs):
            async with timeout(t):
                return await corofunc(*args, **kwargs)

        return run

    return wrapper
