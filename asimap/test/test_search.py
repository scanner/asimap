"""
Test `SearchContext` and `IMAPSearch`
"""

# System imports
#
import os
import random
from collections import Counter, defaultdict
from datetime import date, timezone
from typing import Dict, List, Tuple

# 3rd party imports
#
import pytest
from dirty_equals import IsNow

# Project imports
#
from ..constants import REV_SYSTEM_FLAG_MAP, SYSTEM_FLAGS
from ..generator import get_msg_size, msg_as_string
from ..search import IMAPSearch, SearchContext
from ..utils import parsedate, utime
from .conftest import assert_email_equal


####################################################################
#
@pytest.mark.asyncio
async def test_search_context(mailbox_instance):
    """
    A fairly boring test.. just making sure the SearchContext works as
    expected without any failures.
    """
    async with mailbox_instance() as mbox:
        msg_keys = mbox.mailbox.keys()
        seq_max = len(msg_keys)
        uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
        assert uid_max

        for idx, msg_key in enumerate(msg_keys):
            ctx = SearchContext(mbox, msg_key, idx + 1, seq_max, uid_max)
            msg = mbox.get_msg(msg_key)
            uid_vv, uid = mbox.get_uid_from_msg(msg_key)
            assert uid == ctx.uid()
            ctx._uid = None
            assert await ctx.internal_date() == IsNow(tz=timezone.utc)
            assert ctx.msg_key == msg_key
            assert ctx.seq_max == seq_max
            assert ctx.uid_max == uid_max
            assert ctx.msg_number == idx + 1
            assert ctx.sequences == mbox._msg_sequences(msg_key)
            assert_email_equal(msg, ctx.msg())
            assert uid == ctx.uid()
            assert uid_vv == ctx.uid_vv()
            assert get_msg_size(msg) == ctx.msg_size()


####################################################################
#
@pytest.mark.asyncio
async def test_search_keywords(mailbox_with_bunch_of_email):
    """
    Test search on keywords.
    We create a mailbox with a bunch of email in it.
    We set a some flags on some of these messages.
    We do an IMAP Search for these flags
    Validate that the search results match the messages we set those flags on.
    """
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.mailbox.keys()
    seq_max = len(msg_keys)
    seqs = mbox.sequences
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # Set some flags on the messages
    #
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

        seqs[REV_SYSTEM_FLAG_MAP[flag]] = sorted(msgs_by_flag[flag])

    async with mbox.mh_sequences_lock:
        mbox.set_sequences_in_folder(seqs)

    from pprint import pprint

    print("Sequences set in folder:")
    pprint(seqs)

    matches_by_flag: Dict[str, List[int]] = defaultdict(list)
    for keyword in SYSTEM_FLAGS:
        search_op = IMAPSearch("keyword", keyword=keyword)
        for msg_idx, msg_key in enumerate(msg_keys):
            msg_idx += 1
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
            print(f"Search context: {ctx}, sequences:")
            pprint(ctx.sequences)
            if await search_op.match(ctx):
                matches_by_flag[keyword].append(msg_key)

    for flag, msg_keys in matches_by_flag.items():
        assert seqs[REV_SYSTEM_FLAG_MAP[flag]] == sorted(msg_keys)


