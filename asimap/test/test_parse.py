#!/usr/bin/env python
#
# Copyright (C) 2005 Eric "Scanner" Luce
#
# File: $Id: imapparse_test.py 1456 2007-12-16 07:48:08Z scanner $
#
"""
Test the asimap IMAP message parser.
"""
from ..fetch import FetchOp
from ..parse import IMAPClientCommand


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
def test_fetch_peek(good_received_imap_messages):
    """
    Make sure that `fetch_peek` is set properly on FETCH commans
    """
    for msg, result in good_received_imap_messages:
        # Skip messages that are not FETCH
        #
        if "FETCH" not in msg:
            continue
        p = IMAPClientCommand(msg)
        p.parse()

        # If the message has the string BODY in it but not the string PEEK
        # `fetch_peek` will be False. Otherwise it should be True.
        #
        # One exception that complicates this simple rule: a naked `BODY` fetch
        # att as this is not `BODY` but `BODYSTRUCTURE` (and thus peek == True)
        #
        expected = True
        if "BODY" in msg and "PEEK" not in msg:
            expected = False
            bs_found = False
            b_found = False
            for fetch_att in p.fetch_atts:
                match fetch_att.attribute:
                    case FetchOp.BODYSTRUCTURE:
                        bs_found = True
                    case FetchOp.BODY:
                        if not fetch_att.peek:
                            b_found = True

            # If we found a bodystructure but no body without peek then we
            # expect peek to be True.
            #
            if bs_found and not b_found:
                expected = True

        assert p.fetch_peek == expected
