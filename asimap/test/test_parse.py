#!/usr/bin/env python
#
# Copyright (C) 2005 Eric "Scanner" Luce
#
# File: $Id: imapparse_test.py 1456 2007-12-16 07:48:08Z scanner $
#
"""
Test the asimap IMAP message parser.
"""

from asimap.parse import IMAPClientCommand


####################################################################
#
def test_parse_good_messages(good_received_messages):
    """
    Test the set of messages we know should succeed and what we expect
    the parsed result to print out as.
    """
    for msg, result in good_received_messages:
        p = IMAPClientCommand(msg)
        p.parse()
        assert str(p) == result
