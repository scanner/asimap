"""
Utility functions, logging setup, and lock primitives for asimap.

Contains helpers that do not belong to any single module: IMAP sequence-set
parsing, message UID header I/O, the :class:`UpgradeableReadWriteLock` asyncio
primitive, logging initialisation, and assorted file utilities.
"""

# system imports
#
import asyncio
import atexit
import codecs
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
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from itertools import count, groupby
from pathlib import Path
from queue import SimpleQueue
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
)

# 3rd party module imports
#
import aiofiles
import aiofiles.os
from aiofiles.ospath import wrap as aiofiles_wrap  # type: ignore[attr-defined]

# Project imports
#
from .exceptions import Bad

if TYPE_CHECKING:
    from _typeshed import StrPath

type MsgSet = list | set | tuple

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

LOGGED_IN_USER: str | None = None
# REMOTE_ADDRESS:Optional[str] = None

####################################################################
#
# Provide os.utime as an asyncio function via aiosfiles `wrap` async decorator
#
utime = aiofiles_wrap(os.utime)


####################################################################
#
def encoding_search_fn(encoding: str) -> codecs.CodecInfo | None:
    """Codec search function that maps non-standard encoding names to stdlib codecs.

    Register with :func:`codecs.register` to handle unusual encoding labels
    found in older email messages.

    Args:
        encoding: Encoding name as supplied by the email parser.

    Returns:
        A :class:`codecs.CodecInfo` for the closest standard equivalent, or
        ``None`` if the name is not a known alias.
    """
    match encoding:
        case "ansi_x3.110_1983":
            return codecs.lookup("ascii")
        case "unknown_8bit":
            return codecs.lookup("latin-1")
        case _:
            return None


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
    def __init__(self) -> None:
        self._read_ready = asyncio.Condition()

        # How many read locks are currently held.
        #
        self._readers = 0

        # A list of the task names that are holding read locks. Whenever a task
        # gets a read lock it is prepended to this list. Whenever a task
        # releases a read lock it is removed from this list.
        #
        self._readers_tasks: list[asyncio.Task] = []

        # How many read locks want to upgrade to a write lock
        #
        self._want_write = 0

        # Track which task has the write lock, if any. This is how we can query
        # to see if the currently running task is the one that has the write
        # lock.
        #
        # This potentially could be leveraged for allowing nestable write locks.
        #
        self._write_lock_task: asyncio.Task | None = None

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
    async def read_lock(self) -> AsyncIterator[None]:
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
    async def write_lock(self) -> AsyncIterator[None]:
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

    handlers: list[logging.Handler] = []

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
    username: str | None = None,
    remote_addr: str | None = None,
    trace_dir: Optional["StrPath"] = None,
) -> None:
    """Initialise the logging system for an asimap process.

    Attempts to load a logging configuration file in this order: the path
    given via ``log_config``, then each path in ``DEFAULT_LOG_CONFIG_FILES``,
    then a built-in default that writes to stderr (or to a rotating file under
    ``LOG_DIR`` if that directory exists).

    A custom log-record factory is installed that injects a ``username`` field
    into every log record, making it possible to distinguish per-user
    subprocess output when logs are aggregated (e.g. by Docker).

    NOTE: Logging to stderr is not useful in daemon mode.

    Args:
        log_config: Path to a JSON or INI-style logging configuration file,
            or ``None`` to use the auto-discovery logic.
        debug: When ``True``, sets the root logger level to ``DEBUG``.
        username: The authenticated username to stamp onto log records.
        remote_addr: The remote client address (reserved for future use).
        trace_dir: Directory in which to write JSON trace files.  If the
            directory does not exist a warning is logged and tracing is
            silently disabled.
    """
    global LOGGED_IN_USER
    LOGGED_IN_USER = username if username else "no_user"
    old_factory = logging.getLogRecordFactory()

    def log_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
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
    DEFAULT_LOGGING_CONFIG: dict[str, Any] = {
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
    """Parse an RFC 2822 date string into a timezone-aware UTC datetime.

    Args:
        datetime_str: An RFC 2822 formatted date/time string as found in
            email headers (e.g. ``"Thu, 01 Jan 2026 00:00:00 +0000"``).

    Returns:
        A timezone-aware :class:`datetime` in UTC.
    """
    # email.utils.parsedate_to_datetime creates a naive datetime if the tz
    # string is UTC (ie: "+0000") .. so in that case we set the timezone to be
    # UTC (ie, we are making the datetime be non-naive, and its TZ is UTC).
    #
    dt = email.utils.parsedate_to_datetime(datetime_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
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
) -> list[int]:
    """Convert an IMAP sequence set to a sorted, deduplicated list of integers.

    Handles individual numbers, ``"*"`` (largest message number), and
    ``(start, end)`` range tuples — the three element types produced by the
    IMAP parser.

    NOTE: Using ``"*"`` in an empty mailbox (``seq_max == 0``) raises
          :class:`~asimap.exceptions.Bad` unless this is a UID command.

    Args:
        seq_set: The parsed sequence set — an iterable of ints, ``"*"``
            strings, or ``(start, end)`` tuples.
        seq_max: The largest valid message sequence number (``0`` for an empty
            mailbox). ``"*"`` elements are replaced with this value.
        uid_cmd: When ``True``, sequence numbers larger than ``seq_max`` are
            allowed (UID space may exceed the message count).

    Returns:
        Sorted list of unique integer message sequence numbers.

    Raises:
        Bad: If any sequence number is out of range for a non-UID command.
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
    """Parse the uid_vv and uid integers from an ``X-asimapd-uid`` header value.

    Tolerates malformed headers where a continuation line has been mangled
    into the value (a known issue with some historical messages).

    Args:
        hdr: The raw string value of the ``X-asimapd-uid`` header.

    Returns:
        A ``(uid_vv, uid)`` tuple of ints if parsing succeeds, or
        ``(None, None)`` if the header value is not parseable.
    """
    s = UID_RE.search(hdr)
    if s:
        return tuple(int(x) for x in s.groups())
    return (None, None)


####################################################################
#
def with_timeout(t: int) -> Callable[..., Any]:
    """
    A decorator that makes sure that the wrapped async function times out
    after the specified delay in seconds. Raises the asyncio.TimeoutError
    exception.
    """

    def wrapper(corofunc: Callable[..., Any]) -> Callable[..., Any]:
        async def run(*args: Any, **kwargs: Any) -> Any:
            async with asyncio.timeout(t):
                return await corofunc(*args, **kwargs)

        return run

    return wrapper


##################################################################
#
def find_header_in_binary_file(fname: "StrPath", header: str) -> str | None:
    """Search for an email header in a binary message file.

    Reads the file as raw bytes line by line (latin-1 encoding). The search
    stops at the first blank line (end of headers) or end of file. The
    comparison is case-insensitive.

    NOTE: The full matched line, including the header name, is returned
          after stripping leading and trailing whitespace.

    Args:
        fname: Path to the message file to search.
        header: The header name to look for, e.g. ``"X-asimapd-uid"``.
            Must be encodable as ``latin-1``.

    Returns:
        The matched header line as a string, or ``None`` if not found.
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
async def update_replace_header_in_binary_file(
    fname: "StrPath", header: str
) -> None:
    """Replace or insert an email header in a binary message file.

    Scans the header section of the file (everything before the first blank
    line). If a line starting with the same header name is found it is
    replaced with ``header``; if no such line exists the header is appended
    just before the blank separator line. The body of the message is
    passed through unchanged.

    The update is performed atomically: a temporary sibling file is written,
    its permissions and mtime are set to match the original, and then it is
    renamed over the original.

    ``header`` must be encodable as ``latin-1`` and must follow the format
    ``"header-name: value"`` (no spaces in the header name).

    Args:
        fname: Path to the message file to update.
        header: Full header line to insert or replace, e.g.
            ``"X-asimapd-uid: 1.42"``.
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
    """Compact a set of integers into an MH sequence string.

    Contiguous runs are collapsed into ``start-end`` notation, e.g.
    ``[1, 3, 4, 5, 6]`` becomes ``"1,3-6"``.

    Based on the truncation routine from ``mailbox.py:set_sequences``.

    Args:
        keys: Iterable of integer message keys to compact.

    Returns:
        A comma-separated string of numbers and ranges.
    """

    def as_range(
        iterable: Iterator[int],
    ) -> str:  # not sure how to do this part elegantly
        grouped_ints = list(iterable)
        if len(grouped_ints) > 1:
            return f"{grouped_ints[0]}-{grouped_ints[-1]}"
        else:
            return f"{grouped_ints[0]}"

    keys = sorted(keys)
    result = ",".join(
        as_range(g)
        for _, g in groupby(keys, key=lambda n, c=count(): n - next(c))  # type: ignore[misc]
    )  # '1-3,6-7,10'

    return result


####################################################################
#
def expand_sequence(contents: str) -> list[int]:
    """Expand an MH sequence string into a sorted list of integers.

    The inverse of :func:`compact_sequence`. For example ``"1,3-6"``
    becomes ``[1, 3, 4, 5, 6]``.

    Based on the expansion routine from ``mailbox.py:get_sequences``.

    Args:
        contents: An MH sequence string such as ``"1,3-6,10"``.

    Returns:
        Sorted list of integer message keys.
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
