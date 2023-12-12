"""
Test `SearchContext` and `IMAPSearch`
"""
# System imports
#
import random
from collections import defaultdict
from datetime import timezone
from email.message import EmailMessage
from typing import Dict, List

# 3rd party imports
#
import pytest
from dirty_equals import IsNow

# Project imports
#
from ..constants import REVERSE_SYSTEM_FLAG_MAP, SYSTEM_FLAGS
from ..generator import get_msg_size
from ..search import IMAPSearch, SearchContext
from ..utils import UID_HDR, get_uidvv_uid
from .conftest import assert_email_equal


####################################################################
#
@pytest.mark.asyncio
async def test_search_context(mailbox_instance):
    """
    A fairly boring test.. just making sure the SearchContext works as
    expected without any failures.
    """
    mbox = await mailbox_instance()
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    sequences = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = await mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    async with mbox.lock.read_lock():
        for idx, msg_key in enumerate(msg_keys):
            ctx = SearchContext(
                mbox, msg_key, idx + 1, seq_max, uid_max, sequences
            )
            mhmsg = await mbox.mailbox.aget_message(msg_key)
            uid_vv, uid = get_uidvv_uid(mhmsg[UID_HDR])
            assert uid == await ctx.uid()
            ctx._uid = None
            assert await ctx.internal_date() == IsNow(tz=timezone.utc)
            assert ctx.msg_key == msg_key
            assert ctx.seq_max == seq_max
            assert ctx.uid_max == uid_max
            assert ctx.msg_number == idx + 1
            assert ctx.sequences == mhmsg.get_sequences()
            assert_email_equal(mhmsg, await ctx.msg())
            assert uid == await ctx.uid()
            assert uid_vv == await ctx.uid_vv()
            assert get_msg_size(mhmsg) == await ctx.msg_size()
            email_msg = await ctx.email_message()
            assert isinstance(email_msg, EmailMessage)
            assert_email_equal(mhmsg, email_msg)


####################################################################
#
@pytest.mark.asyncio
async def test_search_keywords(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = await mbox.get_uid_from_msg(msg_keys[-1])
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

        seqs[REVERSE_SYSTEM_FLAG_MAP[flag]] = sorted(msgs_by_flag[flag])

    await mbox.mailbox.aset_sequences(seqs)

    matches_by_flag: Dict[str, List[int]] = defaultdict(list)
    for keyword in SYSTEM_FLAGS:
        search_op = IMAPSearch("keyword", keyword=keyword)
        for msg_idx, msg_key in enumerate(msg_keys):
            msg_idx += 1
            async with mbox.lock.read_lock():
                ctx = SearchContext(
                    mbox, msg_key, msg_idx, seq_max, uid_max, seqs
                )
                if await search_op.match(ctx):
                    matches_by_flag[keyword].append(msg_key)

    for flag, msg_keys in matches_by_flag.items():
        assert seqs[REVERSE_SYSTEM_FLAG_MAP[flag]] == sorted(msg_keys)
