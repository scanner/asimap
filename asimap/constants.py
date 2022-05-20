#!/usr/bin/env python
#
# File: $Id$
#
"""
Various global constants.
"""

# system imports
#

# Here we set the list of defined system flags (flags that may be set on a
# message) and the subset of those flags that may not be set by a  user.
#
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

REVERSE_SYSTEM_FLAG_MAP = {}
for key, value in SYSTEM_FLAG_MAP.items():
    REVERSE_SYSTEM_FLAG_MAP[value] = key


####################################################################
#
def flag_to_seq(flag):
    """
    Map an IMAP flag to an mh sequence name. This basically sees if the flag
    is one we need to translate or not.

    Arguments:
    - `flag`: The IMAP flag we are going to translate.
    """
    if flag in REVERSE_SYSTEM_FLAG_MAP:
        return REVERSE_SYSTEM_FLAG_MAP[flag]
    return flag


####################################################################
#
def seq_to_flag(seq):
    """
    The reverse of flag to seq - map an MH sequence name to the IMAP flag.

    Arguments:
    - `seq`: The MH sequence name
    """
    if seq in SYSTEM_FLAG_MAP:
        return SYSTEM_FLAG_MAP[seq]
    return seq
