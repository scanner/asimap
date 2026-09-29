"""
Read and write asimap delivery spool entries.

A delivery spool is a directory named `SPOOL_DIR_NAME` in the root of a
user's MH mail store. A producer delivers a message by writing
`<uuid7>-incoming.json`, fsync'ing it, and renaming it to
`<uuid7>-ready.json`. asimap imports ready entries in id order and moves
anything it can not import to the `failed/` subdirectory.

This module has no dependencies outside the standard library so that other
services can install it without pulling in asimap.
"""

# system imports
#
import base64
import binascii
import json
import os
import re
import secrets
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
SPOOL_DIR_NAME = ".asimap-spool"
FAILED_DIR_NAME = "failed"
INCOMING_SUFFIX = "-incoming.json"
READY_SUFFIX = "-ready.json"
MESSAGE_ENCODING = "base64"
DEFAULT_FOLDER = "inbox"

# IMAP system flags a producer may set. `\Recent` is managed by the server
# and can not be set by a client (RFC 3501 section 2.3.2).
#
SYSTEM_FLAGS = frozenset(
    {"\\Answered", "\\Deleted", "\\Draft", "\\Flagged", "\\Seen"}
)

# An IMAP keyword is an atom: no controls, spaces, or atom-specials
# (RFC 3501 section 9, `atom-specials`, plus `]` from `resp-specials`.)
#
_KEYWORD_RE = re.compile(r'^[^\x00-\x20\x7f(){%*"\\\]]+$')

