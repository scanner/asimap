"""Test importing messages from the delivery spool."""

# system imports
#
import asyncio
import os
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 3rd party imports
#
import pytest
import pytest_asyncio
import pytest_check as check
from asimap_spool import (
    FAILED_DIR_NAME,
    INCOMING_SUFFIX,
    READY_SUFFIX,
    SPOOL_DIR_NAME,
    Delivery,
    deliver,
    uuid7,
)
from pytest_mock import MockerFixture

# Project imports
#
from ..mbox import InvalidMailbox, Mailbox, NoSuchMailbox
from ..spool_importer import MAX_IMPORT_ATTEMPTS, SpoolImporter, SpoolSettings
from ..user_server import IMAPUserServer

RAW_MESSAGE = (
    b"From: a@example.com\nTo: b@example.com\nSubject: caf\xe9\n\n"
    b"na\xefve body \xff\n"
)
RECEIVED_AT = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


####################################################################
#
@pytest.fixture
def importer(imap_user_server: IMAPUserServer) -> SpoolImporter:
    """
    A spool importer for the test user server, with its spool created and
    the file watcher off, so each test drives `drain()` itself.
    """
    imp = SpoolImporter(imap_user_server, SpoolSettings(watch=False))
    imp._ensure_dirs()
    return imp


####################################################################
#
@pytest.fixture
def spool_message(
    imap_user_server: IMAPUserServer,
) -> Callable[..., Path]:
    """Factory: spool `RAW_MESSAGE` with the given deliveries."""

    def make(*deliveries: Delivery) -> Path:
        return deliver(
            imap_user_server.maildir,
            RAW_MESSAGE,
            deliveries,
            received_at=RECEIVED_AT,
            source="test",
        )

    return make


####################################################################
#
@pytest.fixture
def folder_contents(
    imap_user_server: IMAPUserServer,
) -> Callable[[str], Any]:
    """
    Factory: return `(raw bytes, sequences)` for every message in a
    folder, by message key.
    """

    async def contents(name: str) -> list[tuple[bytes, list[str]]]:
        mbox = await imap_user_server.get_mailbox(name)
        return [
            (
                mbox.mailbox.get_message_path(int(key)).read_bytes(),
                sorted(mbox.msg_sequences(int(key))),
            )
            for key in sorted(mbox.mailbox.keys(), key=int)
        ]

    return contents


####################################################################
#
@pytest.fixture
def failing_append(mocker: MockerFixture) -> Any:
    """Make every `Mailbox.append` raise."""
    return mocker.patch.object(
        Mailbox, "append", side_effect=RuntimeError("disk on fire")
    )


####################################################################
#
@pytest.fixture
def failing_append_to(mocker: MockerFixture) -> Callable[[str], Any]:
    """Factory: make `Mailbox.append` raise for one mailbox name only."""
    original = Mailbox.append

    def patch(name: str) -> Any:
        async def append(self: Mailbox, *args: Any, **kwargs: Any) -> int:
            if self.name == name:
                raise RuntimeError("disk on fire")
            return await original(self, *args, **kwargs)

        return mocker.patch.object(Mailbox, "append", append)

    return patch


####################################################################
#
@pytest.fixture
def failing_unlink(mocker: MockerFixture) -> Any:
    """Make removing any path raise PermissionError."""
    return mocker.patch.object(
        Path, "unlink", side_effect=PermissionError("read-only spool")
    )


####################################################################
#
@pytest_asyncio.fixture
async def running_importer(
    imap_user_server: IMAPUserServer,
) -> AsyncGenerator[Callable[[SpoolSettings], None], None]:
    """
    Factory: start the importer's `run()` loop with the given settings.
    The task is cancelled at teardown.
    """
    tasks: list[asyncio.Task] = []

    def start(settings: SpoolSettings) -> None:
        imp = SpoolImporter(imap_user_server, settings)
        tasks.append(asyncio.create_task(imp.run()))

    yield start

    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


