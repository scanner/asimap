"""
Test the user server.
"""

# system imports
#
from pathlib import Path

# 3rd party imports
#
import pytest

# Project imports
#
from ..client import Authenticated
from ..mbox import Mailbox
from ..parse import IMAPClientCommand
from ..user_server import IMAPUserServer


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
            async with server.get_mailbox(sub_folder) as sf:
                assert sf.in_use_count == 1
                folders.append(sub_folder)

    folders = sorted(folders)

    client_handler = Authenticated(imap_client, server)

    # Expire all the mailboxes we made so `check_all_folders` will check them.
    #
    await server.expire_inactive_folders()

    # Get a handle on two mailboxes and increment their in-use count so that
    # they do not get expired.
    #
    async with server.get_mailbox(folders[2]) as mbox1:
        async with server.get_mailbox(folders[3]) as mbox2:

            # Using the mailbox as a context manager increases the in-use count.
            #
            with mbox1:
                assert mbox1.in_use_count == 2
                assert mbox2.in_use_count == 1

            # Select the inbox (so we have one folder with no expiry time at
            # all)
            #
            cmd = IMAPClientCommand("A001 SELECT INBOX\r\n")
            cmd.parse()
            await client_handler.command(cmd)

            # We should have three active mailboxes now (two protected by async
            # with clauses and one selected by a client.)
            #
            assert len(server.active_mailboxes) == 3

            await server.expire_inactive_folders()

            # and after an expiry check again, still 3 active folders.
            #
            assert len(server.active_mailboxes) == 3

        # We have exited mbox2's async with clause which should make it
        # available for expiry.
        #
        await server.expire_inactive_folders()
        assert len(server.active_mailboxes) == 2
        assert mbox2.name not in server.active_mailboxes


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
async def test_there_is_a_root_folder(imap_user_server):
    server = imap_user_server
    # with pytest.raises(NoSuchMailbox):
    async with server.get_mailbox(""):
        pass


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
            async with server.get_mailbox(sub_folder):
                pass

    # Expire all the mailboxes we made so `check_all_folders` will check them.
    # (Since there are no clients selecting a mailbox, and all the mailboxes we
    # created above have their in_use_count==0 they should all get expired.)
    #
    await server.expire_inactive_folders()

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
