"""
Tests for our subclass of `mailbox.MH` that adds some async methods
"""

# 3rd party imports
#
import pytest

# Project imports
#
from ..mh import MH


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
