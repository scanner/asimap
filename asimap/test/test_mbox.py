"""
Tests for the mbox module
"""
# system imports
#
import asyncio
import os
from typing import List

# 3rd party imports
#
import aiofiles
import pytest
from async_timeout import timeout
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
    mtimes = []
    for msg_key in msg_keys:
        path = os.path.join(mbox.mailbox._path, str(msg_key))
        mtimes.append(await aiofiles.os.path.getmtime(path))

    seqs = await mbox.mailbox.aget_sequences()

    # NOTE: By default `bunch_of_email_in_folder` inserts all messages it
    # creates in to the `unseen` sequence.
    #
    assert mbox.num_msgs == len(msg_keys)
    assert mbox.sequences == seqs
    assert len(mbox.sequences["unseen"]) == len(msg_keys)
    assert mbox.sequences["unseen"] == msg_keys
    assert len(mbox.sequences["Seen"]) == 0
    print(mbox.sequences)
    assert mbox.sequences["Recent"] == msg_keys
    await assert_uids_match_msgs(msg_keys, mbox)

    # The messages have been re-writen to have UID's. However the mtimes should
    # not have changed.
    #
    for msg_key, orig_mtime in zip(msg_keys, mtimes):
        path = os.path.join(mbox.mailbox._path, str(msg_key))
        mtime = await aiofiles.os.path.getmtime(path)
        assert mtime == orig_mtime


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

    async with mbox.lock.read_lock():
        await mbox.resync()
    assert r"\Marked" in mbox.attributes
    assert mbox.last_resync > last_resync
    assert mbox.num_msgs == len(msg_keys)
    assert len(mbox.sequences["unseen"]) == len(msg_keys)
    assert mbox.sequences["unseen"] == msg_keys
    assert mbox.sequences["Recent"] == msg_keys
    assert len(mbox.sequences["Seen"]) == 0
    await assert_uids_match_msgs(msg_keys, mbox)


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_sequence_change(
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

    # Remove unseen on some messages. Mark some messages replied to.
    msg_keys = await mbox.mailbox.akeys()
    seqs = await mbox.mailbox.aget_sequences()
    assert mbox.sequences["Recent"] == msg_keys

    for i in range(10):
        seqs["unseen"].remove(msg_keys[i])
    seqs["Answered"] = [msg_keys[i] for i in range(5)]
    await mbox.mailbox.aset_sequences(seqs)

    async with mbox.lock.read_lock():
        await mbox.resync()
    assert r"\Marked" in mbox.attributes
    assert mbox.last_resync > last_resync
    assert mbox.num_msgs == len(msg_keys)
    assert mbox.sequences != seqs  # Addition of `Seen` sequence
    assert len(mbox.sequences["unseen"]) == 10
    assert mbox.sequences["unseen"] == msg_keys[10:]
    assert len(mbox.sequences["Seen"]) == 10
    assert mbox.sequences["Seen"] == msg_keys[:10]

    # Messages have gained and lost flags (sequences), but no new messages have
    # appeared, thus `Recent` is the same as before (all message keys)
    #
    assert mbox.sequences["Recent"] == msg_keys
    await assert_uids_match_msgs(msg_keys, mbox)


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_resync_msg_with_wrong_uidvv(
    faker, bunch_of_email_in_folder, imap_user_server
):
    """
    Some operations copy messages to new folders, which means they have the
    wrong uidvv for the folder that they have been moved to.
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

    # and give this new message some random uid_vv/uid.
    #
    new_msg = msg_keys[-1]
    msg = await mbox.mailbox.aget_message(new_msg)
    uid_vv = faker.pyint()
    uid = faker.pyint()
    msg[UID_HDR] = f"{uid_vv:010d}.{uid:010d}"
    await mbox.mailbox.asetitem(new_msg, msg)

    async with mbox.lock.read_lock():
        await mbox.resync()

    seqs = await mbox.mailbox.aget_sequences()
    assert r"\Marked" in mbox.attributes
    assert mbox.last_resync > last_resync
    assert mbox.num_msgs == len(msg_keys)
    assert mbox.sequences == seqs
    assert len(mbox.sequences["unseen"]) == len(msg_keys)
    assert mbox.sequences["unseen"] == msg_keys
    assert mbox.sequences["Recent"] == msg_keys
    assert len(mbox.sequences["Seen"]) == 0
    await assert_uids_match_msgs(msg_keys, mbox)


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_resync_two_tasks_racing(
    bunch_of_email_in_folder, imap_user_server
):
    """
    Create a Mailbox. Create an asyncio.Event. Start two tasks that wait on the
    event, make sure several resync's complete, including new messages
    being added to the mailbox. (and there is no deadlock)
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

    # We add some new messages to the mailbox
    #
    bunch_of_email_in_folder(folder=NAME, num_emails=10)
    msg_keys = await mbox.mailbox.akeys()

    # We create an event our two tasks will wait on.
    #
    start_event = asyncio.Event()

    # Our two tasks that are going to race to see who resyncs first.
    async def resync_racer():
        async with mbox.lock.read_lock():
            await start_event.wait()
            await mbox.resync()
        assert r"\Marked" in mbox.attributes
        assert mbox.last_resync > last_resync
        assert mbox.num_msgs == len(msg_keys)
        assert len(mbox.sequences["unseen"]) == len(msg_keys)
        assert mbox.sequences["unseen"] == msg_keys
        assert mbox.sequences["Recent"] == msg_keys
        assert len(mbox.sequences["Seen"]) == 0
        await assert_uids_match_msgs(msg_keys, mbox)

    task1 = asyncio.create_task(resync_racer(), name="task1")
    task2 = asyncio.create_task(resync_racer(), name="task2")

    await asyncio.sleep(1)
    start_event.set()
    async with timeout(2):
        results = await asyncio.gather(task1, task2, return_exceptions=True)
    assert results


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_resync_mysterious_msg_deletions(
    bunch_of_email_in_folder, imap_user_server
):
    """
    The resync code handles something removing messages from a mailbox
    outside of our control.
    """
    NAME = "inbox"
    bunch_of_email_in_folder(folder=NAME)
    server = imap_user_server
    mbox = await Mailbox.new(NAME, server)
    last_resync = mbox.last_resync
    await asyncio.sleep(1)

    # Remove the 5th message.
    #
    msg_keys = await mbox.mailbox.akeys()
    await mbox.mailbox.aremove(msg_keys[5])
    msg_keys = await mbox.mailbox.akeys()
    assert len(msg_keys) == 19

    async with mbox.lock.read_lock():
        await mbox.resync()
    assert r"\Marked" in mbox.attributes
    assert mbox.last_resync > last_resync
    assert mbox.num_msgs == len(msg_keys)
    assert len(mbox.sequences["unseen"]) == len(msg_keys)
    assert mbox.sequences["unseen"] == msg_keys
    assert mbox.sequences["Recent"] == msg_keys
    assert len(mbox.sequences["Seen"]) == 0
    await assert_uids_match_msgs(msg_keys, mbox)


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_resync_mysterious_folder_pack(
    bunch_of_email_in_folder, imap_user_server
):
    """
    Mailbox handles if folder gets `packed` by some outside force
    """
    NAME = "inbox"
    bunch_of_email_in_folder()
    server = imap_user_server
    mbox = await Mailbox.new(NAME, server)
    await asyncio.sleep(1)

    msg_keys = await mbox.mailbox.akeys()
    for i in (1, 5, 10, 15, 16):
        await mbox.mailbox.aremove(msg_keys[i])
    await mbox.mailbox.apack()
    msg_keys = await mbox.mailbox.akeys()

    async with mbox.lock.read_lock():
        await mbox.resync()
    assert r"\Marked" in mbox.attributes
    assert mbox.num_msgs == len(msg_keys)
    assert len(mbox.sequences["unseen"]) == len(msg_keys)
    assert mbox.sequences["unseen"] == msg_keys
    assert mbox.sequences["Recent"] == msg_keys
    assert len(mbox.sequences["Seen"]) == 0
    await assert_uids_match_msgs(msg_keys, mbox)


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_resync_auto_pack(
    bunch_of_email_in_folder, imap_user_server
):
    """
    resync autopacks if the folder is too gappy.
    """
    NAME = "inbox"

    # Gap every other message. This is enough gaps for the auto-repack to kick
    # in.
    #
    bunch_of_email_in_folder(sequence=range(1, 41))

    server = imap_user_server
    mbox = await Mailbox.new(NAME, server)
    msg_keys = list(range(1, 21))  # After pack it should be 1..20
    assert mbox.num_msgs == len(msg_keys)
    assert len(mbox.sequences["unseen"]) == len(msg_keys)
    assert mbox.sequences["unseen"] == msg_keys
    assert mbox.sequences["Recent"] == msg_keys
    await assert_uids_match_msgs(msg_keys, mbox)


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_selected(
    mocker, bunch_of_email_in_folder, imap_user_server_and_client
):
    NAME = "inbox"
    bunch_of_email_in_folder()
    server, imap_client_proxy = imap_user_server_and_client
    mbox = await Mailbox.new(NAME, server)
    msg_keys = await mbox.mailbox.akeys()
    num_msgs = len(msg_keys)

    async with mbox.lock.read_lock():
        await mbox.selected(imap_client_proxy.cmd_processor)

    expected = [
        f"* {num_msgs} EXISTS",
        f"* {num_msgs} RECENT",
        f"* OK [UNSEEN {msg_keys[0]}]",
        f"* OK [UIDVALIDITY {mbox.uid_vv}]",
        f"* OK [UIDNEXT {mbox.next_uid}]",
        r"* FLAGS (\Answered \Deleted \Draft \Flagged \Recent \Seen unseen)",
        r"* OK [PERMANENTFLAGS (\Answered \Deleted \Draft \Flagged \Seen \*)]",
    ]

    results = [x.strip() for x in imap_client_proxy.push.call_args.args]
    assert expected == results
