"""
Tests for the mbox module
"""
# system imports
#

# 3rd party imports
#
import pytest

from .mbox import Mailbox

# Project imports
#
from .user_server import IMAPUserServer


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_init(mh_folder):
    """
    We can create a Mailbox object instance.
    """
    (mh_dir, _, _) = mh_folder()
    server = await IMAPUserServer.new(mh_dir)
    mbox = await Mailbox.new("inbox", server)
    assert mbox


####################################################################
#
@pytest.mark.asyncio
def test_resync_new_folder():
    """
    Test resync on a folder that is new to the system (ie: none of the
    messages have been properly tagged with the x-asimapd-uuid header)
    """
    pass
