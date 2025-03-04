#!/usr/bin/env python
#
# File: $Id$
#
"""
Various global constants.
"""
from collections import defaultdict
from enum import StrEnum
from typing import List, Optional, Set, TypeAlias

Sequences: TypeAlias = defaultdict[str, Set[int]]


# Here we set the list of defined system flags (flags that may be set on a
# message) and the subset of those flags that may not be set by a  user.
#
# XXX Convert these to StrEnum's.
#
class SystemFlags(StrEnum):
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


####################################################################
#
def flags_to_seqs(flags: Optional[List[str]]) -> List[str]:
    """
    Converts an array of IMAP flags to MH sequence names.
    """
    flags = [] if flags is None else flags
    return [flag_to_seq(x) for x in flags]


####################################################################
#
def flag_to_seq(flag):
    """
    Map an IMAP flag to an mh sequence name. This basically sees if the flag
    is one we need to translate or not.

    Arguments:
    - `flag`: The IMAP flag we are going to translate.
    """
    return REV_SYSTEM_FLAG_MAP[flag] if flag in REV_SYSTEM_FLAG_MAP else flag


####################################################################
#
def seqs_to_flags(seqs: Optional[List[str]]) -> List[str]:
    """
    Converts an array of MH sequence names to IMAP flags
    """
    seqs = [] if seqs is None else seqs
    return [seq_to_flag(x) for x in seqs]


####################################################################
#
def seq_to_flag(seq: str) -> str:
    """
    The reverse of flag to seq - map an MH sequence name to the IMAP flag.

    Arguments:
    - `seq`: The MH sequence name
    """
    return SYSTEM_FLAG_MAP[seq] if seq in SYSTEM_FLAG_MAP else seq
