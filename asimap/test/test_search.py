"""
Test `SearchContext` and `IMAPSearch`
"""
# System imports
#
import os
import random
from collections import Counter, defaultdict
from datetime import date, timezone
from email.message import EmailMessage
from typing import Dict, List, Tuple

# 3rd party imports
#
import pytest
from dirty_equals import IsNow

# Project imports
#
from ..constants import REVERSE_SYSTEM_FLAG_MAP, SYSTEM_FLAGS
from ..generator import get_msg_size
from ..search import IMAPSearch, SearchContext
from ..utils import UID_HDR, get_uidvv_uid, parsedate, utime
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


####################################################################
#
@pytest.mark.asyncio
async def test_search_all(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = await mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max
    matched: List[int] = []
    search_op = IMAPSearch("all")
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        async with mbox.lock.read_lock():
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
            if await search_op.match(ctx):
                matched.append(msg_key)

    assert msg_keys == matched


####################################################################
#
@pytest.mark.asyncio
async def test_search_headers(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = await mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # First, searching on an empty string matches messages that have the header.
    #
    matched: List[int] = []
    search_op = IMAPSearch("header", header="subject", string="")
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        async with mbox.lock.read_lock():
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
            if await search_op.match(ctx):
                matched.append(msg_key)

    assert msg_keys == matched

    # Go through the messages and find the most common words in the subject.
    # Those will be what we test header search on.
    #
    words: Counter[str] = Counter()
    for msg_key in msg_keys:
        msg = await mbox.mailbox.aget_message(msg_key)
        for word in msg["Subject"].split():
            words[word.lower()] += 1

    msg_keys_by_word: Dict[str, List[int]] = defaultdict(list)
    for word, count in words.most_common(4):
        search_op = IMAPSearch("header", header="subject", string=word)
        for msg_idx, msg_key in enumerate(msg_keys):
            msg_idx += 1
            async with mbox.lock.read_lock():
                ctx = SearchContext(
                    mbox, msg_key, msg_idx, seq_max, uid_max, seqs
                )
                if await search_op.match(ctx):
                    msg_keys_by_word[word].append(msg_key)

    # Go through all the messages by hand and make sure our searches turned up
    # the right results.
    #
    for word, matched_keys in msg_keys_by_word.items():
        for msg_key in msg_keys:
            msg = await mbox.mailbox.aget_message(msg_key)
            if msg_key in matched_keys:
                assert word in msg["Subject"].lower()
            else:
                assert word not in msg["Subject"].lower()


####################################################################
#
@pytest.mark.asyncio
async def test_search_sent_before_since_on(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = await mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # Go through and find the middle most date.
    #
    dates: List[Tuple[date, int]] = []
    for msg_key in msg_keys:
        msg = await mbox.mailbox.aget_message(msg_key)
        dt = parsedate(msg["Date"]).date()
        dates.append((dt, msg_key))

    dates = sorted(dates, key=lambda x: x[0])
    mp = int(len(dates) / 2)
    check_date = dates[mp][0]
    before_date = sorted([x[1] for x in dates[:mp]])
    on_date = dates[mp][1]
    after_date = sorted([x[1] for x in dates[mp:]])

    search_op = IMAPSearch("sentbefore", date=check_date)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        async with mbox.lock.read_lock():
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
            if await search_op.match(ctx):
                assert msg_key in before_date
            else:
                assert msg_key in after_date

    search_op = IMAPSearch("sentsince", date=check_date)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        async with mbox.lock.read_lock():
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
            if await search_op.match(ctx):
                assert msg_key in after_date
            else:
                assert msg_key in before_date

    search_op = IMAPSearch("senton", date=check_date)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        async with mbox.lock.read_lock():
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
            if await search_op.match(ctx):
                assert msg_key == on_date
            else:
                assert msg_key != on_date


####################################################################
#
@pytest.mark.asyncio
async def test_search_before_since_on(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = await mbox.mailbox.akeys()
    seq_max = len(msg_keys)
    seqs = await mbox.mailbox.aget_sequences()
    uid_vv, uid_max = await mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # Go through the messages and set the mtime on each message to be the
    # parsed value of the `Date` header. We do not cache the mtime outside of
    # the search context so doing this post mbox.resync() is okay in terms of
    # mbox state.
    #
    dates: List[Tuple[date, int]] = []
    for msg_key in msg_keys:
        msg = await mbox.mailbox.aget_message(msg_key)
        dt = parsedate(msg["Date"])
        dt_ts = dt.timestamp()
        msg_path = os.path.join(mbox.mailbox._path, str(msg_key))
        await utime(msg_path, (dt_ts, dt_ts))
        dates.append((dt.date(), msg_key))

    # Find our mid-point date for the three different searches
    #
    dates = sorted(dates, key=lambda x: x[0])
    mp = int(len(dates) / 2)
    check_date = dates[mp][0]
    before_date = sorted([x[1] for x in dates[:mp]])
    on_date = dates[mp][1]
    after_date = sorted([x[1] for x in dates[mp:]])

    search_op = IMAPSearch("before", date=check_date)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        async with mbox.lock.read_lock():
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
            if await search_op.match(ctx):
                assert msg_key in before_date
            else:
                assert msg_key in after_date

    search_op = IMAPSearch("since", date=check_date)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        async with mbox.lock.read_lock():
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
            if await search_op.match(ctx):
                assert msg_key in after_date
            else:
                assert msg_key in before_date

    search_op = IMAPSearch("on", date=check_date)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        async with mbox.lock.read_lock():
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max, seqs)
            if await search_op.match(ctx):
                assert msg_key == on_date
            else:
                assert msg_key != on_date
