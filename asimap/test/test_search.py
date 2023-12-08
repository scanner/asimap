"""
Test `SearchContext` and `IMAPSearch`
"""
# System imports
#
from datetime import timezone

# 3rd party imports
#
import pytest
from dirty_equals import IsNow

# Project imports
#
from ..generator import get_msg_size
from ..search import SearchContext
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
