"""
Test the user server.
"""

# system imports
#
from datetime import datetime
from pathlib import Path

# 3rd party imports
#
import pytest
from dirty_equals import IsDatetime

# Project imports
#
from ..client import Authenticated
from ..mbox import Mailbox
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


####################################################################
#
@pytest.mark.asyncio
async def test_expire_inactive_folders(
    faker, mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email

    # Let us make several other folders.
    #
    folders = ["inbox"]
    for _ in range(5):
        folder_name = faker.word()
        await Mailbox.create(folder_name, server)
        folders.append(folder_name)
        for _ in range(3):
            sub_folder = f"{folder_name}/{faker.word()}"
            if sub_folder in folders:
                continue
            await Mailbox.create(sub_folder, server)
            folders.append(sub_folder)

    folders = sorted(folders)

    client_handler = Authenticated(imap_client, server)

    # Get a handle on two mailboxes.
    #
    mbox1 = await server.get_mailbox(folders[2])
    mbox2 = await server.get_mailbox(folders[3])

    # Select the inbox (so we have one folder with no expiry time at all)
    #
    cmd = IMAPClientCommand("A001 SELECT INBOX\r\n")
    cmd.parse()
    await client_handler.command(cmd)

    # We should have three active mailboxes now.
    #
    assert len(server.active_mailboxes) == len(folders)

    # The inbox will have no expiry since a client has it selected.
    #
    inbox = await server.get_mailbox("inbox")
    assert inbox.expiry is None

    # mbox1 and mbox2 have a positive expiry.
    #
    assert mbox1.expiry == IsDatetime(ge=datetime.now(), unix_number=True)
    assert mbox2.expiry == IsDatetime(ge=datetime.now(), unix_number=True)

    await server.expire_inactive_folders()

    # no expiries since they all hvae expiry times in the future.
    #
    assert len(server.active_mailboxes) == len(folders)

    # For mbox1's expiry time back to the unix epoch.
    #
    mbox1.expiry = 0.0
    await server.expire_inactive_folders()
    assert len(server.active_mailboxes) == len(folders) - 1
    assert mbox1.name not in server.active_mailboxes


####################################################################
#
@pytest.mark.asyncio
async def test_find_all_folders(
    faker, mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email

    # Let us make several other folders.
    #
    folders = ["inbox"]
    for _ in range(5):
        folder_name = faker.word()
        fpath = Path(server.mailbox._path) / folder_name
        fpath.mkdir()
        folders.append(folder_name)
        for _ in range(3):
            sub_folder = f"{folder_name}/{faker.word()}"
            if sub_folder in folders:
                continue
            fpath = Path(server.mailbox._path) / sub_folder
            fpath.mkdir()
            folders.append(sub_folder)

    folders = sorted(folders)

    await server.find_all_folders()

    # After it finds all the folders they will be active for a bit.
    #
    assert len(server.active_mailboxes) == len(folders)

    # and they should each be in the active mailboxes dict.
    #
    for folder in folders:
        assert folder in server.active_mailboxes


####################################################################
#
@pytest.mark.asyncio
async def test_check_folder(
    faker, mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client = imap_user_server_and_client
    mbox = mailbox_with_bunch_of_email

    # This is testing the code paths in this method alone making sure nothing
    # breaks.
    #
    await server.check_folder(mbox.name, 0, force=False)
    await server.check_folder(mbox.name, 0, force=True)


####################################################################
#
@pytest.mark.asyncio
async def test_check_all_folders(
    faker, mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email

    # Let us make several other folders.
    #
    folders = ["inbox"]
    for _ in range(5):
        folder_name = faker.word()
        await Mailbox.create(folder_name, server)
        folders.append(folder_name)
        for _ in range(3):
            sub_folder = f"{folder_name}/{faker.word()}"
            if sub_folder in folders:
                continue
            await Mailbox.create(sub_folder, server)
            folders.append(sub_folder)

    # select and idle on the inbox
    #
    client_handler = Authenticated(imap_client, server)
    cmd = IMAPClientCommand("A001 SELECT INBOX\r\n")
    cmd.parse()
    await client_handler.command(cmd)
    cmd = IMAPClientCommand("A002 IDLE\r\n")
    cmd.parse()
    await client_handler.command(cmd)

    # basically all the sub-components of this action are already tested.
    # We are making sure that this code that invokes them runs.
    #
    await server.check_all_folders(force=True)

    # And stop idling on the inbox.
    #
    await client_handler.do_done()
