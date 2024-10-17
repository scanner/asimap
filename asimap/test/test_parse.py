#!/usr/bin/env python
#
# Copyright (C) 2005 Eric "Scanner" Luce
#
# File: $Id: imapparse_test.py 1456 2007-12-16 07:48:08Z scanner $
#
"""
Test the asimap IMAP message parser.
"""
import pytest

from ..fetch import FetchOp
from ..parse import IMAPClientCommand

IMAP_MESSAGES = [
    pytest.param(
        "A003 APPEND saved-messages (\\Seen) {310}\r\nDate: Mon, 7 Feb 1994 21:52:25 -0800 (PST)\r\nFrom: Fred Foobar <foobar@Blurdybloop.COM>\r\nSubject: afternoon meeting\r\nTo: mooch@owatagu.siam.edu\r\nMessage-Id: <B27397-0100000@Blurdybloop.COM>\r\nMIME-Version: 1.0\r\nContent-Type: TEXT/PLAIN; CHARSET=US-ASCII\r\n\r\nHello Joe, do you think we can meet at 3:30 tomorrow?\r\n\r\n",
        "A003 APPEND saved-messages",
        id="APPEND no datetime",
    ),
    pytest.param(
        'A003 APPEND saved-messages (\\Seen) "05-jan-1999 20:55:23 +0000" {310}\r\nDate: Mon, 7 Feb 1994 21:52:25 -0800 (PST)\r\nFrom: Fred Foobar <foobar@Blurdybloop.COM>\r\nSubject: afternoon meeting\r\nTo: mooch@owatagu.siam.edu\r\nMessage-Id: <B27397-0100000@Blurdybloop.COM>\r\nMIME-Version: 1.0\r\nContent-Type: TEXT/PLAIN; CHARSET=US-ASCII\r\n\r\nHello Joe, do you think we can meet at 3:30 tomorrow?\r\n\r\n',
        "A003 APPEND saved-messages",
        id="APPEND with datetime ",
    ),
    pytest.param(
        "A683 RENAME blurdybloop sarasoop\r\n",
        'A683 RENAME "blurdybloop" "sarasoop"',
        id="RENAME",
    ),
    pytest.param(
        "A999 UID SEARCH 1:100 UID 443:557\r\n",
        "A999 UID SEARCH IMAPSearch('and', [IMAPSearch('message_set', msg_set = [(1, 100)]), IMAPSearch('uid')])",
        id="UID SEARCH",
    ),
    pytest.param(
        "A202 list ~/Mail/ %\r\n",
        'A202 LIST "~/Mail" "%"',
        id="LIST with path 1",
    ),
    pytest.param('A101 LIST "" ""\r\n', 'A101 LIST "" ""', id="LIST"),
    pytest.param(
        'A103 LIST /usr/staff/jones ""\r\n',
        'A103 LIST "/usr/staff/jones" ""',
        id="LIST with path 2",
    ),
    pytest.param(
        'A102 LIST #news.comp.mail.misc ""\r\n',
        'A102 LIST "#news.comp.mail.misc" ""',
        id="LIST with news groups",
    ),
    pytest.param(
        "A002 SUBSCRIBE #news.comp.mail.mime\r\n",
        "A002 SUBSCRIBE #news.comp.mail.mime",
        id="SUBSCRIBE with news groups",
    ),
    pytest.param(
        "A932 EXAMINE blurdybloop\r\n", "A932 EXAMINE blurdybloop", id="EXAMINE"
    ),
    pytest.param("A341 CLOSE\r\n", "A341 CLOSE", id="CLOSE"),
    pytest.param("FXXZ CHECK\r\n", "FXXZ CHECK", id="CHECK"),
    pytest.param(
        "a001 AUTHENTICATE KERBEROS_V4\r\n",
        "a001 AUTHENTICATE",
        id="AUTHENTICATE",
    ),
    pytest.param(
        "A003 CREATE owatagusiam/\r\n", "A003 CREATE owatagusiam", id="CREATE"
    ),
    pytest.param(
        "A004 CREATE owatagusiam/blurdybloop\r\n",
        "A004 CREATE owatagusiam/blurdybloop",
        id="CREATE sub-folder",
    ),
    pytest.param("A142 SELECT INBOX\r\n", "A142 SELECT inbox", id="SELECT"),
    pytest.param(
        "A003 STORE 2:4 +FLAGS (\\Deleted)\r\n",
        "A003 STORE 2:4 +FLAGS (\\Deleted)",
        id="STORE add flags",
    ),
    pytest.param(
        "A003 STORE 2:4 FLAGS \\Seen\r\n",
        "A003 STORE 2:4 FLAGS (\\Seen)",
        id="STORE set flags",
    ),
    pytest.param(
        "A003 STORE 2:4 -FLAGS.SILENT (\\Seen \\Flagged)\r\n",
        "A003 STORE 2:4 -FLAGS.SILENT (\\Seen,\\Flagged)",
        id="STORE remove flags",
    ),
    pytest.param(
        "A042 STATUS blurdybloop (UIDNEXT MESSAGES)\r\n",
        "A042 STATUS blurdybloop (uidnext messages)",
        id="STATUS uidnext, messages",
    ),
    pytest.param(
        "A042 STATUS blurdybloop (RECENT)\r\n",
        "A042 STATUS blurdybloop (recent)",
        id="STATUS recent",
    ),
    pytest.param("1023 logout\r\n", "1023 LOGOUT", id="LOGOUT"),
    pytest.param(
        'A002 LSUB "#news." "comp.mail.*"\r\n',
        'A002 LSUB "#news." "comp.mail.*"',
        id="LSUB news groups",
    ),
    pytest.param(
        "A003 COPY 2:4 MEETING\r\n", "A003 COPY 2:4 MEETING", id="COPY"
    ),
    pytest.param(
        'A282 SEARCH FLAGGED SINCE 1-Feb-1994 NOT FROM "Smith"\r\n',
        "A282 SEARCH IMAPSearch('and', [IMAPSearch('keyword', keyword = \"\\Flagged\"), IMAPSearch('since', date = \"1994-02-01\"), IMAPSearch('not', search_key = IMAPSearch('header', header = \"from\", string = \"smith\"))])",
        id="SEARCH 01",
    ),
    pytest.param(
        'A282 SEARCH OR FLAGGED SINCE 1-Feb-1994 NOT FROM "Smith"\r\n',
        "A282 SEARCH IMAPSearch('and', [IMAPSearch('or', [IMAPSearch('keyword', keyword = \"\\Flagged\"), IMAPSearch('since', date = \"1994-02-01\")]), IMAPSearch('not', search_key = IMAPSearch('header', header = \"from\", string = \"smith\"))])",
        id="SEARCH 02",
    ),
    pytest.param(
        'A282 SEARCH (OR FLAGGED 1:3,4,5,6) SINCE 1-Feb-1994 NOT FROM "Smith"\r\n',
        "A282 SEARCH IMAPSearch('and', [IMAPSearch('or', [IMAPSearch('keyword', keyword = \"\\Flagged\"), IMAPSearch('message_set', msg_set = [(1, 3), 4, 5, 6])]), IMAPSearch('since', date = \"1994-02-01\"), IMAPSearch('not', search_key = IMAPSearch('header', header = \"from\", string = \"smith\"))])",
        id="SEARCH 03",
    ),
    pytest.param("a002 noop\r\n", "a002 NOOP", id="NOOP"),
    pytest.param("A202 EXPUNGE\r\n", "A202 EXPUNGE", id="EXPUNGE"),
    pytest.param("a002 NOOP\r\n", "a002 NOOP", id="NOOP"),  # Repeat?
    pytest.param("abcd CAPABILITY\r\n", "abcd CAPABILITY", id="CAPABILITY"),
    pytest.param(
        "A002 UNSUBSCRIBE #news.comp.mail.mime\r\n",
        "A002 UNSUBSCRIBE #news.comp.mail.mime",
        id="UNSUBSCRIBE news groups",
    ),
    pytest.param(
        "a001 login smith sesame\r\n", "a001 LOGIN smith", id="LOGIN smith"
    ),
    pytest.param(
        "a001 login {11}\r\nFRED FOOBAR {7}\r\nfat man\r\n",
        "a001 LOGIN FRED FOOBAR",
        id="LOGIN fred",
    ),
    pytest.param(
        "A683 DELETE blurdybloop\r\n",
        "A683 DELETE blurdybloop",
        id="DELETE folder",
    ),
    pytest.param(
        "A685 DELETE foo/bar\r\n", "A685 DELETE foo/bar", id="DELETE subfolder"
    ),
    pytest.param(
        "A684 DELETE foo\r\n", "A684 DELETE foo", id="DELETE foo"
    ),  # Repeat?
]

