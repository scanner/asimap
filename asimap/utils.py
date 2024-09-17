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
import json
import logging
import logging.config
import logging.handlers
import os
import re
import stat
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from itertools import count, groupby
from pathlib import Path
from queue import SimpleQueue
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    TypeAlias,
    Union,
)

# 3rd party module imports
#
import aiofiles
import aiofiles.os
from aiofiles.ospath import wrap as aiofiles_wrap

# Project imports
#
from .exceptions import Bad

if TYPE_CHECKING:
    from _typeshed import StrPath

MsgSet: TypeAlias = Union[List | Set | Tuple]

LOG_DIR = Path("/opt/asimap/logs")

# RE used to suss out the digits of the uid_vv/uid header in an email
# message
#
UID_RE = re.compile(r"(\d+)\s*\.\s*(\d+)")

# The MHmessage header that is used for holding a messages uid.
#
UID_HDR = "X-asimapd-uid"

DEFAULT_LOG_CONFIG_FILES = [
    Path("/opt/asimap/asimapd_log.json"),
    Path("/opt/asimap/asimapd_log.cfg"),
    Path("/etc/asimapd_log.json"),
    Path("/etc/asimapd_log.cfg"),
    Path("/usr/local/etc/asimapd_log.json"),
    Path("/usr/local/etc/asimapd_log.cfg"),
    Path("/opt/local/etc/asimapd_log.json"),
    Path("/opt/local/etc/asimapd_log.cfg"),
]

