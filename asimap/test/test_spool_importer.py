"""Test importing messages from the delivery spool."""

# system imports
#
import asyncio
import os
import time
from collections.abc import AsyncGenerator, Callable, Generator
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

# 8-bit, non-UTF-8 content and a folded header that the email generator
# would rewrite, so a byte-for-byte match proves the raw bytes were stored.
#
RAW_MESSAGE = (
    b"From: a@example.com\nTo: b@example.com\nSubject: caf\xe9\n"
    b"X-Folded:   first\n\t\t second   \n\n"
    b"na\xefve body \xff\n"
)
RECEIVED_AT = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

SPOOL_ENV = ("SPOOL_POLL_INTERVAL", "SPOOL_STALE_INCOMING_AGE", "SPOOL_WATCH")


####################################################################
#
@pytest.fixture
def importer(imap_user_server: IMAPUserServer) -> SpoolImporter:
    """
    A spool importer for the test user server, with its spool created and
    the file watcher off, so each test drives `drain()` itself.
    """
    imp = SpoolImporter(imap_user_server, SpoolSettings(watch=False))
    assert imp.check_spool() is None
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
    Factory: return `(raw bytes, sequences, mtime)` for every message in a
    folder, in message key order.
    """

    async def contents(name: str) -> list[tuple[bytes, list[str], float]]:
        mbox = await imap_user_server.get_mailbox(name)
        result = []
        for key in sorted(mbox.mailbox.keys(), key=int):
            path = mbox.mailbox.get_message_path(int(key))
            result.append(
                (
                    path.read_bytes(),
                    sorted(mbox.msg_sequences(int(key))),
                    path.stat().st_mtime,
                )
            )
        return result

    return contents


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
@pytest.fixture
def read_only_spool(importer: SpoolImporter) -> Generator[Path, None, None]:
    """The importer's spool directory, made read-only for the test."""
    importer.spool.chmod(0o500)
    yield importer.spool
    importer.spool.chmod(0o700)


####################################################################
#
@pytest.fixture
def bad_spool_files(importer: SpoolImporter) -> dict[str, Path]:
    """
    Files the importer must not deliver: an invalid ready entry, an
    unreadable one, an abandoned (two hour old) incoming file, and a fresh
    incoming file still being written.
    """
    spool = importer.spool
    files = {
        "invalid": spool / f"{uuid7()}{READY_SUFFIX}",
        "unreadable": spool / f"{uuid7()}{READY_SUFFIX}",
        "stale": spool / f"{uuid7()}{INCOMING_SUFFIX}",
        "fresh": spool / f"{uuid7()}{INCOMING_SUFFIX}",
    }
    for path in files.values():
        path.write_text("{}")
    files["unreadable"].chmod(0)
    old = time.time() - 7200
    os.utime(files["stale"], (old, old))
    return files


####################################################################
#
@pytest.fixture
def old_imported_id(importer: SpoolImporter) -> Callable[..., Any]:
    """
    Factory: record an id as imported two days ago and return it.
    `with_file` also leaves its ready file in the spool.
    """

    async def make(with_file: bool) -> str:
        entry_id = uuid7()
        await importer.server.db.execute(
            "INSERT INTO spool_imported (id, imported_at) VALUES (?, ?)",
            (entry_id, time.time() - 2 * 86400),
            commit=True,
        )
        if with_file:
            (importer.spool / f"{entry_id}{READY_SUFFIX}").write_text("{}")
        return entry_id

    return make


####################################################################
#
@pytest.fixture
def spool_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Factory: clear the spool settings from the environment, then set
    the given ones."""

    def set_env(**env: str) -> None:
        for name in SPOOL_ENV:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)

    return set_env


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
@pytest.mark.parametrize(
    "deliveries,expected",
    [
        # No deliveries means the inbox, unseen.
        ([], {"inbox": [["Recent", "unseen"]]}),
        # Each folder gets a copy; a missing one is created on request.
        (
            [
                Delivery("inbox"),
                Delivery("Lists/python", ("\\Seen", "$Work"), create=True),
            ],
            {
                "inbox": [["Recent", "unseen"]],
                "Lists/python": [["$Work", "Recent", "Seen"]],
            },
        ),
        # Two deliveries to one folder store one copy with merged flags.
        (
            [Delivery("inbox", ("\\Flagged",)), Delivery("INBOX", ("$Work",))],
            {"inbox": [["$Work", "Recent", "flagged", "unseen"]]},
        ),
        # A missing folder that may not be created goes to the inbox.
        ([Delivery("Nowhere")], {"inbox": [["Recent", "unseen"]]}),
        # So does a folder MH refuses to create.
        (
            [Delivery("2024", create=True)],
            {"inbox": [["Recent", "unseen"]]},
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
    THEN:  each target folder holds one copy of the exact bytes, dated
           `received_at`, with the expected sequences; the spool file is
           gone and its id is recorded
    """
    path = spool_message(*deliveries)

    imported = await importer.drain()

    check.equal(imported, 1)
    check.is_false(path.exists())
    check.is_true(
        await importer._already_imported(path.name.removesuffix(READY_SUFFIX))
    )
    for folder, seqs in expected.items():
        check.equal(
            await folder_contents(folder),
            [(RAW_MESSAGE, s, RECEIVED_AT.timestamp()) for s in seqs],
            folder,
        )


