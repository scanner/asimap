"""
Higher up the stack.. testing the `client` module. This is the wrapper over
the Mailbox, basically.
"""
# system imports
#

# 3rd party imports
#
import pytest

# Project imports
#
from ..client import CAPABILITIES, BaseClientHandler
from ..parse import IMAPClientCommand
from .conftest import client_push_responses


####################################################################
#
@pytest.mark.asyncio
async def test_client_handler_idle_done(imap_client_proxy):
    """
    `DONE` is not handled via the IMAPClientCommand. There is code in the
    user_server stream reader loop to handle it.. so we just test it manually
    here.
    """
    imap_client = await imap_client_proxy()
    client_handler = BaseClientHandler(imap_client)

    cmd = IMAPClientCommand("A001 IDLE\r\n")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["+ idling"]
    assert client_handler.idling is True
    await client_handler.do_done(None)
    results = client_push_responses(imap_client)
    assert results == ["A001 OK IDLE terminated"]
    assert client_handler.idling is False


####################################################################
#
@pytest.mark.asyncio
async def test_client_handler_command(imap_client_proxy):
    """
    Using a BaseClientHandler test the `command()` method. Using a
    BaseClientHandler lets us test things that are valid IMAPCommands, but not
    supported by the BaseClientHandler so we get to test various failures as
    well.
    """
    imap_client = await imap_client_proxy()
    client_handler = BaseClientHandler(imap_client)

    # We test various IMAPCommand's against the client handler.
    #
    commands = [
        r"A001 CAPABILITY",
    ]
    expecteds = [
        [
            f"* CAPABILITY {' '.join(CAPABILITIES)}",
            "A001 OK CAPABILITY completed",
        ],
    ]

    for command, expected in zip(commands, expecteds):
        cmd = IMAPClientCommand(command + "\r\n")
        cmd.parse()
        print(f"cmd: '{command}', IMAP Command: {cmd}")
        await client_handler.command(cmd)
        results = client_push_responses(imap_client)
        assert results == expected
