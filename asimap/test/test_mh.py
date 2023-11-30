"""
Tests for our subclass of `mailbox.MH` that adds some async methods
"""
# system imports
#
import random
from mailbox import MHMessage, NotEmptyError

# 3rd party imports
#
import pytest

# Project imports
#
from ..mh import MH
from .conftest import assert_email_equal


####################################################################
#
@pytest.mark.asyncio
async def test_mh_akeys(bunch_of_email_in_folder):
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    inbox_folder = mh.get_folder("inbox")
    inbox_dir = mh_dir / "inbox"
    dir_keys = sorted(
        [int(x.name) for x in inbox_dir.iterdir() if x.name.isdigit()]
    )
    folder_keys = await inbox_folder.akeys()
    assert dir_keys == folder_keys


####################################################################
#
@pytest.mark.asyncio
async def test_mh_listdirs(tmp_path, faker):
    mh_dir = tmp_path / "Mail"
    mh = MH(mh_dir)
    folders = sorted([faker.word() for _ in range(10)])
    for folder in folders:
        mh.add_folder(folder)

    found_folders = sorted(await mh.alist_folders())
    assert folders == found_folders


####################################################################
#
@pytest.mark.asyncio
async def test_mh_lock_folder(tmp_path):
    """
    XXX To do a proper test we need to fork a separate process and validate

        that it blocks while we hold this lock. Tested this by hand and it
        worked so going to leave the full test for later.

    For now we are testing that this does not outright fail.
    """
    mh_dir = tmp_path / "Mail"
    mh = MH(mh_dir)
    inbox = mh.add_folder("inbox")
    assert inbox._locked is False
    async with inbox.lock_folder():
        assert inbox._locked is True
    assert inbox._locked is False

    # Locks are not lost with an exception.
    #
    with pytest.raises(RuntimeError):
        async with inbox.lock_folder():
            assert inbox._locked is True
            raise RuntimeError("Woop")
    assert inbox._locked is False


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aget_message(bunch_of_email_in_folder):
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    inbox_folder = mh.get_folder("inbox")
    inbox_dir = mh_dir / "inbox"
    folder_keys = await inbox_folder.akeys()
    folder_msg = await inbox_folder.aget_message(folder_keys[0])
    with open(inbox_dir / "1", "rb") as f:
        dir_msg = MHMessage(f.read())
    assert_email_equal(folder_msg, dir_msg)


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aget_bytes(bunch_of_email_in_folder):
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    inbox_folder = mh.get_folder("inbox")
    inbox_dir = mh_dir / "inbox"
    folder_keys = await inbox_folder.akeys()
    folder_msg_bytes = await inbox_folder.aget_bytes(folder_keys[0])
    with open(inbox_dir / "1", "rb") as f:
        dir_msg_bytes = f.read()
    assert dir_msg_bytes == folder_msg_bytes


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aadd(tmp_path, email_factory):
    mh_dir = tmp_path / "Mail"
    mh = MH(mh_dir)
    inbox = mh.add_folder("inbox")
    msg = MHMessage(email_factory())
    msg.add_sequence("unseen")
    key = await inbox.aadd(msg)
    inbox_dir = mh_dir / "inbox"
    with open(inbox_dir / str(key), "rb") as f:
        dir_msg = MHMessage(f.read())
    assert_email_equal(msg, dir_msg)
    sequences = await inbox.aget_sequences()
    assert key in sequences["unseen"]


####################################################################
#
@pytest.mark.asyncio
async def test_mh_asetitem(bunch_of_email_in_folder, email_factory):
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    inbox = mh.get_folder("inbox")
    keys = await inbox.akeys()
    msg = MHMessage(email_factory())
    msg.add_sequence("Seen")
    msg.remove_sequence("unseen")
    key = keys[0]
    await inbox.asetitem(key, msg)

    inbox_dir = mh_dir / "inbox"
    with open(inbox_dir / str(key), "rb") as f:
        dir_msg = MHMessage(f.read())
    assert_email_equal(msg, dir_msg)
    sequences = await inbox.aget_sequences()
    assert key in sequences["Seen"]
    assert key not in sequences["unseen"]


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aclear(bunch_of_email_in_folder):
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    inbox_folder = mh.get_folder("inbox")
    inbox_dir = mh_dir / "inbox"
    dir_keys = sorted(
        [int(x.name) for x in inbox_dir.iterdir() if x.name.isdigit()]
    )
    assert dir_keys

    await inbox_folder.aclear()

    dir_keys = sorted(
        [int(x.name) for x in inbox_dir.iterdir() if x.name.isdigit()]
    )
    assert len(dir_keys) == 0


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aremove(bunch_of_email_in_folder):
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    inbox_folder = mh.get_folder("inbox")
    inbox_dir = mh_dir / "inbox"

    dir_keys = sorted(
        [int(x.name) for x in inbox_dir.iterdir() if x.name.isdigit()]
    )
    assert dir_keys

    async with inbox_folder.lock_folder():
        folder_keys = await inbox_folder.akeys()
        for key in folder_keys:
            await inbox_folder.aremove(key)

    dir_keys = sorted(
        [int(x.name) for x in inbox_dir.iterdir() if x.name.isdigit()]
    )
    assert len(dir_keys) == 0


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aget_sequences(bunch_of_email_in_folder):
    # The `bunch_of_email_in_folder` fixture puts all the messages in the
    # sequence `unsee`.
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    inbox = mh.get_folder("inbox")
    keys = await inbox.akeys()
    sequences = await inbox.aget_sequences()
    assert keys == sequences["unseen"]


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aset_sequences(bunch_of_email_in_folder):
    # The `bunch_of_email_in_folder` fixture puts all the messages in the
    # sequence `unsee`.
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    inbox = mh.get_folder("inbox")
    keys = await inbox.akeys()
    sequences = await inbox.aget_sequences()

    # Pick some random messages to move in to the 'Seen' sequence.
    #
    seen = []
    for i in range(int(len(keys) / 2)):
        key = random.choice(sequences["unseen"])
        seen.append(key)
        sequences["unseen"].remove(key)
    sequences["Seen"] = sorted(seen)
    await inbox.aset_sequences(sequences)
    new_sequences = await inbox.aget_sequences()
    assert sequences == new_sequences


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aremove_folder(bunch_of_email_in_folder):
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    _ = mh.add_folder("to_remove")
    _ = mh.get_folder("to_remove")
    to_remove_dir = mh_dir / "to_remove"
    assert to_remove_dir.exists()
    await mh.aremove_folder("to_remove")
    assert not to_remove_dir.exists()

    # can not remove a folder that does not exist.
    #
    with pytest.raises(FileNotFoundError):
        await mh.aremove_folder("to_remove")

    # Can not remove a folder with stuff in it.
    #
    with pytest.raises(NotEmptyError):
        await mh.aremove_folder("inbox")


####################################################################
#
@pytest.mark.asyncio
async def test_mh_apack(bunch_of_email_in_folder):
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    inbox = mh.get_folder("inbox")
    keys = await inbox.akeys()

    # Delete half the messages in the folder.
    #
    num_to_remove = int(len(keys) / 2)
    for i in range(num_to_remove):
        key = random.choice(keys)
        keys.remove(key)
        await inbox.aremove(key)

    # And now pack the folder.
    #
    await inbox.apack()

    keys = await inbox.akeys()
    assert keys == list(range(1, len(keys) + 1))
    sequences = await inbox.aget_sequences()
    assert sequences["unseen"] == keys