####################################################################
#
@pytest.mark.asyncio
async def test_drain_moves_bad_and_stale_files_to_failed(
    importer: SpoolImporter,
    bad_spool_files: dict[str, Path],
    spool_message: Callable[..., Path],
    folder_contents: Callable[[str], Any],
) -> None:
    """
    GIVEN: invalid, unreadable, abandoned, and in-progress spool files,
           and a good entry after them
    WHEN:  the spool is drained
    THEN:  the invalid, unreadable, and abandoned files are in failed/,
           the in-progress one is left alone, and the good entry is
           delivered
    """
    good = spool_message()

    await importer.drain()

    check.equal(
        sorted(os.listdir(importer.spool / FAILED_DIR_NAME)),
        sorted(
            bad_spool_files[k].name for k in ("invalid", "unreadable", "stale")
        ),
    )
    check.is_true(bad_spool_files["fresh"].exists())
    check.is_false(good.exists())
    check.equal(len(await folder_contents("inbox")), 1)


####################################################################
#
@pytest.mark.asyncio
async def test_drain_retries_then_fails_without_duplicating(
    importer: SpoolImporter,
    spool_message: Callable[..., Path],
    folder_contents: Callable[[str], Any],
    failing_append_to: Callable[[str], Any],
) -> None:
    """
    GIVEN: an entry for the inbox and a second folder whose append always
           fails
    WHEN:  the spool is drained repeatedly
    THEN:  the entry stays in the spool until its last attempt, then moves
           to failed/, and the inbox holds exactly one copy
    """
    failing_append_to("Other")
    path = spool_message(Delivery("inbox"), Delivery("Other", create=True))

    for _ in range(MAX_IMPORT_ATTEMPTS - 1):
        await importer.drain()
        check.is_true(path.exists())
    await importer.drain()

    check.is_true((importer.spool / FAILED_DIR_NAME / path.name).exists())
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
    THEN:  both are delivered once; the second pass skips them as already
           imported
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
@pytest.mark.asyncio
async def test_prune_keeps_ids_whose_file_remains(
    importer: SpoolImporter, old_imported_id: Callable[..., Any]
) -> None:
    """
    GIVEN: two old imported ids, one whose ready file is gone and one
           whose file is still in the spool
    WHEN:  imported ids are pruned
    THEN:  only the id whose file is gone is forgotten
    """
    gone = await old_imported_id(with_file=False)
    remaining = await old_imported_id(with_file=True)

    await importer._prune_imported()

    check.is_false(await importer._already_imported(gone))
    check.is_true(await importer._already_imported(remaining))


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
async def test_unusable_spool_is_reported_once_and_recovers(
    importer: SpoolImporter,
    read_only_spool: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    GIVEN: a spool directory the user server can not write to
    WHEN:  the spool is checked on several passes, then access is restored
    THEN:  it is unusable with one error logged, then usable again
    """
    for _ in range(3):
        check.is_false(await importer.spool_usable())
    errors = [r for r in caplog.records if r.levelname == "ERROR"]

    read_only_spool.chmod(0o700)

    check.equal(len(errors), 1)
    check.is_in("no read/write access", errors[0].getMessage())
    check.is_true(await importer.spool_usable())


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
        # A bad value, and the unset ones, fall back to the defaults.
        ({"SPOOL_POLL_INTERVAL": "soon"}, SpoolSettings()),
    ],
)
def test_spool_settings_from_env(
    env: dict[str, str],
    expected: SpoolSettings,
    spool_env: Callable[..., None],
) -> None:
    """
    GIVEN: spool settings in the environment
    WHEN:  settings are read
    THEN:  set values are used; unset or bad ones fall back to defaults
    """
    spool_env(**env)
    assert SpoolSettings.from_env() == expected
