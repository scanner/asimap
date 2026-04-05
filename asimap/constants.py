#!/usr/bin/env python
#
# File: $Id$
#
"""
Global constants and flag/sequence conversion utilities for asimap.

Defines IMAP system flags, their MH sequence name mappings, RFC 6154
special-use attributes, the maximum allowed input size, and helper
functions for converting between IMAP flag names and MH sequence names.
"""

from collections import defaultdict
from enum import StrEnum

type Sequences = defaultdict[str, set[int]]

# Maximum size for any single IMAP input (literal strings and accumulated
# command buffer). 10 MiB. Clients exceeding this are rejected with BAD.
#
MAX_INPUT_SIZE = 10 * 1024 * 1024


# Here we set the list of defined system flags (flags that may be set on a
# message) and the subset of those flags that may not be set by a  user.
#
# XXX Convert these to StrEnum's.
#
class SystemFlags(StrEnum):
    """IMAP system flags as defined in RFC 3501.

    These are the standard per-message flags that all IMAP servers must
    support. Each value is the wire-format flag name including the leading
    backslash.
    """

    ANSWERED = r"\Answered"
    DELETED = r"\Deleted"
    DRAFT = r"\Draft"
    FLAGGED = r"\Flagged"
    RECENT = r"\Recent"
    SEEN = r"\Seen"


SYSTEM_FLAGS = (
    r"\Answered",
    r"\Deleted",
    r"\Draft",
    r"\Flagged",
    r"\Recent",
    r"\Seen",
)
NON_SETTABLE_FLAGS = r"\Recent"
PERMANENT_FLAGS = (
    r"\Answered",
    r"\Deleted",
    r"\Draft",
    r"\Flagged",
    r"\Seen",
    r"\*",
)

# mh does not allow '\' in sequence names so we have a mapping between
# the actual mh sequence name and the corresponding system flag.
#
SYSTEM_FLAG_MAP = {
    "replied": r"\Answered",
    "Deleted": r"\Deleted",
    "Draft": r"\Draft",
    "flagged": r"\Flagged",
    "Recent": r"\Recent",
    "Seen": r"\Seen",
}

REV_SYSTEM_FLAG_MAP = {v: k for k, v in SYSTEM_FLAG_MAP.items()}

# RFC 6154 SPECIAL-USE mailbox attributes. Maps folder names (matching
# as_email_service conventions) to their IMAP special-use attribute.
#
SPECIAL_USE_ATTRS: dict[str, str] = {
    "Junk": r"\Junk",
    "Archive": r"\Archive",
    "Sent Messages": r"\Sent",
    "Drafts": r"\Drafts",
    "Deleted Messages": r"\Trash",
}
SPECIAL_USE_ATTR_VALUES: set[str] = set(SPECIAL_USE_ATTRS.values())


####################################################################
#
def flags_to_seqs(flags: list[str] | None) -> list[str]:
    """Convert a list of IMAP flag names to their MH sequence name equivalents.

    Args:
        flags: List of IMAP flag strings (e.g. `[r"\\Answered", "custom"]`).
            Pass `None` to get an empty list back.

    Returns:
        List of MH sequence names corresponding to the input flags.
    """
    flags = [] if flags is None else flags
    return [flag_to_seq(x) for x in flags]


####################################################################
#
def flag_to_seq(flag: str) -> str:
    """Map a single IMAP flag name to its MH sequence name.

    System flags that have a known MH equivalent (e.g. `\\Answered` maps
    to `replied`) are translated; all other flags are returned unchanged.

    Args:
        flag: An IMAP flag string such as `r"\\Answered"` or a keyword flag.

    Returns:
        The corresponding MH sequence name, or the original flag if no
        mapping exists.
    """
    return REV_SYSTEM_FLAG_MAP[flag] if flag in REV_SYSTEM_FLAG_MAP else flag


####################################################################
#
def seqs_to_flags(seqs: list[str] | None) -> list[str]:
    """Convert a list of MH sequence names to their IMAP flag equivalents.

    Args:
        seqs: List of MH sequence name strings (e.g. `["replied", "Seen"]`).
            Pass `None` to get an empty list back.

    Returns:
        List of IMAP flag names corresponding to the input sequences.
    """
    seqs = [] if seqs is None else seqs
    return [seq_to_flag(x) for x in seqs]


####################################################################
#
def seq_to_flag(seq: str) -> str:
    """Map a single MH sequence name to its IMAP flag equivalent.

    This is the inverse of :func:`flag_to_seq`. Known MH sequences (e.g.
    `replied` maps to `\\Answered`) are translated; all others are returned
    unchanged.

    Args:
        seq: An MH sequence name string such as `"replied"` or `"Seen"`.

    Returns:
        The corresponding IMAP flag string, or the original sequence name if
        no mapping exists.
    """
    return SYSTEM_FLAG_MAP[seq] if seq in SYSTEM_FLAG_MAP else seq
