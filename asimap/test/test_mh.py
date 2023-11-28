"""
Tests for our subclass of `mailbox.MH` that adds some async methods
"""
# system imports
#

# 3rd party imports
#
import pytest

# Project imports
#
from ..mh import MH


####################################################################
#
@pytest.mark.asyncio
async def test_mh_akeys(bunch_of_email_in_folder):
    mh_dir = bunch_of_email_in_folder()
    mh = MH(str(mh_dir))
    inbox_folder = mh.get_folder("inbox")
    inbox_dir = mh_dir / "inbox"
    dir_keys = sorted([int(x.name) for x in inbox_dir.iterdir()])
    folder_keys = await inbox_folder.akeys()
    assert dir_keys == folder_keys


####################################################################
#
@pytest.mark.asyncio
async def test_mh_lock_folder():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aget_message():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aget_bytes():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aadd():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aremove():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aget_sequences():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aset_sequences():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aremove_folder():
    pass


####################################################################
#
@pytest.mark.asyncio
async def test_mh_apack():
    pass