####################################################################
#
@pytest.mark.asyncio
async def test_drain_imports_to_inbox(
    importer: SpoolImporter,
    spool_message: Callable[..., Path],
    folder_contents: Callable[[str], Any],
) -> None:
    """
    GIVEN: a spooled message with no deliveries
    WHEN:  the spool is drained
    THEN:  the inbox holds the exact bytes, unseen and recent, dated
           `received_at`; the spool file is gone and its id is recorded
    """
    path = spool_message()

    imported = await importer.drain()

    mbox = await importer.server.get_mailbox("inbox")
    key = int(mbox.mailbox.keys()[0])
    check.equal(imported, 1)
    check.equal(
        await folder_contents("inbox"), [(RAW_MESSAGE, ["Recent", "unseen"])]
    )
    check.equal(
        mbox.mailbox.get_message_path(key).stat().st_mtime,
        RECEIVED_AT.timestamp(),
    )
    check.is_false(path.exists())
    check.is_true(await importer._already_imported(path.name[:36]))


####################################################################
#
@pytest.mark.parametrize(
    "deliveries,expected",
    [
        # Missing folder that may be created.
        (
            [Delivery("Lists/python", ("\\Seen",), create=True)],
            {"Lists/python": [["Recent", "Seen"]]},
        ),
        # Missing folder that may not be created goes to the inbox.
        (
            [Delivery("Nowhere")],
            {"inbox": [["Recent", "unseen"]]},
        ),
        # Two deliveries to one folder store one copy with merged flags.
        (
            [Delivery("inbox", ("\\Flagged",)), Delivery("INBOX", ("$Work",))],
            {"inbox": [["$Work", "Recent", "flagged", "unseen"]]},
        ),
        # A folder name MH will not create goes to the inbox.
        (
            [Delivery("2024", create=True)],
            {"inbox": [["Recent", "unseen"]]},
        ),
        # Several folders each get a copy.
        (
            [Delivery("inbox"), Delivery("Archive", create=True)],
            {
                "inbox": [["Recent", "unseen"]],
                "Archive": [["Recent", "unseen"]],
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_drain_deliveries(
    deliveries: list[Delivery],
    expected: dict[str, list[list[str]]],
    importer: SpoolImporter,
    spool_message: Callable[..., Path],
    folder_contents: Callable[[str], Any],
) -> None:
    """
    GIVEN: a spooled message with the given deliveries
    WHEN:  the spool is drained
    THEN:  each target folder holds one copy with the expected sequences
    """
    spool_message(*deliveries)

    await importer.drain()

    for folder, seqs in expected.items():
        contents = await folder_contents(folder)
        check.equal([s for _, s in contents], seqs, folder)
        check.is_true(all(raw == RAW_MESSAGE for raw, _ in contents), folder)


####################################################################
#
@pytest.mark.asyncio
async def test_drain_skips_already_imported(
    importer: SpoolImporter,
    spool_message: Callable[..., Path],
    folder_contents: Callable[[str], Any],
) -> None:
    """
    GIVEN: a spooled entry whose id is already recorded as imported
    WHEN:  the spool is drained
    THEN:  the file is removed and nothing is appended again
    """
    path = spool_message()
    await importer._record_imported(path.name[:36])

    imported = await importer.drain()

    check.equal(imported, 0)
    check.is_false(path.exists())
    check.equal(await folder_contents("inbox"), [])


####################################################################
#
@pytest.mark.asyncio
async def test_drain_moves_bad_and_stale_files_to_failed(
    importer: SpoolImporter, folder_contents: Callable[[str], Any]
) -> None:
    """
    GIVEN: a ready file that is not a valid entry, an incoming file older
           than the stale age, and a fresh incoming file
    WHEN:  the spool is drained
    THEN:  the bad and stale files are in failed/, the fresh one is left
           alone, and nothing is delivered
    """
    spool = importer.spool
    bad = spool / f"{uuid7()}{READY_SUFFIX}"
    bad.write_text("{}")
    stale = spool / f"{uuid7()}{INCOMING_SUFFIX}"
    stale.touch()
    old = datetime.now().timestamp() - 7200
    os.utime(stale, (old, old))
    fresh = spool / f"{uuid7()}{INCOMING_SUFFIX}"
    fresh.touch()

    await importer.drain()

    check.equal(
        sorted(os.listdir(spool / FAILED_DIR_NAME)),
        sorted([bad.name, stale.name]),
    )
    check.is_true(fresh.exists())
    check.equal(await folder_contents("inbox"), [])


####################################################################
#
@pytest.mark.asyncio
async def test_drain_retries_then_fails(
    importer: SpoolImporter,
    spool_message: Callable[..., Path],
    failing_append: Any,
) -> None:
    """
    GIVEN: an append that always fails
    WHEN:  the spool is drained repeatedly
    THEN:  the entry stays in the spool until its last attempt, then moves
           to failed/
    """
    path = spool_message()

    for _ in range(MAX_IMPORT_ATTEMPTS - 1):
        await importer.drain()
        check.is_true(path.exists())

    await importer.drain()

    check.is_false(path.exists())
    check.is_true((importer.spool / FAILED_DIR_NAME / path.name).exists())
    check.equal(failing_append.call_count, MAX_IMPORT_ATTEMPTS)


####################################################################
#
@pytest.mark.parametrize("watch,poll_interval", [(True, 60.0), (False, 0.1)])
@pytest.mark.asyncio
async def test_run_imports_new_messages(
    watch: bool,
    poll_interval: float,
    running_importer: Callable[[SpoolSettings], None],
    spool_message: Callable[..., Path],
    folder_contents: Callable[[str], Any],
) -> None:
    """
    GIVEN: a running importer that either watches the spool (with a poll
           interval too long to matter) or only polls
    WHEN:  a message is spooled
    THEN:  it reaches the inbox
    """
    running_importer(SpoolSettings(poll_interval=poll_interval, watch=watch))
    await asyncio.sleep(0.5)

    path = spool_message()

    async with asyncio.timeout(10):
        while path.exists():
            await asyncio.sleep(0.05)
    assert len(await folder_contents("inbox")) == 1


####################################################################
#
@pytest.mark.asyncio
async def test_spool_dir_is_not_a_mailbox(
    imap_user_server: IMAPUserServer, importer: SpoolImporter
) -> None:
    """
    GIVEN: a mail store with a spool directory
    WHEN:  folders are scanned, and the spool name is selected, created,
           or used as a rename target
    THEN:  the spool is never a mailbox
    """
    server = imap_user_server
    await server.find_all_folders()

    names = [
        row[0] async for row in server.db.query("SELECT name FROM mailboxes")
    ]
    check.is_false(any(n.startswith(SPOOL_DIR_NAME) for n in names))
    with check.raises(NoSuchMailbox):
        await server.get_mailbox(SPOOL_DIR_NAME)
    with check.raises(InvalidMailbox):
        await Mailbox.create(f"{SPOOL_DIR_NAME}/sub", server)
    with check.raises(InvalidMailbox):
        await Mailbox.rename("inbox", SPOOL_DIR_NAME, server)


####################################################################
#
@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, SpoolSettings()),
        (
            {
                "SPOOL_POLL_INTERVAL": "2.5",
                "SPOOL_STALE_INCOMING_AGE": "60",
                "SPOOL_WATCH": "no",
            },
            SpoolSettings(
                poll_interval=2.5, stale_incoming_age=60, watch=False
            ),
        ),
        ({"SPOOL_POLL_INTERVAL": "soon"}, SpoolSettings()),
    ],
)
def test_spool_settings_from_env(
    env: dict[str, str],
    expected: SpoolSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    GIVEN: spool settings in the environment (or none, or a bad value)
    WHEN:  settings are read
    THEN:  set values are used and unset or bad ones fall back to defaults
    """
    for name in (
        "SPOOL_POLL_INTERVAL",
        "SPOOL_STALE_INCOMING_AGE",
        "SPOOL_WATCH",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert SpoolSettings.from_env() == expected


####################################################################
#
@pytest.mark.asyncio
async def test_drain_retry_does_not_duplicate(
    importer: SpoolImporter,
    spool_message: Callable[..., Path],
    folder_contents: Callable[[str], Any],
    failing_append_to: Callable[[str], Any],
) -> None:
    """
    GIVEN: an entry for the inbox and a second folder whose append always
           fails
    WHEN:  the spool is drained until the entry is given up on
    THEN:  the inbox holds exactly one copy
    """
    failing_append_to("Other")
    path = spool_message(Delivery("inbox"), Delivery("Other", create=True))

    for _ in range(MAX_IMPORT_ATTEMPTS):
        await importer.drain()

    check.is_false(path.exists())
    check.equal(len(await folder_contents("inbox")), 1)


####################################################################
#
@pytest.mark.asyncio
async def test_drain_continues_past_unreadable_file(
    importer: SpoolImporter,
    spool_message: Callable[..., Path],
    folder_contents: Callable[[str], Any],
) -> None:
    """
    GIVEN: an unreadable ready file ahead of a good one
    WHEN:  the spool is drained
    THEN:  the unreadable file goes to failed/ and the good one is
           delivered
    """
    unreadable = importer.spool / f"{uuid7()}{READY_SUFFIX}"
    unreadable.write_text("{}")
    unreadable.chmod(0)
    good = spool_message()

    await importer.drain()

    check.is_true((importer.spool / FAILED_DIR_NAME / unreadable.name).exists())
    check.is_false(good.exists())
    check.equal(len(await folder_contents("inbox")), 1)


####################################################################
#
@pytest.mark.asyncio
async def test_drain_survives_unremovable_files(
    importer: SpoolImporter,
    spool_message: Callable[..., Path],
    folder_contents: Callable[[str], Any],
    failing_unlink: Any,
) -> None:
    """
    GIVEN: two spooled entries that can not be removed once imported
    WHEN:  the spool is drained twice
    THEN:  both are delivered once; the second pass delivers nothing new
    """
    spool_message()
    spool_message()

    first = await importer.drain()
    second = await importer.drain()

    check.equal(first, 2)
    check.equal(second, 0)
    check.equal(len(await folder_contents("inbox")), 2)


####################################################################
#
@pytest.fixture
def old_imported_ids(importer: SpoolImporter) -> Callable[..., Any]:
    """
    Factory: record ids as imported two days ago, returning them.
    `with_file` also leaves each id's ready file in the spool.
    """

    async def make(count: int, with_file: bool) -> list[str]:
        ids = [uuid7() for _ in range(count)]
        when = datetime.now().timestamp() - 2 * 86400
        for entry_id in ids:
            await importer.server.db.execute(
                "INSERT INTO spool_imported (id, imported_at) VALUES (?, ?)",
                (entry_id, when),
                commit=True,
            )
            if with_file:
                (importer.spool / f"{entry_id}{READY_SUFFIX}").write_text("{}")
        return ids

    return make


####################################################################
#
@pytest.mark.asyncio
async def test_prune_keeps_ids_whose_file_remains(
    importer: SpoolImporter,
    old_imported_ids: Callable[..., Any],
) -> None:
    """
    GIVEN: old imported ids, some whose ready file is gone and some whose
           file is still in the spool
    WHEN:  imported ids are pruned
    THEN:  only the ids whose file is gone are forgotten
    """
    gone = await old_imported_ids(2, with_file=False)
    remaining = await old_imported_ids(2, with_file=True)

    await importer._prune_imported()

    for entry_id in gone:
        check.is_false(await importer._already_imported(entry_id), entry_id)
    for entry_id in remaining:
        check.is_true(await importer._already_imported(entry_id), entry_id)
