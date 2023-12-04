"""
Tests for the mbox module
"""
# system imports
#

# 3rd party imports
#
import pytest
from dirty_equals import IsNow

# Project imports
#
from ..mbox import Mailbox


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
async def test_get_sequences_update_seen(
    bunch_of_email_in_folder, imap_user_server
):
    server = imap_user_server
    mbox = await Mailbox.new("inbox", server)
    msg_keys = await mbox.mailbox.akeys()
    seqs = await mbox._get_sequences_update_seen(msg_keys)
    assert seqs


####################################################################
#
@pytest.mark.asyncio
async def test_resync_new_folder():
    """
    Test resync on a folder that is new to the system (ie: none of the
    messages have been properly tagged with the x-asimapd-uuid header)
    """
    pass


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
