#!/usr/bin/env python
#
# Copyright (C) 2005 Eric "Scanner" Luce
#
# File: $Id: imapparse_test.py 1456 2007-12-16 07:48:08Z scanner $
#
"""
Test the asimap IMAP message parser.
"""
from ..parse import CONFLICTING_COMMANDS, IMAPClientCommand, sole_access_cmd


####################################################################
#
def test_parse_good_messages(good_received_imap_messages):
    """
    Test the set of messages we know should succeed and what we expect
    the parsed result to print out as.
    """
    for msg, result in good_received_imap_messages:
        p = IMAPClientCommand(msg)
        p.parse()
        assert str(p) == result


####################################################################
#
def test_sole_access_cmd(good_received_imap_messages):
    for msg, _ in good_received_imap_messages:
        result = False
        if "PEEK" in msg:
            result = True
        for cmd in CONFLICTING_COMMANDS:
            if cmd in msg:
                result = True
                break

        p = IMAPClientCommand(msg)
        p.parse()

        assert result == sole_access_cmd(p)


####################################################################
#
def test_fetch_peek():
    """
    Make sure that `fetch_peek` is set properly on FETCH commans
    """
    assert False
