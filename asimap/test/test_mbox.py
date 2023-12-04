"""
Tests for the mbox module
"""
# system imports
#
import asyncio
from typing import List

# 3rd party imports
#
import pytest
from dirty_equals import IsNow

# Project imports
#
from ..mbox import Mailbox
from ..utils import UID_HDR


####################################################################
#
async def assert_uids_match_msgs(msg_keys: List[int], mbox: Mailbox):
    """
    A helper function to validate that the messages in the mailbox all have
    the UID_HDR, and that all the uid's set in the messages match the ones in
    `mbox.uids` (and that the order is the same.)

    This assures that one of the most basic functions of `mbox.resync()` works
    properly.
    """
    assert len(msg_keys) == len(mbox.uids)
    for msg_key, uid in zip(msg_keys, mbox.uids):
        msg = await mbox.mailbox.aget_message(msg_key)
        uid_vv, msg_uid = [int(x) for x in msg[UID_HDR].strip().split(".")]
        assert uid_vv == mbox.uid_vv
        assert uid == msg_uid

        cached_uid_vv, cached_uid = await mbox.get_uid_from_msg(msg_key)
        assert cached_uid_vv == mbox.uid_vv
        assert uid == cached_uid


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_init(imap_user_server):
    """
    We can create a Mailbox object instance.
    """
    server = imap_user_server
    NAME = "inbox"
    mbox = await Mailbox.new(NAME, server)
    assert mbox
    assert mbox.id
    assert mbox.last_resync == IsNow(unix_number=True)

    results = await server.db.fetchone(
        "select id, uid_vv,attributes,mtime,next_uid,num_msgs,"
        "num_recent,uids,last_resync,subscribed from mailboxes "
        "where name=?",
        (NAME,),
    )
    (
        id,
        uid_vv,
        attributes,
        mtime,
        next_uid,
        num_msgs,
        num_recent,
        uids,
        last_resync,
        subscribed,
    ) = results
    assert id == mbox.id
    assert uid_vv == 1  # 1 because first mailbox in server
    assert mbox.uid_vv == uid_vv
    assert sorted(attributes.split(",")) == [r"\HasNoChildren", r"\Marked"]
    assert mbox.mtime == mtime
    assert mtime == IsNow(unix_number=True)
    assert next_uid == 1
    assert mbox.next_uid == next_uid
    assert num_msgs == 0
    assert mbox.num_msgs == num_msgs
    assert num_recent == 0
    assert mbox.num_recent == num_recent
    assert uids == ""
    assert len(mbox.uids) == 0
    assert mbox.last_resync == last_resync
    assert bool(subscribed) is False
    assert mbox.subscribed == bool(subscribed)


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_init_with_messages(
    bunch_of_email_in_folder, imap_user_server
):
    NAME = "inbox"
    bunch_of_email_in_folder(folder=NAME)
    server = imap_user_server
    mbox = await Mailbox.new(NAME, server)
    assert mbox.uid_vv == 1
    assert r"\Marked" in mbox.attributes
    assert r"\HasNoChildren" in mbox.attributes

    msg_keys = await mbox.mailbox.akeys()
    assert len(msg_keys) > 0

    seqs = await mbox.mailbox.aget_sequences()

    # NOTE: By default `bunch_of_email_in_folder` inserts all messages it
    # creates in to the `unseen` sequence.
    #
    assert mbox.num_msgs == len(msg_keys)
    assert mbox.sequences == seqs
    assert len(mbox.sequences["unseen"]) == len(msg_keys)
    assert mbox.sequences["unseen"] == msg_keys
    assert len(mbox.sequences["Seen"]) == 0
    await assert_uids_match_msgs(msg_keys, mbox)


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_gets_new_message(
    bunch_of_email_in_folder, imap_user_server
):
    """
    After initial init, add message to folder. Do resync.
    """
    NAME = "inbox"
    bunch_of_email_in_folder(folder=NAME)
    server = imap_user_server
    mbox = await Mailbox.new(NAME, server)
    last_resync = mbox.last_resync

    # We need to sleep at least one second for mbox.last_resync to change (we
    # only consider seconds)
    #
    await asyncio.sleep(1)

    # Now add one message to the folder.
    #
    bunch_of_email_in_folder(folder=NAME, num_emails=1)
    msg_keys = await mbox.mailbox.akeys()
    seqs = await mbox.mailbox.aget_sequences()

    async with mbox.lock.read_lock():
        await mbox.resync()
    assert r"\Marked" in mbox.attributes
    assert mbox.last_resync > last_resync
    assert mbox.num_msgs == len(msg_keys)
    assert mbox.sequences == seqs
    assert len(mbox.sequences["unseen"]) == len(msg_keys)
    assert mbox.sequences["unseen"] == msg_keys
    assert len(mbox.sequences["Seen"]) == 0
    await assert_uids_match_msgs(msg_keys, mbox)


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_resync_two_tasks_fighting():
    """
    Create a Mailbox. Create a condition. Start two tasks that wait on the
    condition, make sure several resync's complete, including new messages
    being added to the mailbox.
    """
    pass
