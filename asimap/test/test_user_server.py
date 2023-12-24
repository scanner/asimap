"""
Test the user server.
"""
# system imports
#

# 3rd party imports
#
import pytest

# Project imports
#
from ..client import Authenticated
from ..parse import IMAPClientCommand
from ..user_server import IMAPUserServer
from .conftest import client_push_responses


####################################################################
#
@pytest.mark.asyncio
async def test_user_server_instantiate(mh_folder):
    (mh_dir, _, _) = mh_folder()
    try:
        user_server = await IMAPUserServer.new(mh_dir)
        assert user_server
    finally:
        await user_server.shutdown()


####################################################################
#
@pytest.mark.asyncio
async def test_check_all_active_folders(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    """
    Search is tested mostly `test_search`.. so we only need a very simple
    search.
    """
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    # Select the inbox.
    #
    cmd = IMAPClientCommand("A001 SELECT inbox")
    cmd.parse()
    await client_handler.command(cmd)
    _ = client_push_responses(imap_client)
    cmd = IMAPClientCommand("A001 IDLE\r\n")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["+ idling"]
    assert client_handler.idling is True

    # Check all active folders will now scan the inbox.
    #
    await server.check_all_active_folders()
