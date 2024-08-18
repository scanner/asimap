"""
Fetch.. the part that gets various bits and pieces of messages.
"""

# System imports
#
import random
from collections import defaultdict
from datetime import datetime, timezone
from email import message_from_string
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any, Dict, List, Tuple, cast

# 3rd party imports
#
import pytest

# Project imports
#
from ..constants import REVERSE_SYSTEM_FLAG_MAP, SYSTEM_FLAGS
from ..fetch import STR_TO_FETCH_OP, FetchAtt, FetchOp
from ..generator import msg_as_string, msg_headers_as_string
from ..mbox import mbox_msg_path
from ..parse import _lit_ref_re
from ..search import SearchContext
from ..utils import UID_HDR, get_uidvv_uid, parsedate
from .conftest import assert_email_equal


####################################################################
#
def test_fetch_create_and_str():
    """
    The FetchAtt's __str__ method is part of the reply to the IMAP client,
    so we need to make sure we get back the right strings for the right
    fetches.
    """
    # We are going to create a bunch of FetchAtt objects. These are the args,
    # kwargs for those objects as well as the expected `str()` of the objects.
    #
    inputs: List[Tuple[str, Dict[str, Any], str]] = [
        ("body", {"section": [], "actual_command": "RFC822"}, "RFC822"),
        ("rfc822.size", {}, "RFC822.SIZE"),
        (
            "body",
            {
                "section": ["header"],
                "peek": True,
                "actual_command": "RFC822.HEADER",
            },
            "RFC822.HEADER",
        ),
        (
            "body",
            {"section": ["text"], "actual_command": "RFC822.TEXT"},
            "RFC822.TEXT",
        ),
        (
            "bodystructure",
            {"ext_data": False, "actual_command": "BODY"},
            "BODY",
        ),
        ("body", {}, "BODY"),
        ("body", {"section": [1, 2, 3, 4, "header"]}, "BODY[1.2.3.4.HEADER]"),
        ("body", {"section": [3, "text"]}, "BODY[3.TEXT]"),
        ("body", {"section": [], "partial": (0, 1024)}, "BODY[]<0.1024>"),
        ("flags", {}, "FLAGS"),
        ("internaldate", {}, "INTERNALDATE"),
        ("envelope", {}, "ENVELOPE"),
        ("uid", {}, "UID"),
    ]

    for fetch_op, kwargs, expected in inputs:
        f = FetchAtt(STR_TO_FETCH_OP[fetch_op], **kwargs)
        assert str(f) == expected


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_body(mailbox_with_mimekit_email):
    mbox = mailbox_with_mimekit_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
        msg = await ctx.email_message()
        msg_size = await ctx.msg_size()
        fetch = FetchAtt(FetchOp.BODY)
        result = await fetch.fetch(ctx)

        assert result.startswith("BODY {")
        m = _lit_ref_re.search(result)
        assert m
        result_msg_size = int(m.group(1))
        assert result_msg_size == msg_size

        msg_start_idx = result.find("}\r\n") + 3
        data = result[msg_start_idx : msg_start_idx + result_msg_size]
        result_msg = cast(
            EmailMessage, message_from_string((data), policy=SMTP)
        )
        assert_email_equal(msg, result_msg)


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_bodystructure(mailbox_with_mimekit_email):
    mbox = mailbox_with_mimekit_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # XXX Yeah.. should move these in data files and have it come out with the
    #     "mimiekit_email" fixtures..
    #
    expecteds = [
        '("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1 NIL NIL NIL NIL)',
        '("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1 NIL NIL NIL NIL)',
        '(("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1 NIL NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1 NIL NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "Next_Alternative") NIL NIL NIL)',
        '(("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1 NIL NIL NIL NIL)("IMAGE" "GIF" ("NAME" "empty.gif") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.gif")) NIL NIL)("IMAGE" "JPEG" ("NAME" "empty.jpg") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.jpg")) NIL NIL) "RELATED" ("BOUNDARY" "Next_Related") NIL NIL NIL)',
        '((("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1 NIL NIL NIL NIL)("IMAGE" "GIF" ("NAME" "empty.gif") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.gif")) NIL NIL)("IMAGE" "JPEG" ("NAME" "empty.jpg") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.jpg")) NIL NIL) "RELATED" ("BOUNDARY" "Next_Related") NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1 NIL NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "Next_Alternative") NIL NIL NIL)',
        '(((("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1 NIL NIL NIL NIL)("IMAGE" "GIF" ("NAME" "empty.gif") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.gif")) NIL NIL)("IMAGE" "JPEG" ("NAME" "empty.jpg") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.jpg")) NIL NIL) "RELATED" ("BOUNDARY" "Next_Related") NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1 NIL NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "Next_Alternative") NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII" "NAME" "document.txt") NIL NIL "7BIT" 31 1 NIL ("ATTACHMENT" ("FILENAME" "document.txt")) NIL NIL) "MIXED" ("BOUNDARY" "Next_Mixed") NIL NIL NIL)',
        '(((("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1 NIL NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1 NIL NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "Next_Alternative") NIL NIL NIL)("IMAGE" "GIF" ("NAME" "empty.gif") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.gif")) NIL NIL)("IMAGE" "JPEG" ("NAME" "empty.jpg") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.jpg")) NIL NIL) "RELATED" ("BOUNDARY" "Next_Related") NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII" "NAME" "document.txt") NIL NIL "7BIT" 31 1 NIL ("ATTACHMENT" ("FILENAME" "document.txt")) NIL NIL) "MIXED" ("BOUNDARY" "Next_Mixed") NIL NIL NIL)',
        '(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1 NIL NIL NIL NIL)("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 65 1 NIL NIL NIL NIL)("APPLICATION" "OCTET-STREAM" ("NAME" "attachment.dat") NIL NIL "7BIT" 2 NIL ("ATTACHMENT" ("FILENAME" "attachment.dat")) NIL NIL) "MIXED" ("BOUNDARY" "Next_Mixed") NIL NIL NIL)',
        '(("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1 NIL NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 46 1 NIL NIL NIL NIL)("APPLICATION" "OCTET-STREAM" ("NAME" "attachment.dat") NIL NIL "7BIT" 2 NIL ("ATTACHMENT" ("FILENAME" "attachment.dat")) NIL NIL) "MIXED" ("BOUNDARY" "Next_Mixed") NIL NIL NIL)',
        """(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL "Notification" "base64" 1868 24 NIL NIL NIL NIL)(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "base64" 2 1 NIL NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1 NIL NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1 NIL NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 724 12 NIL NIL NIL NIL) "DELIVERY-STATUS" ("CHARSET" "US-ASCII") NIL NIL NIL)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL "Undelivered Message" "7BIT" 17961 ("Wed, 26 Jan 2022 09:06:45 +0000" "FILE-SRV | FILE-SRV | Ellern Mede | eventlog | Security - Microsoft-Windows-Security-Auditing | Code: '4625' - Type: 'Critical, Error, Warning, , Information, Verbose' - Desc: '' {44565655}" (("Netec RMM" NIL "helpdesk" "netecgc.com")) (("Netec RMM" NIL "helpdesk" "netecgc.com")) ((NIL NIL "helpdesk" "netecgc.com")) ((NIL NIL "netec.test" "netecgc.com")) NIL NIL NIL "<44565655.JitbitHelpdesk.13439.691a0afc-a0a0-457c-8208-f20b7c4a4cb1@jitbit.com>") (("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "base64" 5452 88 NIL NIL NIL NIL)("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "base64" 11254 145 NIL NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "=-JG9iIL0Fmro08hOsLcfbiQ==") NIL NIL NIL) 266 NIL NIL NIL NIL) "REPORT" ("REPORT-TYPE" "delivery-status" "BOUNDARY" "630A242E63.1643188008/hmail.jitbit.com") NIL NIL NIL)""",
        """((("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1 NIL NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1 NIL NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1 NIL NIL NIL NIL) "DELIVERY-STATUS" ("CHARSET" "US-ASCII") NIL NIL NIL)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 840 ("Mon, 29 Jul 1996 02:04:52 -0700" "unsubscribe" (("Jamie Zawinski" NIL "jwz" "netscape.com")) ((NIL NIL "jwz" "netscape.com")) (("Jamie Zawinski" NIL "jwz" "netscape.com")) ((NIL NIL "newsletter-request" "imusic.com")) NIL NIL NIL "<31FC7EB4.41C6@netscape.com>") ("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7bit" 13 1 NIL NIL NIL NIL) 19 NIL NIL NIL NIL) "REPORT" ("REPORT-TYPE" "delivery-status" "BOUNDARY" "A41C7.838631588=_/mm1") NIL NIL NIL)""",
        """((("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1 NIL NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1 NIL NIL NIL NIL) "DELIVERY-STATUS" ("CHARSET" "US-ASCII") NIL NIL NIL)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 840 ("Mon, 29 Jul 1996 02:04:52 -0700" "unsubscribe" (("Jamie Zawinski" NIL "jwz" "netscape.com")) ((NIL NIL "jwz" "netscape.com")) (("Jamie Zawinski" NIL "jwz" "netscape.com")) ((NIL NIL "newsletter-request" "imusic.com")) NIL NIL NIL "<31FC7EB4.41C6@netscape.com>") ("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7bit" 13 1 NIL NIL NIL NIL) 19 NIL NIL NIL NIL) "REPORT" ("REPORT-TYPE" "delivery-status" "BOUNDARY" "A41C7.838631588=_/mm1") NIL NIL NIL)""",
        """(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 230 4 NIL NIL NIL NIL)(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1 NIL NIL NIL NIL) "DISPOSITION-NOTIFICATION" ("CHARSET" "US-ASCII") NIL NIL NIL)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 358 ("Tue, 19 Sep 1995 13:30:00 -0000" "First draft of report" (("Jane Sender" NIL "Jane_Sender" "example.com")) (("Jane Sender" NIL "Jane_Sender" "example.com")) (("Jane Sender" NIL "Jane_Sender" "example.com")) (("Joe Recipient" NIL "Joe_Recipient" "example.com")) NIL NIL NIL "<199509192301.23456@example.org>") ("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 96 5 NIL NIL NIL NIL) 13 NIL NIL NIL NIL) "REPORT" ("REPORT-TYPE" "disposition-notification" "BOUNDARY" "RAA14128.773615765/example.com") NIL NIL NIL)""",
        '(("MULTIPART" "ALTERNATIVE" ("BOUNDARY" "----=_NextPart_001_0040_01CE98CE.6E826F90") NIL NIL "7BIT" 4 NIL NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 85 1 NIL NIL NIL NIL) "MIXED" ("BOUNDARY" "----=_NextPart_000_003F_01CE98CE.6E826F90") NIL NIL NIL)',
        # '(("MULTIPART" "ALTERNATIVE" ("BOUNDARY" "----=_NextPart_001_0040_01CE98CE.6E826F90") NIL NIL "7BIT" 5)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 85 1) "MIXED" ("BOUNDARY" "----=_NextPart_000_003F_01CE98CE.6E826F90") NIL NIL NIL)',
        '(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 89 3 NIL NIL NIL NIL)("APPLICATION" "POSTSCRIPT" NIL NIL NIL "base64" 2 NIL NIL NIL NIL) "MIXED" ("BOUNDARY" "NutNews,-a-nntpmtsonsguinrcfas,-boundary") NIL NIL NIL)',
        '(("TEXT" "PLAIN" ("CHARSET" "ISO-8859-1") NIL NIL "quoted-printable" 16 2 NIL NIL NIL NIL)("TEXT" "HTML" ("CHARSET" "ISO-8859-1") NIL NIL "quoted-printable" 494 12 NIL NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "----=_NextPart_000_0031_01D36222.8A648550") NIL ("en-US" "it-IT") NIL)',
        '("TEXT" "PLAIN" ("CHARSET" "ISO-2022-JP") NIL NIL "7bit" 117 6 NIL NIL NIL NIL)',
        '("TEXT" "PLAIN" ("CHARSET" "US-ASCII" "NAME" "document.xml.gz") NIL NIL "7BIT" 16 1 NIL NIL NIL NIL)',
        '("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 354 ("Sun, 12 Aug 2012 12:34:56 +0300" "submsg" ((NIL NIL "sub" "domain.org")) ((NIL NIL "sub" "domain.org")) ((NIL NIL "sub" "domain.org")) NIL NIL NIL NIL NIL) (("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 46 (NIL "m1" ((NIL NIL "m1" "example.com")) ((NIL NIL "m1" "example.com")) ((NIL NIL "m1" "example.com")) NIL NIL NIL NIL NIL) ("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 9 1 NIL NIL NIL NIL) 4 NIL NIL NIL NIL)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 46 (NIL "m2" ((NIL NIL "m2" "example.com")) ((NIL NIL "m2" "example.com")) ((NIL NIL "m2" "example.com")) NIL NIL NIL NIL NIL) ("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 9 1 NIL NIL NIL NIL) 4 NIL NIL NIL NIL) "DIGEST" ("BOUNDARY" "foo") NIL NIL NIL) 27 NIL NIL NIL NIL)',
        '(("IMAGE" "PNG" NIL NIL NIL "base64" 1220 NIL NIL NIL "image1")("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "quoted-printable" 930 13 NIL NIL NIL NIL) "RELATED" ("TYPE" "text/html" "BOUNDARY" "----=_NextPart_115e1404-dbbc-4611-b4ce-d08a4b021c45") NIL NIL NIL)',
        '(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 34 1 NIL NIL NIL NIL)("APPLICATION" "OCTET-STREAM" NIL NIL NIL "7BIT" 34 NIL NIL NIL NIL)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 320 (NIL "This part specifier should be: 3" ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) NIL NIL NIL NIL) (("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 36 1 NIL NIL NIL NIL)("APPLICATION" "OCTET-STREAM" NIL NIL NIL "7BIT" 36 NIL NIL NIL NIL) "MIXED" ("BOUNDARY" "3.x") NIL NIL NIL) 17 NIL NIL NIL NIL)(("IMAGE" "GIF" NIL NIL NIL "7BIT" 36 NIL NIL NIL NIL)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 491 (NIL "This part specifier should be: 4.2" ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) NIL NIL NIL NIL) (("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 38 1 NIL NIL NIL NIL)(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 40 1 NIL NIL NIL NIL)("TEXT" "RICHTEXT" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 40 1 NIL NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "4.2.2.x") NIL NIL NIL) "MIXED" ("BOUNDARY" "4.2.x") NIL NIL NIL) 27 NIL NIL NIL NIL) "MIXED" ("BOUNDARY" "4.x") NIL NIL NIL) "MIXED" ("BOUNDARY" "x") NIL NIL NIL)',
        '("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 16 2 NIL NIL NIL NIL)',
    ]
    # for an unpacked folder with one message in it the message key and the
    # message index are the same value.
    #
    for msg_idx, (msg_key, expected) in enumerate(zip(msg_keys, expecteds)):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
        fetch = FetchAtt(FetchOp.BODYSTRUCTURE)
        result = await fetch.fetch(ctx)

        assert result.startswith("BODYSTRUCTURE ")
        if result[14:] != expected:
            pytest.fail(
                f"Message {msg_key} failed fetch: result {result[14:]} != expected {expected}"
            )
        # assert result[14:] == expected


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_envelope(mailbox_with_mimekit_email):
    mbox = mailbox_with_mimekit_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    expecteds = [
        """("Sat, 02 Jan 2016 17:42:00 -0400" "MimeMessage.TextBody and HtmlBody tests" (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) NIL NIL NIL NIL)""",
        """("Sat, 02 Jan 2016 17:42:00 -0400" "MimeMessage.TextBody and HtmlBody tests" (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) NIL NIL NIL NIL)""",
        """("Sat, 02 Jan 2016 17:42:00 -0400" "MimeMessage.TextBody and HtmlBody tests" (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) NIL NIL NIL NIL)""",
        """("Sat, 02 Jan 2016 17:42:00 -0400" "MimeMessage.TextBody and HtmlBody tests" (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) NIL NIL NIL NIL)""",
        """("Sat, 02 Jan 2016 17:42:00 -0400" "MimeMessage.TextBody and HtmlBody tests" (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) NIL NIL NIL NIL)""",
        """("Sat, 02 Jan 2016 17:42:00 -0400" "MimeMessage.TextBody and HtmlBody tests" (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) NIL NIL NIL NIL)""",
        """("Sat, 02 Jan 2016 17:42:00 -0400" "MimeMessage.TextBody and HtmlBody tests" (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) NIL NIL NIL NIL)""",
        """("Sat, 02 Jan 2016 17:42:00 -0400" "MimeMessage.TextBody and HtmlBody tests" (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) NIL NIL NIL NIL)""",
        """("Sat, 02 Jan 2016 17:42:00 -0400" "MimeMessage.TextBody and HtmlBody tests" (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) (("MimeKit Unit Tests" NIL "mimekit" "mimekit.net")) NIL NIL NIL NIL)""",
        """("Wed, 26 Jan 2022 04:06:48 -0500" "Undelivered Mail Returned to Sender" ((NIL NIL "MAILER-DAEMON" "hmail.jitbit.com")) ((NIL NIL "MAILER-DAEMON" "hmail.jitbit.com")) ((NIL NIL "MAILER-DAEMON" "hmail.jitbit.com")) ((NIL NIL "helpdesk" "netecgc.com")) NIL NIL NIL "<20220126090648.12632412E9@hmail.jitbit.com>")""",
        """("Mon, 29 Jul 1996 02:13:08 -0700" "email delivery error" (("The Post Office" NIL "postmaster" "mm1.sprynet.com")) (("The Post Office" NIL "postmaster" "mm1.sprynet.com")) (("The Post Office" NIL "postmaster" "mm1.sprynet.com")) ((NIL NIL "noone" "example.net")) (("The Postmaster" NIL "postmaster" "mm1.sprynet.com")) NIL NIL "<96Jul29.022158-0700pdt.148226-12799+708@mm1.sprynet.com>")""",
        """("Mon, 29 Jul 1996 02:13:08 -0700" "email delivery error" (("The Post Office" NIL "postmaster" "mm1.sprynet.com")) (("The Post Office" NIL "postmaster" "mm1.sprynet.com")) (("The Post Office" NIL "postmaster" "mm1.sprynet.com")) ((NIL NIL "noone" "example.net")) (("The Postmaster" NIL "postmaster" "mm1.sprynet.com")) NIL NIL "<96Jul29.022158-0700pdt.148226-12799+708@mm1.sprynet.com>")""",
        """("Wed, 20 Sep 1995 00:19:00 -0000" "Disposition notification" (("Joe Recipient" NIL "Joe_Recipient" "example.com")) (("Joe Recipient" NIL "Joe_Recipient" "example.com")) (("Joe Recipient" NIL "Joe_Recipient" "example.com")) (("Jane Sender" NIL "Jane_Sender" "example.org")) NIL NIL NIL "<199509200019.12345@example.com>")""",
        """("Tue, 12 Nov 2013 09:12:42 -0500" "test of empty multipart/alternative" ((NIL NIL "mimekit" "example.com")) ((NIL NIL "mimekit" "example.com")) ((NIL NIL "mimekit" "example.com")) ((NIL NIL "mimekit" "example.com")) NIL NIL NIL "<54AD68C9E3B0184CAC6041320424FD1B5B81E74D@localhost.localdomain>")""",
        """("Sun, 07 May 1995 16:21:03 +0000" "Re: The Once and Future OS" (("Peter Urka" NIL "pcu" "umich.edu")) ((NIL NIL "preston" "urkabox.chem.lsa.umich.edu")) ((NIL NIL "pcu" "umich.edu")) NIL NIL NIL NIL "<07May1621030321@urkabox.chem.lsa.umich.edu>")""",
        """("Wed, 15 Nov 2017 14:16:14 +0000" "R: R: R: I: FR-selca LA selcaE" ((NIL NIL "jang.abcdef" "xyzlinu")) ((NIL NIL "jang.abcdef" "xyzlinu")) ((NIL NIL "jang.abcdef" "xyzlinu")) (("jang12@linux12.org.new" NIL "jang12" "linux12.org.new")) NIL NIL "<5185e377-81c5-4361-91ba-11d42f4c5cc9@AM5EUR02FT056.eop-EUR02.prod.protection.outlook.com>" "<AM4PR01MB1444B3F21AE7DA9C8128C28FF7290@AM4PR01MB1444.eurprd01.prod.exchangelabs.com>")""",
        """("Wed, 22 Jul 2015 01:02:29 +0900" "日本語メールテスト (testing Japanese emails)" (("Atsushi Eno" NIL "x" "x.com")) (("Atsushi Eno" NIL "x" "x.com")) (("Atsushi Eno" NIL "x" "x.com")) (("Jeffrey Stedfast" NIL "x" "x.com")) NIL NIL NIL "<55AE6D15.4010805@veritas-vos-liberabit.com>")""",
        """("Tue, 29 Dec 2015 09:06:17 -0400" "Test of an invalid mime-type" (("someone" NIL "someone" "somewhere.com")) (("someone" NIL "someone" "somewhere.com")) (("someone" NIL "someone" "somewhere.com")) (("someone else" NIL "someone.else" "somewhere.else.com")) NIL NIL NIL NIL)""",
        """("Sat, 24 Mar 2007 23:00:00 +0200" NIL ((NIL NIL "user" "domain.org")) ((NIL NIL "user" "domain.org")) ((NIL NIL "user" "domain.org")) NIL NIL NIL NIL NIL)""",
        """(NIL NIL NIL NIL NIL NIL NIL NIL NIL NIL)""",
        """(NIL "Sample message structure for IMAP part specifiers" ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) NIL NIL NIL NIL)""",
        """("Fri, 03 Nov 2017 12:00:00 -0800" "Message Subject" (("test" NIL "test" "test.com")) (("test" NIL "test" "test.com")) (("test" NIL "test" "test.com")) (("test" NIL "test" "test.com")(NIL NIL "date" NIL)) NIL NIL NIL "<aasfasdfasdfa@bb>")""",
    ]
    for msg_idx, (msg_key, expected) in enumerate(zip(msg_keys, expecteds)):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
        fetch = FetchAtt(FetchOp.ENVELOPE)
        result = await fetch.fetch(ctx)

        assert result.startswith("ENVELOPE ")
        assert result[9:] == expected


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_rfc822_size(mailbox_with_mimekit_email):
    mbox = mailbox_with_mimekit_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    expecteds = [
        291,
        309,
        491,
        632,
        816,
        1056,
        1056,
        624,
        624,
        # 28244,   # why did our body rfc822.size change from 28242 to 28244?
        28242,
        2479,
        2477,
        1479,
        757,
        1259,
        9434,
        623,
        302,
        505,
        2724,
        1391,
        138447,
    ]

    for msg_idx, (msg_key, expected) in enumerate(zip(msg_keys, expecteds)):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
        fetch = FetchAtt(FetchOp.RFC822_SIZE)
        result = await fetch.fetch(ctx)

        assert result.startswith("RFC822.SIZE ")
        assert int(result[12:]) == expected


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_flags(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # Set some flags on the messages
    msgs_by_flag: Dict[str, List[int]] = {}
    flags_by_msg: Dict[int, List[str]] = defaultdict(list)
    for flag in SYSTEM_FLAGS:
        msgs_by_flag[flag] = random.sample(msg_keys, k=5)
        for k in msgs_by_flag[flag]:
            flags_by_msg[k].append(flag)
        if flag == r"\Seen":
            seqs["unseen"] = list(set(msg_keys) - set(msgs_by_flag[flag]))
            for k in seqs["unseen"]:
                flags_by_msg[k].append("unseen")

        seqs[REVERSE_SYSTEM_FLAG_MAP[flag]] = msgs_by_flag[flag]

    await mbox.mailbox.aset_sequences(seqs)

    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
        fetch = FetchAtt(FetchOp.FLAGS)
        result = await fetch.fetch(ctx)

        assert result.startswith("FLAGS ")
        flags = result[6:]
        assert flags[0] == "(" and flags[-1] == ")"
        assert sorted(flags_by_msg[msg_key]) == sorted(flags[1:-1].split(" "))


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_internaldate(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # Get all the mtime's of the messages in the mailbox and convert these in
    # to non-naive datetimes.
    #
    internal_date_by_msg: Dict[int, datetime] = {}
    for msg_key in msg_keys:
        mtime = int(mbox_msg_path(mbox.mailbox, msg_key).stat().st_mtime)
        internal_date_by_msg[msg_key] = datetime.fromtimestamp(
            mtime, timezone.utc
        )
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
        fetch = FetchAtt(FetchOp.INTERNALDATE)
        result = await fetch.fetch(ctx)

        assert result.startswith("INTERNALDATE ")
        assert result[13] == '"' and result[-1] == '"'
        internal_date = parsedate(result[14:-1])
        assert internal_date == internal_date_by_msg[msg_key]


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_uid(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    uid_by_msg: Dict[int, int] = {}
    for msg_key in msg_keys:
        msg = await mbox.mailbox.aget_message(msg_key)
        uid_vv, uid = get_uidvv_uid(msg[UID_HDR])
        uid_by_msg[msg_key] = uid

    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
        fetch = FetchAtt(FetchOp.UID)
        result = await fetch.fetch(ctx)

        assert result.startswith("UID ")
        assert int(result[4:]) == uid_by_msg[msg_key]


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_body_section_text(mailbox_with_mimekit_email):
    """
    We only need to test one message, with lots of headers.
    """
    mbox = mailbox_with_mimekit_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    msg_key = 10
    msg_idx = 10

    ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
    fetch = FetchAtt(FetchOp.BODY, section=["TEXT"])
    result = await fetch.fetch(ctx)
    email_msg = await ctx.email_message()

    assert result.startswith("BODY[TEXT] {")
    body_start = result.find("}") + 3
    res_length = int(result[result.find("{") + 1 : result.find("}")])
    result_body = result[body_start:]
    assert len(result_body) == res_length

    msg_body = msg_as_string(email_msg, headers=False)

    assert res_length == len(msg_body)
    assert msg_body == result_body

    # section 1 TEXT
    #
    ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
    fetch = FetchAtt(FetchOp.BODY, section=[1, "TEXT"])
    result = await fetch.fetch(ctx)

    assert result.startswith("BODY[1.TEXT] {")
    body_start = result.find("}") + 3
    res_length = int(result[result.find("{") + 1 : result.find("}")])
    result_body = result[body_start:]
    assert len(result_body) == res_length

    msg_parts = email_msg.get_payload()
    msg_body = msg_as_string(msg_parts[0], headers=False)
    assert res_length == len(msg_body)
    assert msg_body == result_body

    # section 2.1.TEXT
    #
    ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
    fetch = FetchAtt(FetchOp.BODY, section=[2, 4, "TEXT"])
    result = await fetch.fetch(ctx)

    assert result.startswith("BODY[2.4.TEXT] {")
    body_start = result.find("}") + 3
    res_length = int(result[result.find("{") + 1 : result.find("}")])
    result_body = result[body_start:]
    assert len(result_body) == res_length
    sub_parts = msg_parts[1].get_payload()
    msg_body = msg_as_string(sub_parts[3], headers=False)

    assert res_length == len(msg_body)
    assert msg_body == result_body


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_body_section_header(mailbox_with_mimekit_email):
    """
    We only need to test one message, with lots of headers.
    """
    mbox = mailbox_with_mimekit_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    msg_key = 10
    msg_idx = 10

    ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
    fetch = FetchAtt(FetchOp.BODY, peek=True, section=["HEADER"])
    result = await fetch.fetch(ctx)
    email_msg = await ctx.email_message()

    assert result.startswith("BODY[HEADER] {")
    headers_start = result.find("}") + 3
    res_length = int(result[result.find("{") + 1 : result.find("}")])
    result_headers = result[headers_start:]

    assert len(result_headers) == res_length

    msg_headers = msg_headers_as_string(email_msg)

    assert res_length == len(msg_headers)
    assert msg_headers == result_headers

    # With or without peek the response is the same.
    #
    ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
    fetch = FetchAtt(FetchOp.BODY, section=["HEADER"])
    result = await fetch.fetch(ctx)

    # And also make sure headers are the same
    #
    result_headers = result[headers_start:]
    assert msg_headers == result_headers

    # headers of sub-parts - 1.HEADER
    #
    ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
    fetch = FetchAtt(FetchOp.BODY, section=[1, "HEADER"])
    result = await fetch.fetch(ctx)

    result_headers = result[headers_start:]
    assert result.startswith("BODY[1.HEADER] {")
    body_start = result.find("}") + 3
    res_length = int(result[result.find("{") + 1 : result.find("}")])
    result_body = result[body_start:]
    assert len(result_body) == res_length

    msg_parts = email_msg.get_payload()
    msg_headers = msg_headers_as_string(msg_parts[0])
    assert res_length == len(msg_headers)
    assert msg_headers == result_body

    # headers of sub-parts - 2.4.HEADER
    #
    ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
    fetch = FetchAtt(FetchOp.BODY, section=[2, 4, "HEADER"])
    result = await fetch.fetch(ctx)

    result_headers = result[headers_start:]
    assert result.startswith("BODY[2.4.HEADER] {")
    body_start = result.find("}") + 3
    res_length = int(result[result.find("{") + 1 : result.find("}")])
    result_body = result[body_start:]
    assert len(result_body) == res_length
    # NOTE: zero-based array vs 1-based section list so 2.4 is index 1, index 3
    #
    sub_parts = msg_parts[1].get_payload()
    msg_headers = msg_headers_as_string(sub_parts[3])

    assert res_length == len(msg_headers)
    assert msg_headers == result_body


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_body_section_header_fields(mailbox_with_mimekit_email):
    mbox = mailbox_with_mimekit_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    msg_key = 10
    msg_idx = 10

    headers = ["date", "subject", "from", "to", "cc", "message-id"]
    ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
    fetch = FetchAtt(FetchOp.BODY, section=[["HEADER.FIELDS", headers]])
    result = await fetch.fetch(ctx)
    email_msg = await ctx.email_message()

    assert result.startswith(f"BODY[HEADER.FIELDS ({' '.join(headers)})] {{")
    headers_start = result.find("}") + 3
    res_length = int(result[result.find("{") + 1 : result.find("}")])
    result_headers = result[headers_start:]

    assert len(result_headers) == res_length

    msg_headers = msg_headers_as_string(
        email_msg, headers=tuple(headers), skip=False
    )

    assert res_length == len(msg_headers)
    assert msg_headers == result_headers

    # HEADER.FIELDS.NOT...
    #
    ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
    fetch = FetchAtt(FetchOp.BODY, section=[["HEADER.FIELDS.NOT", headers]])
    result = await fetch.fetch(ctx)
    email_msg = await ctx.email_message()

    assert result.startswith(
        f"BODY[HEADER.FIELDS.NOT ({' '.join(headers)})] {{"
    )
    headers_start = result.find("}") + 3
    res_length = int(result[result.find("{") + 1 : result.find("}")])
    result_headers = result[headers_start:]

    assert len(result_headers) == res_length

    msg_headers = msg_headers_as_string(
        email_msg, headers=tuple(headers), skip=True
    )

    assert res_length == len(msg_headers)
    assert msg_headers == result_headers


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_body_text_with_partials(mailbox_with_mimekit_email):
    mbox = mailbox_with_mimekit_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    msg_key = 10
    msg_idx = 10
    ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
    email_msg = await ctx.email_message()
    msg_body = msg_as_string(email_msg, headers=False)
    size = len(msg_body)
    mid = int(size / 2)

    fetch = FetchAtt(FetchOp.BODY, section=["TEXT"], partial=(0, mid))
    result1 = await fetch.fetch(ctx)
    fetch = FetchAtt(FetchOp.BODY, section=["TEXT"], partial=(mid, size))
    result2 = await fetch.fetch(ctx)

    assert result1.startswith(f"BODY[TEXT]<0.{mid}> {{")

    open_brace = result1.find("{") + 1
    close_brace = result1.find("}")
    result1_len = int(result1[open_brace:close_brace])
    result1_msg = result1[close_brace + 3 :]

    assert result1_len == len(result1_msg)
    assert result2.startswith(f"BODY[TEXT]<{mid}.{size}> {{")

    open_brace = result2.find("{") + 1
    close_brace = result2.find("}")
    result2_len = int(result2[open_brace:close_brace])
    result2_msg = result2[close_brace + 3 :]

    assert result2_len == len(result2_msg)
    assert msg_body == result1_msg + result2_msg


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_body_braces(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
        email_msg = await ctx.email_message()
        msg_body = msg_as_string(email_msg)
        size = len(msg_body)
        mid = int(size / 2)
        fetch = FetchAtt(FetchOp.BODY, section=[], partial=(0, mid))
        result1 = await fetch.fetch(ctx)

        fetch = FetchAtt(FetchOp.BODY, section=[], partial=(mid, size))
        result2 = await fetch.fetch(ctx)

        assert result1.startswith(f"BODY[]<0.{mid}> {{")

        open_brace = result1.find("{") + 1
        close_brace = result1.find("}")
        result1_len = int(result1[open_brace:close_brace])
        result1_msg = result1[close_brace + 3 :]

        assert result1_len == len(result1_msg)
        assert result2.startswith(f"BODY[]<{mid}.{size}> {{")

        open_brace = result2.find("{") + 1
        close_brace = result2.find("}")
        result2_len = int(result2[open_brace:close_brace])
        result2_msg = result2[close_brace + 3 :]

        assert result2_len == len(result2_msg)
        assert msg_body == result1_msg + result2_msg