LOGGED_IN_USER: Optional[str] = None
# REMOTE_ADDRESS:Optional[str] = None


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
    # XXX Maybe we should make this take a boolean.. that is "noop if this task
    #     already has the read lock." That way you can safely "nest" readlock
    #     calls since they are only via context manager, the nested state is
    #     entirely handled by the context manager.
    #     (We could do the same with write locks?)
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
                    self._read_ready.notify_all()

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
    log_config: Optional["StrPath"],
    debug: bool,
    username: Optional[str] = None,
    remote_addr: Optional[str] = None,
    trace_dir: Optional["StrPath"] = None,
):
    """
    Set up the logger. We log either to files in 'logdir'
    or to stderr.

    NOTE: It does not make sense to log to stderr if we are running in
          daemon mode.. maybe we should exit with a warning before we
          try to enter daemon mode if logdir == 'stderr'

    NOTE: We also use a custom log record factory to add `username` and
          `remaddr` fields to the log record.

    XXX Move this to use a logging config file if one is provided.
        ie: log to stderr normally, but if there is a logging config file
        use that.
    """
    global LOGGED_IN_USER
    LOGGED_IN_USER = username if username else "no_user"
    old_factory = logging.getLogRecordFactory()

    def log_record_factory(*args, **kwargs):
        """
        We add a log record factory to our logging system so that we can
        add as a fundamental part of our log records the logged in user and
        remote address if they are set to our log records.

        One of the purposes is that when asimapd is run inside of a docker
        container all the individual user_server processes will have all their
        logs mixed together in the logs collected by docker and we need to be
        able to separate them by user (and by remote address)
        """
        record = old_factory(*args, **kwargs)
        record.username = LOGGED_IN_USER
        return record

    logging.setLogRecordFactory(log_record_factory)
    root_logger = logging.getLogger()

    if debug:
        root_logger.setLevel(logging.DEBUG)

    # Attempt to load the logging config passed in. If we are not able to load
    # that then check a bunch of common directories. If none of those work, use
    # a default config that logs to stderr.
    #
    if log_config is not None:
        log_config = Path(log_config)
        if log_config.exists():
            if log_config.suffix == ".json":
                cfg = json.loads(log_config.read_text())
                logging.config.dictConfig(cfg)
            else:
                logging.config.fileConfig(str(log_config))
            return
        print(
            f"WARNING: Logging config '{log_config}' does not exist",
            file=sys.stderr,
        )

    for log_config in DEFAULT_LOG_CONFIG_FILES:
        if log_config.exists():
            if log_config.suffix == ".json":
                cfg = json.loads(log_config.read_text())
                logging.config.dictConfig(cfg)
            else:
                logging.config.fileConfig(str(log_config))
            return

    # If no logging config file is specified then this is what will be used.
    # It is formatted as a logging config dict.
    #
    DEFAULT_LOGGING_CONFIG: Dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "basic": {
                "format": "[{asctime}] {username:<30} {levelname}:{module}.{funcName}: {message}",
                "style": "{",
            },
            "trace": {
                "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                "format": "%(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "basic",
                "stream": "ext://sys.stderr",
            },
        },
        "loggers": {
            "asimap": {
                "handlers": ["console"],
                "level": "DEBUG" if debug else "INFO",
                "propagate": True,
            },
            "core.run": {  # This is used by sqlite3 and is noisy at debug.
                "handlers": ["console"],
                "level": "ERROR",
                "propagate": True,
            },
        },
    }

    # If the log dir exists the write our logs there.
    #
    if LOG_DIR.exists() and LOG_DIR.is_dir():
        DEFAULT_LOGGING_CONFIG["handlers"]["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "basic",
            "filename": str(LOG_DIR / f"{LOGGED_IN_USER}-asimapd.log"),
            "maxBytes": 20971520,
            "backupCount": 5,
        }
        DEFAULT_LOGGING_CONFIG["loggers"]["asimap"]["handlers"] = ["file"]

    # Add the trace file sections only if the trace dir exists.
    #
    warn_no_trace_dir = False
    if trace_dir:
        trace_dir = Path(trace_dir)
        if trace_dir.exists() and trace_dir.is_dir():
            DEFAULT_LOGGING_CONFIG["handlers"]["trace_file"] = {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "trace",
                "filename": f"{trace_dir}/{LOGGED_IN_USER}-asimapd.trace",
                "maxBytes": 20971520,
                "backupCount": 5,
            }
            DEFAULT_LOGGING_CONFIG["loggers"]["asimap.trace"] = {
                "handlers": ["trace_file"],
                "level": "INFO",
                "propagate": False,
            }
        else:
            warn_no_trace_dir = True
    logging.config.dictConfig(DEFAULT_LOGGING_CONFIG)
    logger = logging.getLogger("asimap.utils")
    logger.info("Logging initialized")
    logger.debug("Debug enabled")
    if warn_no_trace_dir:
        logger.warning(
            "Unable to set up tracing because trace dir '%s' either does not exist or is not a directory.",
            trace_dir,
        )


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
    # UTC (ie, we are making the datetime be non-naive, and its TZ is UTC).
    #
    dt = email.utils.parsedate_to_datetime(datetime_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
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
                    f"Message index '{elt}' is invalid in empty mailbox when not a uid command"
                )
            result.append(seq_max)
        elif isinstance(elt, int):
            if elt < 1:
                raise Bad(
                    f"Message index '{elt}' is invalid. Must be at least 1"
                )

            if elt > seq_max and not uid_cmd:
                raise Bad(
                    f"Message index '{elt}' is greater than the size of the mailbox"
                )
            result.append(elt)
        elif isinstance(elt, tuple):
            start, end = elt
            if (start == "*" or end == "*") and seq_max == 0 and not uid_cmd:
                raise Bad(
                    f"Message sequence '{elt}' is invalid. Start: {start}, "
                    f"end: {end}, seq_max: {seq_max}"
                )
            if start == "*":
                start = seq_max
            if end == "*":
                end = seq_max
            assert isinstance(start, int)  # These are really here for mypy
            assert isinstance(end, int)  # so it knows that they are ints now
            if (
                start < 1 or end < 1 or start > seq_max or end > seq_max
            ) and not uid_cmd:
                raise Bad(
                    f"Message sequence '{elt}' is invalid. Start: {start}, "
                    f"end: {end}, seq_max: {seq_max}"
                )
            # In a range it may be <start>:<end> or <end>:<start>
            #
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
            async with asyncio.timeout(t):
                return await corofunc(*args, **kwargs)

        return run

    return wrapper


