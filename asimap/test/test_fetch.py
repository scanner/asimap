"""
Fetch.. the part that gets various bits and pieces of messages.
"""
# System imports
#
from mailbox import MHMessage
from typing import Any, Dict, List, Tuple

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

    async with mbox.lock.read_lock():
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, sequences)
        msg = await ctx.msg()
        msg_size = await ctx.msg_size()
        fetch = FetchAtt(FetchOp.BODY)
        result = await fetch.fetch(ctx)

    assert result.startswith("BODY {")
    m = _lit_ref_re.search(result)
    assert m
    result_msg_size = int(m.group(1))
    assert result_msg_size == msg_size

    msg_start_idx = result.find("}\r\n") + 3
    msg_data = result[msg_start_idx : msg_start_idx + result_msg_size]
    result_msg = MHMessage(msg_data)
    assert_email_equal(msg, result_msg)


####################################################################
#
@pytest.mark.asyncio
async def test_fetch_bodystructure():
    pass


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