####################################################################
#
@pytest.mark.asyncio
async def test_search_all(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.mailbox.keys()
    seq_max = len(msg_keys)
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max
    matched: List[int] = []
    search_op = IMAPSearch("all")
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            matched.append(msg_key)

    assert msg_keys == matched


####################################################################
#
@pytest.mark.asyncio
async def test_search_headers(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.mailbox.keys()
    seq_max = len(msg_keys)
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # First, searching on an empty string matches messages that have the header.
    #
    search_op = IMAPSearch("header", header="subject", string="")
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if not await search_op.match(ctx):
            assert False

    # Go through the messages and find the most common words in the subject.
    # Those will be what we test header search on.
    #
    words: Counter[str] = Counter()
    for msg_key in msg_keys:
        msg = mbox.get_msg(msg_key)
        for word in msg["Subject"].split():
            words[word.lower()] += 1

    msg_keys_by_word: Dict[str, List[int]] = defaultdict(list)
    for word, count in words.most_common(4):
        search_op = IMAPSearch("header", header="subject", string=word)
        for msg_idx, msg_key in enumerate(msg_keys):
            msg_idx += 1
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
            if await search_op.match(ctx):
                msg_keys_by_word[word].append(msg_key)

    # Go through all the messages by hand and make sure our searches turned up
    # the right results.
    #
    for word, matched_keys in msg_keys_by_word.items():
        for msg_key in msg_keys:
            msg = mbox.get_msg(msg_key)
            if msg_key in matched_keys:
                assert word in msg["Subject"].lower()
            else:
                assert word not in msg["Subject"].lower()


####################################################################
#
@pytest.mark.asyncio
async def test_search_sent_before_since_on(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.mailbox.keys()
    seq_max = len(msg_keys)
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # Go through and find the middle most date.
    #
    dates: List[Tuple[date, int]] = []
    for msg_key in msg_keys:
        msg = mbox.get_msg(msg_key)
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
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key in before_date
        else:
            assert msg_key in after_date

    search_op = IMAPSearch("sentsince", date=check_date)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key in after_date
        else:
            assert msg_key in before_date

    search_op = IMAPSearch("senton", date=check_date)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key == on_date
        else:
            assert msg_key != on_date


####################################################################
#
@pytest.mark.asyncio
async def test_search_before_since_on(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.mailbox.keys()
    seq_max = len(msg_keys)
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # Go through the messages and set the mtime on each message to be the
    # parsed value of the `Date` header. We do not cache the mtime outside of
    # the search context so doing this post mbox.resync() is okay in terms of
    # mbox state.
    #
    dates: List[Tuple[date, int]] = []
    for msg_key in msg_keys:
        msg = mbox.get_msg(msg_key)
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
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key in before_date
        else:
            assert msg_key in after_date

    search_op = IMAPSearch("since", date=check_date)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key in after_date
        else:
            assert msg_key in before_date

    search_op = IMAPSearch("on", date=check_date)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key == on_date
        else:
            assert msg_key != on_date


####################################################################
#
@pytest.mark.asyncio
async def test_search_body(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.mailbox.keys()
    seq_max = len(msg_keys)
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # First, searching on an empty string matches all messages with a body.
    #
    search_op = IMAPSearch("body", string="")
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if not await search_op.match(ctx):
            assert False

    # Go through the messages and find the most common words in the text/plain
    # part.
    #
    words: Counter[str] = Counter()
    for msg_key in msg_keys:
        msg = mbox.get_msg(msg_key)
        parts = msg.get_payload()
        body = msg_as_string(parts[0], headers=False).lower()
        for line in body.splitlines():
            for word in line.split():
                if word.isalpha():
                    words[word] += 1

    # Match the top couple of words.
    #
    msg_keys_by_word: Dict[str, List[int]] = defaultdict(list)
    for word, count in words.most_common(5):
        search_op = IMAPSearch("body", string=word.lower())
        for msg_idx, msg_key in enumerate(msg_keys):
            msg_idx += 1
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
            if await search_op.match(ctx):
                msg_keys_by_word[word].append(msg_key)

    # Go through all the messages by hand and make sure our searches turned up
    # the right results.
    #
    for word, matched_keys in msg_keys_by_word.items():
        for msg_key in msg_keys:
            msg = mbox.get_msg(msg_key)
            parts = msg.get_payload()
            body = msg_as_string(parts[0], headers=False).lower()
            if msg_key in matched_keys:
                assert word in body.lower()
            else:
                assert word not in body.lower()


####################################################################
#
@pytest.mark.asyncio
async def test_search_text(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.mailbox.keys()
    seq_max = len(msg_keys)
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # First, searching on an empty string matches all messages with a body.
    #
    search_op = IMAPSearch("text", string="")
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if not await search_op.match(ctx):
            assert False

    # Go through the messages and find the most common words in the text/plain
    # part.
    #
    words: Counter[str] = Counter()
    for msg_key in msg_keys:
        msg = mbox.get_msg(msg_key)
        body = msg_as_string(msg, headers=True).lower()
        for line in body.splitlines():
            for word in line.split():
                if word.isalpha():
                    words[word] += 1

    # Match the top couple of words.
    #
    msg_keys_by_word: Dict[str, List[int]] = defaultdict(list)
    for word, count in words.most_common(5):
        search_op = IMAPSearch("text", string=word)
        for msg_idx, msg_key in enumerate(msg_keys):
            msg_idx += 1
            ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
            if await search_op.match(ctx):
                msg_keys_by_word[word].append(msg_key)

    # Go through all the messages by hand and make sure our searches turned up
    # the right results.
    #
    for word, matched_keys in msg_keys_by_word.items():
        for msg_key in msg_keys:
            msg = mbox.get_msg(msg_key)
            body = msg_as_string(msg, headers=True).lower()
            if msg_key in matched_keys:
                assert word in body.lower()
            else:
                assert word not in body.lower()


####################################################################
#
@pytest.mark.asyncio
async def test_search_larger_smaller(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.mailbox.keys()
    seq_max = len(msg_keys)
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # Go through and find the various sizes and determine a mid-point
    #
    sizes: List[Tuple[int, int]] = []
    for msg_key in msg_keys:
        msg = mbox.get_msg(msg_key)
        msg_size = get_msg_size(msg)
        sizes.append((msg_size, msg_key))

    sizes = sorted(sizes, key=lambda x: x[0])
    mp = int(len(sizes) / 2)
    mid_size = sizes[mp][0] - 1  # One octet smaller.
    smaller = sorted([x[1] for x in sizes[:mp]])
    larger = sorted([x[1] for x in sizes[mp:]])

    search_op = IMAPSearch("smaller", n=mid_size)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key in smaller
        else:
            assert msg_key in larger

    search_op = IMAPSearch("larger", n=mid_size)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key in larger
        else:
            assert msg_key in smaller


####################################################################
#
@pytest.mark.asyncio
async def test_search_message_set_and_not(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.mailbox.keys()
    seq_max = len(msg_keys)
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # We have 20 messages.. so construct a message set that tests all the
    # features.
    #
    msg_set = (1, 2, (7, 10), (19, "*"), "*")
    expected = [1, 2, 7, 8, 9, 10] + list(range(19, seq_max + 1))
    search_op = IMAPSearch("message_set", msg_set=msg_set)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key in expected
        else:
            assert msg_key not in expected

    # Let us try this with the `not` operator conveniently also testing it.
    #
    search_op = IMAPSearch("not", search_key=search_op)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key not in expected
        else:
            assert msg_key in expected


####################################################################
#
@pytest.mark.asyncio
async def test_search_uid(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.mailbox.keys()
    seq_max = len(msg_keys)
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # We have 20 messages.. so construct a message set that tests all the
    # features.. These are the first 20 messages ever so the UID's actually
    # happen to match the message sequence numbers and message keys.
    #
    msg_set = (1, 2, (7, 10), (19, "*"), "*")
    expected = [1, 2, 7, 8, 9, 10] + list(range(19, seq_max + 1))
    search_op = IMAPSearch("uid", msg_set=msg_set)
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key in expected
        else:
            assert msg_key not in expected


####################################################################
#
@pytest.mark.asyncio
async def test_search_and_or(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.mailbox.keys()
    seq_max = len(msg_keys)
    uid_vv, uid_max = mbox.get_uid_from_msg(msg_keys[-1])
    assert uid_max

    # Use message sets to test and & or
    #
    # We have 20 messages.. so construct a message set that tests all the
    # features.
    #
    msg_set_1 = [(1, 10)]
    msg_set_2 = [(8, 15)]
    expected_1 = [8, 9, 10]  # (msg_set_1 and msg_set_2)
    expected_2 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]  # or

    search_op1 = IMAPSearch("message_set", msg_set=msg_set_1)
    search_op2 = IMAPSearch("message_set", msg_set=msg_set_2)
    search_op = IMAPSearch("and", search_key=[search_op1, search_op2])
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key in expected_1
        else:
            assert msg_key not in expected_1

    search_op = IMAPSearch("or", search_key=[search_op1, search_op2])
    for msg_idx, msg_key in enumerate(msg_keys):
        msg_idx += 1
        ctx = SearchContext(mbox, msg_key, msg_idx, seq_max, uid_max)
        if await search_op.match(ctx):
            assert msg_key in expected_2
        else:
            assert msg_key not in expected_2