_ENTRY_KEYS = frozenset(
    {
        "version",
        "id",
        "received_at",
        "source",
        "deliveries",
        "message_encoding",
        "message",
    }
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


########################################################################
########################################################################
#
class SpoolFormatError(ValueError):
    """A spool entry does not conform to the spool format."""


########################################################################
########################################################################
#
@dataclass(frozen=True)
class Delivery:
    """
    One destination for a spooled message.

    Args:
        folder: MH folder name, relative to the mail store root. 'inbox' is
            matched case-insensitively.
        flags: IMAP flags to set on the delivered message. No '\\Seen' means
            the message is unseen.
        create: Create the folder if it does not exist. When False and the
            folder is missing, the message is delivered to the inbox.
    """

    folder: str = DEFAULT_FOLDER
    flags: tuple[str, ...] = ()
    create: bool = False

    ####################################################################
    #
    def __post_init__(self) -> None:
        object.__setattr__(self, "folder", validate_folder_name(self.folder))
        object.__setattr__(self, "flags", validate_flags(self.flags))
        if not isinstance(self.create, bool):
            raise SpoolFormatError(
                f"'create' must be a boolean: {self.create!r}"
            )

    ####################################################################
    #
    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable form of this delivery."""
        return {
            "folder": self.folder,
            "flags": list(self.flags),
            "create": self.create,
        }

    ####################################################################
    #
    @classmethod
    def from_dict(cls, data: Any) -> "Delivery":
        """
        Build a Delivery from its JSON form.

        Raises:
            SpoolFormatError: if the data is not a valid delivery.
        """
        if not isinstance(data, dict):
            raise SpoolFormatError(f"delivery must be an object: {data!r}")
        unknown = set(data) - {"folder", "flags", "create"}
        if unknown:
            raise SpoolFormatError(f"unknown delivery keys: {sorted(unknown)}")
        flags = data.get("flags", [])
        if not isinstance(flags, list):
            raise SpoolFormatError(f"'flags' must be a list: {flags!r}")
        return cls(
            folder=data.get("folder", DEFAULT_FOLDER),
            flags=tuple(flags),
            create=data.get("create", False),
        )


########################################################################
########################################################################
#
@dataclass(frozen=True)
class SpoolEntry:
    """
    A message and the metadata asimap needs to deliver it.

    Args:
        id: A lowercase uuid7 string. It is also the file name stem, so
            entries sort by creation time.
        received_at: When the message was received. asimap uses it as the
            message's internal date. Must be timezone aware.
        message: The raw RFC 5322 message, exactly as it is to be stored.
        deliveries: Where to deliver the message. Empty means the inbox.
        source: Name of the producer, for logging.
    """

    id: str
    received_at: datetime
    message: bytes
    deliveries: tuple[Delivery, ...] = field(default_factory=tuple)
    source: str | None = None

    ####################################################################
    #
    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _UUID_RE.match(self.id):
            raise SpoolFormatError(
                f"'id' must be a lowercase uuid7: {self.id!r}"
            )
        if not isinstance(self.received_at, datetime):
            raise SpoolFormatError("'received_at' must be a datetime")
        if self.received_at.utcoffset() is None:
            raise SpoolFormatError("'received_at' must be timezone aware")
        if not isinstance(self.message, bytes) or not self.message:
            raise SpoolFormatError("'message' must be non-empty bytes")
        if self.source is not None and not isinstance(self.source, str):
            raise SpoolFormatError(
                f"'source' must be a string: {self.source!r}"
            )
        if not self.deliveries:
            object.__setattr__(self, "deliveries", (Delivery(),))

    ####################################################################
    #
    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable form of this entry."""
        data: dict[str, Any] = {
            "version": FORMAT_VERSION,
            "id": self.id,
            "received_at": self.received_at.isoformat(),
            "deliveries": [d.to_dict() for d in self.deliveries],
            "message_encoding": MESSAGE_ENCODING,
            "message": base64.b64encode(self.message).decode("ascii"),
        }
        if self.source is not None:
            data["source"] = self.source
        return data

    ####################################################################
    #
    @classmethod
    def from_dict(cls, data: Any) -> "SpoolEntry":
        """
        Build a SpoolEntry from its JSON form.

        Raises:
            SpoolFormatError: if the data is not a valid spool entry.
        """
        if not isinstance(data, dict):
            raise SpoolFormatError("spool entry must be a JSON object")
        unknown = set(data) - _ENTRY_KEYS
        if unknown:
            raise SpoolFormatError(f"unknown entry keys: {sorted(unknown)}")
        version = data.get("version")
        if version != FORMAT_VERSION or isinstance(version, bool):
            raise SpoolFormatError(f"unsupported spool version: {version!r}")
        encoding = data.get("message_encoding")
        if encoding != MESSAGE_ENCODING:
            raise SpoolFormatError(
                f"unsupported message_encoding: {encoding!r}"
            )

        received_at = data.get("received_at")
        if not isinstance(received_at, str):
            raise SpoolFormatError("'received_at' must be an ISO 8601 string")
        try:
            received = datetime.fromisoformat(received_at)
        except ValueError as e:
            raise SpoolFormatError(f"bad 'received_at': {e}") from e

        message = data.get("message")
        if not isinstance(message, str):
            raise SpoolFormatError("'message' must be a base64 string")
        try:
            raw = base64.b64decode(message, validate=True)
        except (binascii.Error, ValueError) as e:
            raise SpoolFormatError(f"bad base64 'message': {e}") from e

        deliveries = data.get("deliveries", [])
        if not isinstance(deliveries, list):
            raise SpoolFormatError("'deliveries' must be a list")

        return cls(
            id=data.get("id", ""),
            received_at=received,
            message=raw,
            deliveries=tuple(Delivery.from_dict(d) for d in deliveries),
            source=data.get("source"),
        )


####################################################################
#
def validate_folder_name(name: Any) -> str:
    """
    Check that a folder name is safe to deliver into.

    The name must be a relative path inside the mail store with no empty,
    '.', '..', or dot-prefixed components. The whole name 'inbox' is
    case-insensitive and is returned in lowercase.

    Args:
        name: The folder name from a spool entry.

    Returns:
        The normalized folder name.

    Raises:
        SpoolFormatError: if the name is not a safe folder name.
    """
    if not isinstance(name, str) or not name:
        raise SpoolFormatError(f"folder must be a non-empty string: {name!r}")
    if "\x00" in name or name.startswith("/"):
        raise SpoolFormatError(f"invalid folder name: {name!r}")
    for part in name.split("/"):
        if not part or part.startswith("."):
            raise SpoolFormatError(f"invalid folder name: {name!r}")
    if name.lower() == DEFAULT_FOLDER:
        return DEFAULT_FOLDER
    return name


####################################################################
#
def validate_flags(flags: Iterable[Any]) -> tuple[str, ...]:
    """
    Check that every flag is a settable IMAP system flag or a keyword.

    Raises:
        SpoolFormatError: if any flag is invalid.
    """
    result = []
    for flag in flags:
        if not isinstance(flag, str):
            raise SpoolFormatError(f"flag must be a string: {flag!r}")
        if flag.startswith("\\"):
            if flag not in SYSTEM_FLAGS:
                raise SpoolFormatError(f"flag can not be set: {flag!r}")
        elif not _KEYWORD_RE.match(flag):
            raise SpoolFormatError(f"invalid keyword: {flag!r}")
        result.append(flag)
    return tuple(result)


####################################################################
#
def uuid7() -> str:
    """Return a new uuid7 (RFC 9562) as a lowercase string."""
    if hasattr(uuid, "uuid7"):
        return str(uuid.uuid7())
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(secrets.token_bytes(10))
    value = (ms & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


####################################################################
#
def spool_dir(maildir: str | os.PathLike[str]) -> Path:
    """Return the spool directory for the MH mail store at `maildir`."""
    return Path(maildir) / SPOOL_DIR_NAME


####################################################################
#
def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename in it is durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


####################################################################
#
def write_entry(spool: str | os.PathLike[str], entry: SpoolEntry) -> Path:
    """
    Write a spool entry and make it visible to asimap.

    The entry is written to `<id>-incoming.json`, fsync'd, and renamed to
    `<id>-ready.json`. asimap only reads ready entries, so it never sees a
    partial file.

    Args:
        spool: The spool directory. Created if it does not exist.
        entry: The entry to write.

    Returns:
        The path of the ready entry.
    """
    spool = Path(spool)
    spool.mkdir(exist_ok=True)
    incoming = spool / f"{entry.id}{INCOMING_SUFFIX}"
    ready = spool / f"{entry.id}{READY_SUFFIX}"
    data = json.dumps(entry.to_dict()).encode("utf-8")

    fd = os.open(incoming, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(incoming, ready)
    except BaseException:
        incoming.unlink(missing_ok=True)
        raise
    _fsync_dir(spool)
    return ready


####################################################################
#
def deliver(
    maildir: str | os.PathLike[str],
    message: bytes,
    deliveries: Iterable[Delivery] = (),
    received_at: datetime | None = None,
    source: str | None = None,
) -> Path:
    """
    Spool a message for delivery into the MH mail store at `maildir`.

    Args:
        maildir: Root of the user's MH mail store.
        message: The raw RFC 5322 message.
        deliveries: Where to deliver it. Empty means the inbox.
        received_at: When it was received. Defaults to now, in UTC.
        source: Name of the producer, for logging.

    Returns:
        The path of the ready spool entry.
    """
    entry = SpoolEntry(
        id=uuid7(),
        received_at=received_at or datetime.now(UTC),
        message=message,
        deliveries=tuple(deliveries),
        source=source,
    )
    return write_entry(spool_dir(maildir), entry)


####################################################################
#
def read_entry(path: str | os.PathLike[str]) -> SpoolEntry:
    """
    Read and validate a ready spool entry.

    Raises:
        SpoolFormatError: if the file is not a valid entry, or its id does
            not match its file name.
        OSError: if the file can not be read.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SpoolFormatError(f"not valid JSON: {e}") from e
    entry = SpoolEntry.from_dict(data)
    if path.name != f"{entry.id}{READY_SUFFIX}":
        raise SpoolFormatError(
            f"id {entry.id!r} does not match file name {path.name!r}"
        )
    return entry


####################################################################
#
def ready_entries(spool: str | os.PathLike[str]) -> list[Path]:
    """Return the ready entries in the spool, oldest first."""
    try:
        with os.scandir(spool) as it:
            paths = [
                Path(e.path)
                for e in it
                if e.name.endswith(READY_SUFFIX) and e.is_file()
            ]
    except FileNotFoundError:
        return []
    return sorted(paths, key=lambda p: p.name)


####################################################################
#
def stale_incoming(
    spool: str | os.PathLike[str], max_age: float, now: float | None = None
) -> list[Path]:
    """
    Return incoming entries older than `max_age` seconds.

    These are left behind by a producer that died before renaming them.
    """
    now = time.time() if now is None else now
    stale = []
    try:
        with os.scandir(spool) as it:
            for e in it:
                if not e.name.endswith(INCOMING_SUFFIX) or not e.is_file():
                    continue
                try:
                    if now - e.stat().st_mtime > max_age:
                        stale.append(Path(e.path))
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        return []
    return sorted(stale, key=lambda p: p.name)


####################################################################
#
def move_to_failed(path: str | os.PathLike[str]) -> Path:
    """
    Move a spool file into the spool's `failed/` subdirectory.

    Returns:
        The file's new path.
    """
    path = Path(path)
    failed = path.parent / FAILED_DIR_NAME
    failed.mkdir(exist_ok=True)
    dest = failed / path.name
    os.rename(path, dest)
    return dest


####################################################################
#
def schema() -> dict[str, Any]:
    """Return the JSON Schema for spool entries."""
    text = (
        resources.files("asimap_spool")
        .joinpath("spool-entry.schema.json")
        .read_text(encoding="utf-8")
    )
    result: dict[str, Any] = json.loads(text)
    return result
