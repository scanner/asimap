"""
Fetch.. the part that gets various bits and pieces of messages.
"""
# System imports
#
from email import message_from_string
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any, Dict, List, Tuple, cast

# 3rd party imports
#
import pytest

from ..fetch import STR_TO_FETCH_OP, FetchAtt, FetchOp

# Project imports
#
from ..parse import _lit_ref_re
from ..search import SearchContext
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
async def test_fetch_body(mailbox_with_big_static_email):
    mbox = mailbox_with_big_static_email
    msg_keys = await mbox.mailbox.akeys()
    msg_key = msg_keys[0]
    seq_max = len(msg_keys)
    sequences = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = await mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # for an unpacked folder with one message in it the message key and the
    # message index are the same value.
    #
    msg_idx = msg_key

    from ..generator import msg_as_string

    async with mbox.lock.read_lock():
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, sequences)
        msg = await ctx.email_message()
        msg_size = await ctx.msg_size()
        fetch = FetchAtt(FetchOp.BODY)
        result = await fetch.fetch(ctx)

    assert result.startswith("BODY {")
    m = _lit_ref_re.search(result)
    assert m
    result_msg_size = int(m.group(1))
    with open("msg1.txt", "w") as f:
        f.write(result[result.find("}\r\n") + 3 :])
    with open("msg2.txt", "w") as f:
        f.write(msg_as_string(msg))
    print(len(result[result.find("}\r\n") + 3 :]))
    assert result_msg_size == msg_size

    msg_start_idx = result.find("}\r\n") + 3
    data = result[msg_start_idx : msg_start_idx + result_msg_size]
    result_msg = cast(EmailMessage, message_from_string((data), policy=SMTP))
    assert_email_equal(msg, result_msg)


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_bodystructure(mailbox_with_mimekit_email):
    mbox = mailbox_with_mimekit_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = await mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # XXX Yeah.. should move these in data files and have it come out with the
    #     "mimiekit_email" fixtures..
    #
    expecteds = [
        '("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1)',
        '("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1)',
        '(("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1) "ALTERNATIVE" ("BOUNDARY" "Next_Alternative") NIL NIL NIL)',
        '(("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1)("IMAGE" "GIF" ("NAME" "empty.gif") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.gif")) NIL NIL)("IMAGE" "JPEG" ("NAME" "empty.jpg") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.jpg")) NIL NIL) "RELATED" ("BOUNDARY" "Next_Related") NIL NIL NIL)',
        '((("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1)("IMAGE" "GIF" ("NAME" "empty.gif") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.gif")) NIL NIL)("IMAGE" "JPEG" ("NAME" "empty.jpg") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.jpg")) NIL NIL) "RELATED" ("BOUNDARY" "Next_Related") NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1) "ALTERNATIVE" ("BOUNDARY" "Next_Alternative") NIL NIL NIL)',
        '(((("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1)("IMAGE" "GIF" ("NAME" "empty.gif") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.gif")) NIL NIL)("IMAGE" "JPEG" ("NAME" "empty.jpg") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.jpg")) NIL NIL) "RELATED" ("BOUNDARY" "Next_Related") NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1) "ALTERNATIVE" ("BOUNDARY" "Next_Alternative") NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII" "NAME" "document.txt") NIL NIL "7BIT" 31 1 NIL ("ATTACHMENT" ("FILENAME" "document.txt")) NIL NIL) "MIXED" ("BOUNDARY" "Next_Mixed") NIL NIL NIL)',
        '(((("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1) "ALTERNATIVE" ("BOUNDARY" "Next_Alternative") NIL NIL NIL)("IMAGE" "GIF" ("NAME" "empty.gif") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.gif")) NIL NIL)("IMAGE" "JPEG" ("NAME" "empty.jpg") NIL NIL "7BIT" 2 NIL ("INLINE" ("FILENAME" "empty.jpg")) NIL NIL) "RELATED" ("BOUNDARY" "Next_Related") NIL NIL NIL)("TEXT" "PLAIN" ("CHARSET" "US-ASCII" "NAME" "document.txt") NIL NIL "7BIT" 31 1 NIL ("ATTACHMENT" ("FILENAME" "document.txt")) NIL NIL) "MIXED" ("BOUNDARY" "Next_Mixed") NIL NIL NIL)',
        '(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 24 1)("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 65 1)("APPLICATION" "OCTET-STREAM" ("NAME" "attachment.dat") NIL NIL "7BIT" 2 NIL ("ATTACHMENT" ("FILENAME" "attachment.dat")) NIL NIL) "MIXED" ("BOUNDARY" "Next_Mixed") NIL NIL NIL)',
        '(("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 43 1)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 46 1)("APPLICATION" "OCTET-STREAM" ("NAME" "attachment.dat") NIL NIL "7BIT" 2 NIL ("ATTACHMENT" ("FILENAME" "attachment.dat")) NIL NIL) "MIXED" ("BOUNDARY" "Next_Mixed") NIL NIL NIL)',
        """(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL "Notification" "base64" 1868 24)(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "base64" 2 1)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 724 12) "DELIVERY-STATUS" ("CHARSET" "US-ASCII") NIL NIL NIL)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL "Undelivered Message" "7BIT" 17961 ("Wed, 26 Jan 2022 09:06:45 +0000" "FILE-SRV | FILE-SRV | Ellern Mede | eventlog | Security - Microsoft-Windows-Security-Auditing | Code: '4625' - Type: 'Critical, Error, Warning, , Information, Verbose' - Desc: '' {44565655}" (("Netec RMM" NIL "helpdesk" "netecgc.com")) (("Netec RMM" NIL "helpdesk" "netecgc.com")) ((NIL NIL "helpdesk" "netecgc.com")) ((NIL NIL "netec.test" "netecgc.com")) NIL NIL NIL "<44565655.JitbitHelpdesk.13439.691a0afc-a0a0-457c-8208-f20b7c4a4cb1@jitbit.com>") (("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "base64" 5452 88)("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "base64" 11254 145) "ALTERNATIVE" ("BOUNDARY" "=-JG9iIL0Fmro08hOsLcfbiQ==") NIL NIL NIL) 266) "REPORT" ("REPORT-TYPE" "delivery-status" "BOUNDARY" "630A242E63.1643188008/hmail.jitbit.com") NIL NIL NIL)""",
        """((("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1) "DELIVERY-STATUS" ("CHARSET" "US-ASCII") NIL NIL NIL)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 840 ("Mon, 29 Jul 1996 02:04:52 -0700" "unsubscribe" (("Jamie Zawinski" NIL "jwz" "netscape.com")) ((NIL NIL "jwz" "netscape.com")) (("Jamie Zawinski" NIL "jwz" "netscape.com")) ((NIL NIL "newsletter-request" "imusic.com")) NIL NIL NIL "<31FC7EB4.41C6@netscape.com>") ("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7bit" 13 1) 19) "REPORT" ("REPORT-TYPE" "delivery-status" "BOUNDARY" "A41C7.838631588=_/mm1") NIL NIL NIL)""",
        """((("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1) "DELIVERY-STATUS" ("CHARSET" "US-ASCII") NIL NIL NIL)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 840 ("Mon, 29 Jul 1996 02:04:52 -0700" "unsubscribe" (("Jamie Zawinski" NIL "jwz" "netscape.com")) ((NIL NIL "jwz" "netscape.com")) (("Jamie Zawinski" NIL "jwz" "netscape.com")) ((NIL NIL "newsletter-request" "imusic.com")) NIL NIL NIL "<31FC7EB4.41C6@netscape.com>") ("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7bit" 13 1) 19) "REPORT" ("REPORT-TYPE" "delivery-status" "BOUNDARY" "A41C7.838631588=_/mm1") NIL NIL NIL)""",
        """(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 230 4)(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2 1) "DISPOSITION-NOTIFICATION" ("CHARSET" "US-ASCII") NIL NIL NIL)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 358 ("Tue, 19 Sep 1995 13:30:00 -0000" "First draft of report" (("Jane Sender" NIL "Jane_Sender" "example.com")) (("Jane Sender" NIL "Jane_Sender" "example.com")) (("Jane Sender" NIL "Jane_Sender" "example.com")) (("Joe Recipient" NIL "Joe_Recipient" "example.com")) NIL NIL NIL "<199509192301.23456@example.org>") ("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 96 5) 13) "REPORT" ("REPORT-TYPE" "disposition-notification" "BOUNDARY" "RAA14128.773615765/example.com") NIL NIL NIL)""",
        '(("MULTIPART" "ALTERNATIVE" ("BOUNDARY" "----=_NextPart_001_0040_01CE98CE.6E826F90") NIL NIL "7BIT" 4)("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 85 1) "MIXED" ("BOUNDARY" "----=_NextPart_000_003F_01CE98CE.6E826F90") NIL NIL NIL)',
        '(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 89 3)("APPLICATION" "POSTSCRIPT" NIL NIL NIL "base64" 2) "MIXED" ("BOUNDARY" "NutNews,-a-nntpmtsonsguinrcfas,-boundary") NIL NIL NIL)',
        '(("TEXT" "PLAIN" ("CHARSET" "ISO-8859-1") NIL NIL "quoted-printable" 16 2)("TEXT" "HTML" ("CHARSET" "ISO-8859-1") NIL NIL "quoted-printable" 494 12) "ALTERNATIVE" ("BOUNDARY" "----=_NextPart_000_0031_01D36222.8A648550") NIL ("en-US" "it-IT") NIL)',
        '("TEXT" "PLAIN" ("CHARSET" "ISO-2022-JP") NIL NIL "7bit" 117 6)',
        '("TEXT" "PLAIN" ("CHARSET" "US-ASCII" "NAME" "document.xml.gz") NIL NIL "7BIT" 16 1)',
        '("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 354 ("Sun, 12 Aug 2012 12:34:56 +0300" "submsg" ((NIL NIL "sub" "domain.org")) ((NIL NIL "sub" "domain.org")) ((NIL NIL "sub" "domain.org")) NIL NIL NIL NIL NIL) (("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 46 (NIL "m1" ((NIL NIL "m1" "example.com")) ((NIL NIL "m1" "example.com")) ((NIL NIL "m1" "example.com")) NIL NIL NIL NIL NIL) ("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 9 1) 4)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 46 (NIL "m2" ((NIL NIL "m2" "example.com")) ((NIL NIL "m2" "example.com")) ((NIL NIL "m2" "example.com")) NIL NIL NIL NIL NIL) ("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 9 1) 4) "DIGEST" ("BOUNDARY" "foo") NIL NIL NIL) 27)',
        '(("IMAGE" "PNG" NIL NIL NIL "base64" 1220 NIL NIL NIL "image1")("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "quoted-printable" 930 13) "RELATED" ("TYPE" "text/html" "BOUNDARY" "----=_NextPart_115e1404-dbbc-4611-b4ce-d08a4b021c45") NIL NIL NIL)',
        '(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 34 1)("APPLICATION" "OCTET-STREAM" NIL NIL NIL "7BIT" 34)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 320 (NIL "This part specifier should be: 3" ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) NIL NIL NIL NIL) (("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 36 1)("APPLICATION" "OCTET-STREAM" NIL NIL NIL "7BIT" 36) "MIXED" ("BOUNDARY" "3.x") NIL NIL NIL) 17)(("IMAGE" "GIF" NIL NIL NIL "7BIT" 36)("MESSAGE" "RFC822" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 491 (NIL "This part specifier should be: 4.2" ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) ((NIL NIL "me" "myself.com")) NIL NIL NIL NIL) (("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 38 1)(("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 40 1)("TEXT" "RICHTEXT" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 40 1) "ALTERNATIVE" ("BOUNDARY" "4.2.2.x") NIL NIL NIL) "MIXED" ("BOUNDARY" "4.2.x") NIL NIL NIL) 27) "MIXED" ("BOUNDARY" "4.x") NIL NIL NIL) "MIXED" ("BOUNDARY" "x") NIL NIL NIL)',
        '("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 16 2)',
    ]
    # for an unpacked folder with one message in it the message key and the
    # message index are the same value.
    #
    for msg_idx, (msg_key, expected) in enumerate(zip(msg_keys, expecteds)):
        async with mbox.lock.read_lock():
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
            fetch = FetchAtt(FetchOp.BODYSTRUCTURE)
            result = await fetch.fetch(ctx)

        assert result.startswith("BODYSTRUCTURE ")
        assert result[14:] == expected


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_envelope():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_rfc822_size():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_flags():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_internaldate():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_uid():
    pass
