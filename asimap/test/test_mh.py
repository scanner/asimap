"""
Tests for our subclass of `mailbox.MH` that adds some async methods
"""

# system imports
#
import shutil
from mailbox import NoSuchMailboxError

# 3rd party imports
#
import pytest

# Project imports
#
from .. import mh as mh_module
from ..mh import MH


####################################################################
#
@pytest.fixture
def enable_file_locking():
    """Enable MH file locking for the duration of the test."""
    mh_module.set_file_locking(True)
    yield
    mh_module.set_file_locking(False)


####################################################################
#
@pytest.mark.asyncio
async def test_mh_lock_folder(tmp_path, enable_file_locking):
    """
    GIVEN: FILE_LOCKING_ENABLED is True
    WHEN:  lock_folder() is used as a context manager
    THEN:  it acquires and releases the file lock normally

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
async def test_mh_lock_folder_noop(tmp_path):
    """
    GIVEN: FILE_LOCKING_ENABLED is False (default)
    WHEN:  lock_folder() is used as a context manager
    THEN:  it yields without opening any files or setting _locked
    """
    assert mh_module.FILE_LOCKING_ENABLED is False

    mh_dir = tmp_path / "Mail"
    mh = MH(mh_dir)
    inbox = mh.add_folder("inbox")

    assert inbox._locked is False
    async with inbox.lock_folder():
        # When file locking is disabled, _locked remains False
        assert inbox._locked is False
    assert inbox._locked is False


####################################################################
#
@pytest.mark.asyncio
async def test_mh_lock_folder_noop_deleted_folder(tmp_path):
    """
    GIVEN: FILE_LOCKING_ENABLED is False (default)
    WHEN:  lock_folder() is called on a folder that no longer exists
    THEN:  NoSuchMailboxError is still raised
    """
    assert mh_module.FILE_LOCKING_ENABLED is False

    mh_dir = tmp_path / "Mail"
    mh = MH(mh_dir)
    inbox = mh.add_folder("inbox")

    # Remove the folder from disk
    shutil.rmtree(mh_dir / "inbox")

    with pytest.raises(NoSuchMailboxError):
        async with inbox.lock_folder():
            pass


####################################################################
#
def test_set_file_locking():
    """
    GIVEN: default state (FILE_LOCKING_ENABLED is False)
    WHEN:  set_file_locking() is called
    THEN:  FILE_LOCKING_ENABLED is updated accordingly
    """
    assert mh_module.FILE_LOCKING_ENABLED is False
    mh_module.set_file_locking(True)
    assert mh_module.FILE_LOCKING_ENABLED is True
    mh_module.set_file_locking(False)
    assert mh_module.FILE_LOCKING_ENABLED is False


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
        folder_keys = inbox_folder.keys()
        for key in folder_keys:
            await inbox_folder.aremove(key)

    dir_keys = sorted(
        [int(x.name) for x in inbox_dir.iterdir() if x.name.isdigit()]
    )
    assert len(dir_keys) == 0
