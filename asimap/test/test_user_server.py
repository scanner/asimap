"""
Test the user server.
"""

# system imports
#
import asyncio
from collections.abc import Callable
from mailbox import MH
from pathlib import Path
from typing import Any

# 3rd party imports
#
import pytest
from faker import Faker
from pytest_mock import MockerFixture

# Project imports
#
from ..client import Authenticated
from ..constants import SPECIAL_USE_ATTRS
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

    # find_all_folders() also auto-creates SPECIAL-USE mailboxes. Add
    # any that were not already present to our expected list.
    #
    for su_folder in SPECIAL_USE_ATTRS:
        if su_folder not in folders:
            folders.append(su_folder)
    folders = sorted(folders)

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


####################################################################
#
@pytest.mark.asyncio
async def test_caretaker_pass_resyncs_idle_mailbox(
    bunch_of_email_in_folder: Callable[..., Path],
    mailbox_with_bunch_of_email: Mailbox,
    imap_user_server: IMAPUserServer,
) -> None:
    """
    GIVEN: an active mailbox with no clients, no executing commands, and no
           queued commands
    WHEN:  new messages are delivered to the folder by an external agent and
           the caretaker does a pass
    THEN:  the caretaker resyncs the mailbox, picking up the new messages
    """
    server = imap_user_server
    mbox = mailbox_with_bunch_of_email
    num_msgs = mbox.num_msgs

    # Deliver new messages "externally" (directly in to the MH folder.)
    #
    bunch_of_email_in_folder(num_emails=3, folder=mbox.name)

    # The folder mtime has one second granularity so the delivery above may
    # not be distinguishable from the resync done when the mailbox was
    # instantiated. Clearing `optional_resync` forces the next resync to
    # actually scan the folder.
    #
    mbox.optional_resync = False

    await server.caretaker_pass()

    assert mbox.num_msgs == num_msgs + 3
    assert server.poll_stats["caretaker_passes"] == 1
    assert server.poll_stats["caretaker_checked"] >= 1


####################################################################
#
@pytest.mark.asyncio
async def test_caretaker_skips_busy_mailboxes(
    mocker: MockerFixture,
    mailbox_with_bunch_of_email: Mailbox,
    imap_user_server: IMAPUserServer,
) -> None:
    r"""
    GIVEN: a mailbox that has clients, executing commands, queued commands,
           or the `\Noselect` attribute
    WHEN:  the caretaker does a pass
    THEN:  it does not resync that mailbox
    """
    server = imap_user_server
    mbox = mailbox_with_bunch_of_email

    # Cancel the management task so we can put commands on the task queue
    # without them being consumed.
    #
    mbox.mgmt_task.cancel()

    idle_resync = mocker.patch.object(mbox, "idle_resync")

    # Has a client.
    #
    mbox.clients["fake_client"] = mocker.Mock()
    await server.caretaker_pass()
    idle_resync.assert_not_awaited()
    mbox.clients.clear()

    # Has an executing command.
    #
    cmd = IMAPClientCommand("A001 NOOP\r\n").parse()
    mbox.executing_tasks.append(cmd)
    await server.caretaker_pass()
    idle_resync.assert_not_awaited()
    mbox.executing_tasks.clear()

    # Has a queued command.
    #
    mbox.task_queue.put_nowait(cmd)
    await server.caretaker_pass()
    idle_resync.assert_not_awaited()
    mbox.task_queue.get_nowait()

    # Has the `\Noselect` attribute.
    #
    mbox.attributes.add(r"\Noselect")
    await server.caretaker_pass()
    idle_resync.assert_not_awaited()
    mbox.attributes.discard(r"\Noselect")

    # And with none of the above the caretaker checks the mailbox.
    #
    await server.caretaker_pass()
    idle_resync.assert_awaited_once()


####################################################################
#
@pytest.mark.asyncio
async def test_caretaker_management_task_mutual_exclusion(
    mailbox_with_bunch_of_email: Mailbox,
    imap_user_server: IMAPUserServer,
) -> None:
    """
    GIVEN: the caretaker holding a mailbox's resync_lock (as it does while
           checking that mailbox)
    WHEN:  an IMAP command arrives at the mailbox's management task
    THEN:  the command is not allowed to begin executing until the lock is
           released
    """
    mbox = mailbox_with_bunch_of_email

    cmd = IMAPClientCommand("A001 NOOP\r\n").parse()

    async def run_cmd() -> None:
        async with cmd.ready_and_okay(mbox):
            pass

    async with mbox.resync_lock:
        cmd_task = asyncio.create_task(run_cmd())
        # Give the management task ample time to (incorrectly) let the
        # command proceed.
        #
        await asyncio.sleep(0.2)
        assert not cmd.ready.is_set()

    # Once the lock is released the management task finishes its pre-command
    # resync and lets the command proceed.
    #
    await asyncio.wait_for(cmd_task, timeout=5)
    assert cmd.ready.is_set()


####################################################################
#
@pytest.mark.asyncio
async def test_caretaker_resyncs_are_serialized(
    mocker: MockerFixture,
    mailbox_with_bunch_of_email: Mailbox,
    imap_user_server: IMAPUserServer,
) -> None:
    """
    GIVEN: the caretaker and other tasks calling `idle_resync` concurrently
    WHEN:  they all run
    THEN:  `check_new_msgs_and_flags` never runs concurrently with itself on
           the same mailbox
    """
    server = imap_user_server
    mbox = mailbox_with_bunch_of_email

    active = 0
    max_active = 0
    orig_check = mbox.check_new_msgs_and_flags

    async def instrumented(*args: Any, **kwargs: Any) -> bool:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            # Give the other callers a chance to overlap with us.
            #
            await asyncio.sleep(0.05)
            return await orig_check(*args, **kwargs)
        finally:
            active -= 1

    mocker.patch.object(
        mbox, "check_new_msgs_and_flags", side_effect=instrumented
    )

    await asyncio.gather(
        server.caretaker_pass(),
        mbox.idle_resync(),
        mbox.idle_resync(),
    )

    assert max_active == 1
    assert active == 0
