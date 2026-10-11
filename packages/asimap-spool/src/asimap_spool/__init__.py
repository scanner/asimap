"""
Deliver messages to asimap by dropping them in a spool directory.

    from asimap_spool import Delivery, deliver

    deliver(maildir, raw_bytes, [Delivery("Lists/python", create=True)])
"""

from .spool import (
    DEFAULT_FOLDER,
    FAILED_DIR_NAME,
    FORMAT_VERSION,
    INCOMING_SUFFIX,
    READY_SUFFIX,
    SPOOL_DIR_NAME,
    SYSTEM_FLAGS,
    Delivery,
    SpoolEntry,
    SpoolFormatError,
    SpoolUnavailableError,
    deliver,
    move_to_failed,
    read_entry,
    ready_entries,
    schema,
    spool_dir,
    stale_incoming,
    uuid7,
    validate_flags,
    validate_folder_name,
    write_entry,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_FOLDER",
    "FAILED_DIR_NAME",
    "FORMAT_VERSION",
    "INCOMING_SUFFIX",
    "READY_SUFFIX",
    "SPOOL_DIR_NAME",
    "SYSTEM_FLAGS",
    "Delivery",
    "SpoolEntry",
    "SpoolFormatError",
    "SpoolUnavailableError",
    "deliver",
    "move_to_failed",
    "read_entry",
    "ready_entries",
    "schema",
    "spool_dir",
    "stale_incoming",
    "uuid7",
    "validate_flags",
    "validate_folder_name",
    "write_entry",
]
