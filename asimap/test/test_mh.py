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
    inbox = mh_dir / "inbox"
    for msg in inbox.iterdir():
        print(f"Message: {msg}, exists: {msg.exists()}")
    keys = await mh.akeys()
    print(f"Keys: {keys}")
    assert False


####################################################################
#
async def test_mh_lock_folder():
    pass


####################################################################
#
async def test_mh_aget_message():
    pass


####################################################################
#
async def test_mh_aget_bytes():
    pass


####################################################################
#
async def test_mh_aadd():
    pass


####################################################################
#
async def test_mh_aremove():
    pass


####################################################################
#
async def test_mh_aget_sequences():
    pass


####################################################################
#
async def test_mh_aset_sequences():
    pass


####################################################################
#
async def test_mh_aremove_folder():
    pass


####################################################################
#
async def test_mh_apack():
    pass
