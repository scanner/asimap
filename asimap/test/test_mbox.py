"""
Tests for the mbox module
"""
# system imports
#

# 3rd party imports
#
import pytest

# Project imports
#
from .mbox import Mailbox


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_init(imap_user_server):
    """
    We can create a Mailbox object instance.
    """
    server = imap_user_server
    mbox = await Mailbox.new("inbox", server)
    assert mbox


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
def test_resync_new_folder():
    """
    Test resync on a folder that is new to the system (ie: none of the
    messages have been properly tagged with the x-asimapd-uuid header)
    """
    pass
