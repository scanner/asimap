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
SYSTEM_FLAGS = (r'\Answered', r'\Deleted', r'\Draft', r'\Flagged', r'\Recent',
                r'\Seen', r'\Unseen')
NON_SETTABLE_FLAGS = (r'\Recent')
PERMANENT_FLAGS = (r'\Answered', r'\Deleted', r'\Draft', r'\Flagged', r'\Seen',
                   r'\Unseen', r'\*')

# mh does not allow '\' in sequence names so we have a mapping between
# the actual mh sequence name and the corresponding system flag.
#
SYSTEM_FLAG_MAP = {
    'Answered' : r'\Answered',
    'Deleted'  : r'\Deleted',
    'Draft'    : r'\Draft',
    'Flagged'  : r'\Flagged',
    'Recent'   : r'\Recent',
    'Seen'     : r'\Seen',
    'unseen'   : r'\Unseen',
    }