PEEK_IMAP_MESSAGES = [
    pytest.param(
        "A999 UID FETCH 4827313:4828442 FLAGS\r\n",
        "A999 UID FETCH 4827313:4828442 (FLAGS)",
        id="UID FETCH",
    ),
    pytest.param(
        "A001 FETCH 2:4 ALL",
        "A001 FETCH 2:4 (FLAGS INTERNALDATE RFC822.SIZE ENVELOPE)",
        id="FETCH ALL",
    ),
    pytest.param(
        "A001 FETCH 2:4 all",
        "A001 FETCH 2:4 (FLAGS INTERNALDATE RFC822.SIZE ENVELOPE)",
        id="FETCH ALL 2",  # Repeat?
    ),
    pytest.param(
        "A001 FETCH 2:4 FAST",
        "A001 FETCH 2:4 (FLAGS INTERNALDATE RFC822.SIZE)",
        id="FETCH fast",
    ),
    pytest.param(
        "A001 FETCH 2:4 FULL",
        "A001 FETCH 2:4 (FLAGS INTERNALDATE RFC822.SIZE ENVELOPE BODY)",
        id="FETCH full",
    ),
    pytest.param(
        "A654 FETCH 2:4 (FLAGS BODY[HEADER.FIELDS (DATE FROM)])\r\n",
        "A654 FETCH 2:4 (FLAGS BODY[HEADER.FIELDS (DATE FROM)])",
        id="FETCH flags, body headers",
    ),
    pytest.param("A654 FETCH 2:4 BODY\r\n", "A654 FETCH 2:4 (BODY)", id=""),
    pytest.param(
        "A654 FETCH 2:4 BODY[]<0.2048>\r\n",
        "A654 FETCH 2:4 (BODY[]<0.2048>)",
        id="FETCH body partial",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY[1.2.3.4.HEADER]\r\n",
        "A654 FETCH 2:4 (BODY[1.2.3.4.HEADER])",
        id="FETCH BODY sub-section headers",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY[HEADER]\r\n",
        "A654 FETCH 2:4 (BODY[HEADER])",
        id="FETCH BODY headers",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY[TEXT]\r\n",
        "A654 FETCH 2:4 (BODY[TEXT])",
        id="FETCH BODY[TEXT]",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY.PEEK[HEADER]\r\n",
        "A654 FETCH 2:4 (BODY.PEEK[HEADER])",
        id="FETCH BODY.PEEK[HEADER]",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY.PEEK[TEXT]\r\n",
        "A654 FETCH 2:4 (BODY.PEEK[TEXT])",
        id="FETCH BODY.PEEK[TEXT]",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY[1]\r\n",
        "A654 FETCH 2:4 (BODY[1])",
        id="FETCH BODY[1]",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY[3.HEADER]\r\n",
        "A654 FETCH 2:4 (BODY[3.HEADER])",
        id="FETCH BODY[3.HEADER]",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY[3.TEXT]\r\n",
        "A654 FETCH 2:4 (BODY[3.TEXT])",
        id="FETCH BODY[3.TEXT]",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY[3.1]\r\n",
        "A654 FETCH 2:4 (BODY[3.1])",
        id="FETCH BODY[3.1]",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY[4.1.MIME]\r\n",
        "A654 FETCH 2:4 (BODY[4.1.MIME])",
        id="FETCH BODY[4.1.MIME]",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY[4.2.HEADER]\r\n",
        "A654 FETCH 2:4 (BODY[4.2.HEADER])",
        id="FETCH BODY[4.2.HEADER]",
    ),
    pytest.param(
        "A654 FETCH 2:4 BODY[4.2.2.1]\r\n",
        "A654 FETCH 2:4 (BODY[4.2.2.1])",
        id="FETCH BODY[4.2.2.1]",
    ),
]


####################################################################
#
@pytest.mark.parametrize(
    "received,expected", IMAP_MESSAGES + PEEK_IMAP_MESSAGES
)
def test_parse_good_messages(received, expected):
    """
    Test the set of messages we know should succeed and what we expect
    the parsed result to print out as.
    """
    p = IMAPClientCommand(received)
    p.parse()
    assert str(p) == expected


####################################################################
#
@pytest.mark.parametrize("received,expected", PEEK_IMAP_MESSAGES)
def test_fetch_peek(received, expected):
    """
    Make sure that `fetch_peek` is set properly on FETCH commans
    """
    p = IMAPClientCommand(received)
    p.parse()

    # If the message has the string BODY in it but not the string PEEK
    # `fetch_peek` will be False. Otherwise it should be True.
    #
    # One exception that complicates this simple rule: a naked `BODY` fetch
    # att as this is not `BODY` but `BODYSTRUCTURE` (and thus peek == True)
    #
    peek_expected = True
    if "BODY" in received and "PEEK" not in received:
        peek_expected = False
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
            peek_expected = True

    assert p.fetch_peek == peek_expected
