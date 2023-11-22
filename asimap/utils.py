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
import calendar
import datetime
import email.utils
import logging
import logging.handlers
import re
from contextlib import asynccontextmanager
from pathlib import Path
from queue import SimpleQueue
from typing import TYPE_CHECKING, List, Optional, Union

# 3rd party module imports
#
import pytz

# Project imports
#
from .exceptions import Bad

if TYPE_CHECKING:
    from _typeshed import StrPath

# RE used to suss out the digits of the uid_vv/uid header in an email
# message
#
uid_re = re.compile(r"(\d+)\s*\.\s*(\d+)")


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

        # How many read locks want to upgrade to a write lock
        #
        self._want_write = 0

    ####################################################################
    #
    @asynccontextmanager
    async def read_lock(self):
        async with self._read_ready:
            self._readers += 1
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
                if self._readers == self._want_write:
                    self._read_ready.notify_all()

    ####################################################################
    #
    @asynccontextmanager
    async def write_lock(self):
        """
        Upgrade a read lock to a read/write lock. This MUST ONLY be called
        when you already have a readlock, otherwise the logic will not work.
        """
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
            await self._read_ready.await_for(
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
            yield


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
def parsedate(date_time_str):
    """
    All date time data is stored as a datetime.datetime object in UTC.
    This routine uses common routines provided by python to parse a rfc822
    formatted date time in to a datetime.datetime object.

    It is pretty simple, but makes the code a lot shorter and easier to read.
    """
    return datetime.datetime.fromtimestamp(
        email.utils.mktime_tz(email.utils.parsedate_tz(date_time_str)),
        pytz.UTC,
    )


############################################################################
#
def formatdate(datetime, localtime=False, usegmt=False):
    """
    This is the reverse. It will take a datetime object and format
    and do the deconversions necessary to pass it to email.utils.formatdate()
    and thus return a string properly formatted as an RFC822 date.
    """
    return email.utils.formatdate(
        calendar.timegm(datetime.utctimetuple()),
        localtime=localtime,
        usegmt=usegmt,
    )


####################################################################
#
def sequence_set_to_list(seq_set, seq_max, uid_cmd=False):
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
    - `uid_cmd`: This is a UID command sequence and it can include
      numbers larger than seq_max.
    """
    result = []
    for elt in seq_set:
        # Any occurences of '*' we can just swap in the sequence max value.
        #
        if str(elt) == "*":
            if seq_max == 0 and not uid_cmd:
                raise Bad(
                    "Message index '*' is greater than the size of "
                    "the mailbox"
                )
            result.append(seq_max)
        elif isinstance(elt, int):
            if elt > seq_max and not uid_cmd:
                raise Bad(
                    f"Message index '{elt}' is greater than the size of "
                    "the mailbox"
                )
            result.append(elt)
        elif isinstance(elt, tuple):
            start, end = elt
            if str(start) == "*":
                start = seq_max
            if str(end) == "*":
                end = seq_max
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
def get_uidvv_uid(hdr):
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
    s = uid_re.search(hdr)
    if s:
        return tuple((int(x) for x in s.groups()))
    return (None, None)
