"""
Test the user server.
"""

# system imports
#
from collections.abc import Callable
from mailbox import MH
from pathlib import Path

# 3rd party imports
#
import pytest
from faker import Faker

# Project imports
#
from ..client import Authenticated
from ..mbox import Mailbox, NoSuchMailbox
from ..parse import IMAPClientCommand
from ..user_server import IMAPClientProxy, IMAPUserServer


####################################################################
#
@pytest.mark.asyncio
async def test_user_server_instantiate(
    mh_folder: Callable[..., tuple[Path, MH, MH]],
) -> None:
    (mh_dir, _, _) = mh_folder()
    try:
        user_server = await IMAPUserServer.new(mh_dir)
        assert user_server
    finally:
        await user_server.shutdown()


####################################################################
#
@pytest.mark.asyncio
async def test_find_all_folders(
    faker: Faker,
    mailbox_with_bunch_of_email: Mailbox,
    imap_user_server_and_client: tuple[IMAPUserServer, IMAPClientProxy],
) -> None:
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
    faker: Faker,
    mailbox_with_bunch_of_email: Mailbox,
    imap_user_server_and_client: tuple[IMAPUserServer, IMAPClientProxy],
) -> None:
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
async def test_check_folder_removes_stale_db_entry(
    mailbox_with_bunch_of_email: Mailbox,
    imap_user_server_and_client: tuple[IMAPUserServer, IMAPClientProxy],
) -> None:
    """
    GIVEN: a mailbox that exists in the database but whose folder has been
           removed from disk (e.g., by an external MH client)
    WHEN:  check_folder is called for that mailbox
    THEN:  the stale database entry should be cleaned up
    """
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email

    # Create a folder, make sure it's in the DB
    #
    folder_name = "test_stale_folder"
    await Mailbox.create(folder_name, server)
    mbox = await server.get_mailbox(folder_name)
    assert mbox is not None

    # Verify it exists in the database
    #
    row = await server.db.fetchone(
        "SELECT name FROM mailboxes WHERE name = ?", (folder_name,)
    )
    assert row is not None
    assert row[0] == folder_name

    # Remove the folder from disk (simulating an external MH client
    # removing it) and remove it from active mailboxes so check_folder
    # will try to re-activate it.
    #
    import shutil

    folder_path = Path(server.mailbox._path) / folder_name
    shutil.rmtree(folder_path)
    async with server.active_mailboxes_lock:
        server.active_mailboxes.pop(folder_name, None)

    # Now check_folder should detect the folder is gone and clean up
    # the database entry.
    #
    await server.check_folder(folder_name, 0, force=True)

    # The database entry should be gone
    #
    row = await server.db.fetchone(
        "SELECT name FROM mailboxes WHERE name = ?", (folder_name,)
    )
    assert row is None

    # And it should not be in active_mailboxes
    #
    assert folder_name not in server.active_mailboxes


####################################################################
#
@pytest.mark.asyncio
async def test_there_is_a_root_folder(imap_user_server: IMAPUserServer) -> None:
    server = imap_user_server
    # In an attempt to see if the root folder would fix iOS 18's IMAP problems
    # we allow the root folder to exist. (But it still did not fix iOS 18)
    #
    with pytest.raises(NoSuchMailbox):
        _ = await server.get_mailbox("")


####################################################################
#
@pytest.mark.asyncio
async def test_check_all_folders(
    faker: Faker,
    mailbox_with_bunch_of_email: Mailbox,
    imap_user_server_and_client: tuple[IMAPUserServer, IMAPClientProxy],
) -> None:
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email

    # Let us make several other folders.
    #
    folders = ["inbox"]
    for _ in range(5):
        folder_name = faker.word()
        await Mailbox.create(folder_name, server)
        # Make sure folder exists and is active
        #
        await server.get_mailbox(folder_name)
        folders.append(folder_name)

        for _ in range(3):
            sub_folder = f"{folder_name}/{faker.word()}"
            if sub_folder in folders:
                continue
            await Mailbox.create(sub_folder, server)
            # Make sure folder exists and is active
            #
            await server.get_mailbox(sub_folder)
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

    # basically all the sub-components of this action are already tested.  We
    # are making sure that this code that invokes them runs. Turn debug on for
    # the server to test the debugging log statements with statistics.
    #
    server.debug = True
    await server.check_all_folders(force=True)

    # And stop idling on the inbox.
    #
    await client_handler.do_done()
