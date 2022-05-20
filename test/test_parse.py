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

GOOD_MESSAGES = [
    (
        "A003 APPEND saved-messages (\\Seen) {310}\r\nDate: Mon, 7 Feb 1994 "
        "21:52:25 -0800 (PST)\r\nFrom: Fred Foobar <foobar@Blurdybloop.COM>\r\n"
        "Subject: afternoon meeting\r\nTo: mooch@owatagu.siam.edu\r\nMessage-Id:"
        " <B27397-0100000@Blurdybloop.COM>\r\nMIME-Version: 1.0\r\nContent-Type:"
        " TEXT/PLAIN; CHARSET=US-ASCII\r\n\r\nHello Joe, do you think we can "
        "meet at 3:30 tomorrow?\r\n\r\n",
        "A003 APPEND saved-messages",
    ),
    (
        'A003 APPEND saved-messages (\\Seen) "05-jan-1999 20:55:23 +0000" '
        "{310}\r\nDate: Mon, 7 Feb 1994 21:52:25 -0800 (PST)\r\nFrom: Fred "
        "Foobar <foobar@Blurdybloop.COM>\r\nSubject: afternoon meeting\r\nTo: "
        "mooch@owatagu.siam.edu\r\nMessage-Id: <B27397-0100000@Blurdybloop.COM>"
        "\r\nMIME-Version: 1.0\r\nContent-Type: TEXT/PLAIN; CHARSET=US-ASCII"
        "\r\n\r\nHello Joe, do you think we can meet at 3:30 tomorrow?\r\n\r\n",
        "A003 APPEND saved-messages",
    ),
    (
        "A683 RENAME blurdybloop sarasoop\r\n",
        'A683 RENAME "blurdybloop" "sarasoop"',
    ),
    (
        "A999 UID FETCH 4827313:4828442 FLAGS\r\n",
        "A999 UID FETCH (4827313, 4828442) (FLAGS)",
    ),
    (
        "A999 UID SEARCH 1:100 UID 443:557\r\n",
        "A999 UID SEARCH IMAPSearch('and', [IMAPSearch('message_set'), "
        "IMAPSearch('uid')])",
    ),
    ("A202 list ~/Mail/ %\r\n", 'A202 LIST "~/Mail" "%"'),
    ('A101 LIST "" ""\r\n', 'A101 LIST "" ""'),
    ('A103 LIST /usr/staff/jones ""\r\n', 'A103 LIST "/usr/staff/jones" ""'),
    (
        'A102 LIST #news.comp.mail.misc ""\r\n',
        'A102 LIST "#news.comp.mail.misc" ""',
    ),
    (
        "A002 SUBSCRIBE #news.comp.mail.mime\r\n",
        "A002 SUBSCRIBE #news.comp.mail.mime",
    ),
    ("A932 EXAMINE blurdybloop\r\n", "A932 EXAMINE blurdybloop"),
    ("A341 CLOSE\r\n", "A341 CLOSE"),
    ("FXXZ CHECK\r\n", "FXXZ CHECK"),
    ("a001 AUTHENTICATE KERBEROS_V4\r\n", "a001 AUTHENTICATE"),
    ("A003 CREATE owatagusiam/\r\n", "A003 CREATE owatagusiam"),
    (
        "A004 CREATE owatagusiam/blurdybloop\r\n",
        "A004 CREATE owatagusiam/blurdybloop",
    ),
    ("A142 SELECT INBOX\r\n", "A142 SELECT inbox"),
    (
        "A003 STORE 2:4 +FLAGS (\\Deleted)\r\n",
        "A003 STORE (2, 4) +FLAGS (\\Deleted)",
    ),
    ("A003 STORE 2:4 FLAGS \\Seen\r\n", "A003 STORE (2, 4) FLAGS (\\Seen)"),
    (
        "A003 STORE 2:4 -FLAGS.SILENT (\\Seen \\Flagged)\r\n",
        "A003 STORE (2, 4) -FLAGS.SILENT (\\Seen,\\Flagged)",
    ),
    (
        "A042 STATUS blurdybloop (UIDNEXT MESSAGES)\r\n",
        "A042 STATUS blurdybloop (uidnext messages)",
    ),
    (
        "A042 STATUS blurdybloop (RECENT)\r\n",
        "A042 STATUS blurdybloop (recent)",
    ),
    ("1023 logout\r\n", "1023 LOGOUT"),
    (
        'A002 LSUB "#news." "comp.mail.*"\r\n',
        'A002 LSUB "#news." "comp.mail.*"',
    ),
    ("A003 COPY 2:4 MEETING\r\n", "A003 COPY"),
    (
        'A282 SEARCH FLAGGED SINCE 1-Feb-1994 NOT FROM "Smith"\r\n',
        "A282 SEARCH IMAPSearch('and', [IMAPSearch('keyword', keyword = "
        '"\\Flagged"), IMAPSearch(\'since\', date = "1994-02-01 00:00:00+00:00"),'
        " IMAPSearch('not', search_key = IMAPSearch('header', header = "
        '"from", string = "smith"))])',
    ),
    (
        'A282 SEARCH OR FLAGGED SINCE 1-Feb-1994 NOT FROM "Smith"\r\n',
        "A282 SEARCH IMAPSearch('and', [IMAPSearch('or', [IMAPSearch"
        "('keyword', keyword = \"\\Flagged\"), IMAPSearch('since', date = "
        "\"1994-02-01 00:00:00+00:00\")]), IMAPSearch('not', search_key = "
        'IMAPSearch(\'header\', header = "from", string = "smith"))])',
    ),
    (
        "A282 SEARCH (OR FLAGGED 1:3,4,5,6) SINCE 1-Feb-1994 NOT FROM "
        '"Smith"\r\n',
        "A282 SEARCH IMAPSearch('and', [IMAPSearch('or', [IMAPSearch"
        "('keyword', keyword = \"\\Flagged\"), IMAPSearch('message_set')]), "
        "IMAPSearch('since', date = \"1994-02-01 00:00:00+00:00\"), IMAPSearch"
        "('not', search_key = IMAPSearch('header', header = \"from\", string "
        '= "smith"))])',
    ),
    ("a002 noop\r\n", "a002 NOOP"),
    ("A202 EXPUNGE\r\n", "A202 EXPUNGE"),
    ("a002 NOOP\r\n", "a002 NOOP"),
    ("abcd CAPABILITY\r\n", "abcd CAPABILITY"),
    (
        "A002 UNSUBSCRIBE #news.comp.mail.mime\r\n",
        "A002 UNSUBSCRIBE #news.comp.mail.mime",
    ),
    ("a001 login smith sesame\r\n", "a001 LOGIN smith"),
    (
        "a001 login {11}\r\nFRED FOOBAR {7}\r\nfat man\r\n",
        "a001 LOGIN FRED FOOBAR",
    ),
    (
        "A654 FETCH 2:4 (FLAGS BODY[HEADER.FIELDS (DATE FROM)])\r\n",
        "A654 FETCH (2, 4) (FLAGS BODY[HEADER.FIELDS (DATE FROM)])",
    ),
    ("A654 FETCH 2:4 BODY\r\n", "A654 FETCH (2, 4) (BODY)"),
    (
        "A654 FETCH 2:4 BODY[]<0.2048>\r\n",
        "A654 FETCH (2, 4) (BODY[]<0.2048>)",
    ),
    (
        "A654 FETCH 2:4 BODY[1.2.3.4.HEADER]\r\n",
        "A654 FETCH (2, 4) (BODY[1.2.3.4.HEADER])",
    ),
    ("A654 FETCH 2:4 BODY[HEADER]\r\n", "A654 FETCH (2, 4) (BODY[HEADER])"),
    ("A654 FETCH 2:4 BODY[TEXT]\r\n", "A654 FETCH (2, 4) (BODY[TEXT])"),
    ("A654 FETCH 2:4 BODY[1]\r\n", "A654 FETCH (2, 4) (BODY[1])"),
    (
        "A654 FETCH 2:4 BODY[3.HEADER]\r\n",
        "A654 FETCH (2, 4) (BODY[3.HEADER])",
    ),
    ("A654 FETCH 2:4 BODY[3.TEXT]\r\n", "A654 FETCH (2, 4) (BODY[3.TEXT])"),
    ("A654 FETCH 2:4 BODY[3.1]\r\n", "A654 FETCH (2, 4) (BODY[3.1])"),
    (
        "A654 FETCH 2:4 BODY[4.1.MIME]\r\n",
        "A654 FETCH (2, 4) (BODY[4.1.MIME])",
    ),
    (
        "A654 FETCH 2:4 BODY[4.2.HEADER]\r\n",
        "A654 FETCH (2, 4) (BODY[4.2.HEADER])",
    ),
    ("A654 FETCH 2:4 BODY[4.2.2.1]\r\n", "A654 FETCH (2, 4) (BODY[4.2.2.1])"),
    ("A683 DELETE blurdybloop\r\n", "A683 DELETE blurdybloop"),
    ("A685 DELETE foo/bar\r\n", "A685 DELETE foo/bar"),
    ("A684 DELETE foo\r\n", "A684 DELETE foo"),
]


####################################################################
#
def test_parse_good_messages():
    """
    Test the set of messages we know should succeed and what we expect
    the parsed result to print out as.
    """
    for msg, result in GOOD_MESSAGES:
        p = IMAPClientCommand(msg)
        p.parse()
        assert str(p) == result