##################################################################
#
def find_header_in_binary_file(
    fname: "StrPath", header: str
) -> Union[str | None]:
    """
    Convert the header string given as an argument to bytes. The string
    must be encodable as `latin-1`. If not an encoding error will be
    raised.

    The file indicated by fname is opened "rb" and read as binary line by
    line. If any line begins with the `header`, the line is converted to a
    string, again using `latin-1` encoding, and returned. If that fails an
    encoding error will be raised.

    NOTE: The entire line, including the matched `header` string is
    returned, but it will be `strip()`d of any leading or trailing white
    space.

    The search progresses until two blank lines or the end of the file is
    reached.

    If no match is found `None` is returned.
    """
    fname = str(fname)
    header_b = bytes(header, "latin-1").lower()
    with open(fname, "rb") as f:
        for line in f:
            line = line.strip().lower()
            if len(line) == 0:
                return None
            if line.startswith(header_b):
                return str(line, "latin-1")
    return None


####################################################################
#
async def update_replace_header_in_binary_file(fname: "StrPath", header: str):
    """
    This will go through the file indicating by fname, as a binary file,
    and if it encounters a line that begins with the same header as `header` it
    will replace that existing line with `header`.

    It will only replace/update a line in the _headers_ of the file. The
    headers are separated from the rest of the file by a blank line.

    If the header can not be found in the files existing headers, then the
    given header line will be added to the headers of the file.

    `header` must be encodable via `latin-1` to a bytes.

    `header` is expected to consist of the "header name" followed by a colon,
    followed by the "header data".

    eg: "foo: hello there"

    `foo` is the header. ` hello there` is the header data.
    Spaces are not allowed in the header.

    So with `foo: hello there` if a line begins with `foo:` it will be replaced
    with `foo: hello there`.

    This operation is done by writing a new file adjacent to the one specfied
    via `fname`. When the new file has been written, its timestamp and mode
    will be set to the same as the file it is replacing and then renamed over
    the existing file.
    """
    fname = str(fname)
    headerb = bytes(header, "latin-1")
    header_name = headerb.split(b":")[0].lower()
    new_fname = fname + "-" + str(time.time())
    in_header = True
    found_header = False
    stats = await aiofiles.os.stat(fname)
    line_sep = None

    with open(fname, "rb") as input:
        with open(new_fname, "wb") as output:
            for line in input:
                if line_sep is None:
                    line_sep = b"\r\n" if line.endswith(b"\r\n") else b"\n"

                if in_header:
                    if len(line.strip()) == 0:
                        in_header = False
                        if not found_header:
                            output.write(headerb + line_sep)
                    elif line.lower().startswith(header_name):
                        found_header = True
                        line = headerb + line_sep
                output.write(line)

    os.chmod(new_fname, stat.S_IMODE(stats.st_mode))
    await utime(new_fname, (stats.st_mtime, stats.st_mtime))
    await aiofiles.os.rename(new_fname, fname)


####################################################################
#
def compact_sequence(keys: Iterable[int]) -> str:
    """
    Turns a msg set in to a compact string. Contiguous ranges are turned
    from 1,3,4,5,6 to '1,3-6'

    Based on the truncation routine from mailbox.py:set_sequences
    """

    def as_range(
        iterable: Iterator[int],
    ) -> str:  # not sure how to do this part elegantly
        grouped_ints = list(iterable)
        if len(grouped_ints) > 1:
            return "{0}-{1}".format(grouped_ints[0], grouped_ints[-1])
        else:
            return "{0}".format(grouped_ints[0])

    keys = sorted(keys)
    result = ",".join(
        as_range(g)
        for _, g in groupby(keys, key=lambda n, c=count(): n - next(c))
    )  # '1-3,6-7,10'

    return result


####################################################################
#
def expand_sequence(contents: str) -> List[int]:
    """
    Turns a compacted sequence in to a list of integers.
    The string '1,3-6' becomes [1,3,4,5,6]

    Based on the expansion routine from mailbox.py:get_sequences
    """
    if not contents.strip():
        return []

    keys = set()
    for spec in contents.split(","):
        if spec.isdigit():
            keys.add(int(spec))
        else:
            start, stop = (int(x) for x in spec.split("-"))
            keys.update(range(start, stop + 1))

    return sorted(keys)
