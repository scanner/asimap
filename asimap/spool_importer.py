"""
Import messages that other services drop in the user's delivery spool.

Producers use the `asimap_spool` package to write `<uuid7>-ready.json` files
into `.asimap-spool/` at the root of the user's MH mail store. The importer
appends each message to its target folders through the same mailbox command
queue an IMAP APPEND uses, records the entry id in the database, and removes
the file.

The spool is checked every `SPOOL_POLL_INTERVAL` seconds. When `SPOOL_WATCH`
is on, a `watchfiles` watcher on the spool directory wakes the importer as
soon as a ready file appears; polling continues regardless.
"""

# system imports
#
import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

# 3rd party imports
#
from asimap_spool import (
    DEFAULT_FOLDER,
    FAILED_DIR_NAME,
    READY_SUFFIX,
    Delivery,
    SpoolEntry,
    SpoolFormatError,
    move_to_failed,
    read_entry,
    ready_entries,
    spool_dir,
    stale_incoming,
)
from watchfiles import Change, awatch

# Project imports
#
from .mbox import InvalidMailbox, Mailbox, MailboxExists, NoSuchMailbox
from .parse import IMAPClientCommand, IMAPCommand
from .utils import env_bool, env_float

if TYPE_CHECKING:
    from .user_server import IMAPUserServer

logger = logging.getLogger("asimap.spool_importer")

# Number of times an entry may fail to import before it is moved to
# `failed/`.
#
MAX_IMPORT_ATTEMPTS = 3

# How long imported ids are kept once their spool file is gone. An id
# whose file is still in the spool is kept until the file is removed.
#
IMPORTED_ID_RETENTION = 86400.0


########################################################################
########################################################################
#
@dataclass(frozen=True)
class SpoolSettings:
    """
    Spool importer settings, read from the environment.

    Args:
        poll_interval: Seconds between spool checks when nothing wakes the
            importer (`SPOOL_POLL_INTERVAL`).
        stale_incoming_age: Seconds after which an incoming file is taken
            as abandoned and moved to `failed/`
            (`SPOOL_STALE_INCOMING_AGE`).
        watch: Watch the spool directory for new files (`SPOOL_WATCH`).
    """

    poll_interval: float = 5.0
    stale_incoming_age: float = 3600.0
    watch: bool = True

    ####################################################################
    #
    @classmethod
    def from_env(cls) -> "SpoolSettings":
        """Build settings from the environment, using defaults if unset."""
        return cls(
            poll_interval=env_float("SPOOL_POLL_INTERVAL", cls.poll_interval),
            stale_incoming_age=env_float(
                "SPOOL_STALE_INCOMING_AGE", cls.stale_incoming_age
            ),
            watch=env_bool("SPOOL_WATCH", cls.watch),
        )


####################################################################
#
def _ready_file_added(change: Change, path: str) -> bool:
    """watchfiles filter: only a ready file appearing wakes the importer."""
    return change != Change.deleted and path.endswith(READY_SUFFIX)


########################################################################
########################################################################
#
class SpoolImporter:
    """Moves spooled messages into the user's mailboxes."""

    ####################################################################
    #
    def __init__(
        self, server: "IMAPUserServer", settings: SpoolSettings
    ) -> None:
        self.server = server
        self.settings = settings
        self.spool = spool_dir(server.maildir)
        self.wake = asyncio.Event()
        self.stop_watching = asyncio.Event()
        self.attempts: dict[str, int] = {}
        self.delivered: dict[str, set[str]] = {}
        self.unremovable: set[Path] = set()

    ####################################################################
    #
    async def run(self) -> None:
        """
        Drain the spool, then wait for a watcher wake-up or the poll
        interval, and repeat until cancelled.
        """
        await asyncio.to_thread(self._ensure_dirs)
        watcher = (
            asyncio.create_task(self.watch(), name="spool watcher")
            if self.settings.watch
            else None
        )
        try:
            while True:
                self.wake.clear()
                try:
                    await self.drain()
                except Exception as e:
                    logger.exception("spool: drain failed: %r", e)
                try:
                    async with asyncio.timeout(self.settings.poll_interval):
                        await self.wake.wait()
                except TimeoutError:
                    pass
        finally:
            if watcher:
                self.stop_watching.set()
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass

    ####################################################################
    #
    def _ensure_dirs(self) -> None:
        """Create the spool and its `failed/` directory if missing."""
        (self.spool / FAILED_DIR_NAME).mkdir(parents=True, exist_ok=True)

    ####################################################################
    #
    async def watch(self) -> None:
        """
        Set `wake` whenever a ready file appears in the spool. If the
        watcher can not be set up or fails, log it and return; polling
        continues without it.
        """
        try:
            async for _ in awatch(
                self.spool,
                watch_filter=_ready_file_added,
                debounce=100,
                recursive=False,
                stop_event=self.stop_watching,
            ):
                self.wake.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "spool: watching '%s' failed, polling only: %r", self.spool, e
            )

    ####################################################################
    #
    async def drain(self) -> int:
        """
        Import every ready entry in id order. Move abandoned incoming files
        and entries that can not be imported to `failed/`.

        Returns:
            The number of entries imported.
        """
        stale = await asyncio.to_thread(
            stale_incoming, self.spool, self.settings.stale_incoming_age
        )
        for path in stale:
            await self._fail(path, "incoming file abandoned by its producer")

        imported = 0
        for path in await asyncio.to_thread(ready_entries, self.spool):
            try:
                entry = await asyncio.to_thread(read_entry, path)
            except FileNotFoundError:
                continue
            except (SpoolFormatError, OSError) as e:
                await self._fail(path, str(e))
                continue

            if await self._already_imported(entry.id):
                await self._remove(path)
                continue

            try:
                await self.import_entry(entry)
            except Exception as e:
                attempts = self.attempts.get(entry.id, 0) + 1
                self.attempts[entry.id] = attempts
                if attempts < MAX_IMPORT_ATTEMPTS:
                    logger.warning(
                        "spool: import of %s failed (attempt %d): %r",
                        path.name,
                        attempts,
                        e,
                    )
                else:
                    self._forget(entry.id)
                    await self._fail(path, f"import failed: {e!r}")
                continue

            self._forget(entry.id)
            await self._record_imported(entry.id)
            await self._remove(path)
            imported += 1

        if imported:
            await self._prune_imported()
        return imported

    ####################################################################
    #
    async def _prune_imported(self) -> None:
        """
        Forget imported ids older than `IMPORTED_ID_RETENTION` whose spool
        file is gone.
        """
        cutoff = time.time() - IMPORTED_ID_RETENTION
        old = [
            row[0]
            async for row in self.server.db.query(
                "SELECT id FROM spool_imported WHERE imported_at < ?",
                (cutoff,),
            )
        ]
        gone = [
            entry_id
            for entry_id in old
            if not (self.spool / f"{entry_id}{READY_SUFFIX}").exists()
        ]
        for entry_id in gone:
            await self.server.db.execute(
                "DELETE FROM spool_imported WHERE id = ?", (entry_id,)
            )
        if gone:
            await self.server.db.commit()

    ####################################################################
    #
    async def import_entry(self, entry: SpoolEntry) -> None:
        """
        Append the entry's message to each of its target folders.

        Deliveries that resolve to the same folder are merged, their flags
        combined, so the message is stored there once. Folders that got the
        message on an earlier, failed attempt are skipped.
        """
        delivered = self.delivered.setdefault(entry.id, set())
        targets: dict[str, set[str]] = {}
        for delivery in entry.deliveries:
            folder = await self.resolve_folder(delivery)
            targets.setdefault(folder, set()).update(delivery.flags)

        for folder, flags in targets.items():
            if folder in delivered:
                continue
            mbox = await self.server.get_mailbox(folder)
            cmd = IMAPClientCommand(f"spool APPEND {folder}")
            cmd.tag = "spool"
            cmd.command = IMAPCommand.APPEND
            async with cmd.ready_and_okay(mbox):
                await mbox.append(
                    entry.message, sorted(flags), entry.received_at
                )
            delivered.add(folder)
        logger.info(
            "spool: imported %s from %s into %s",
            entry.id,
            entry.source or "unknown source",
            ", ".join(targets),
        )

    ####################################################################
    #
    async def resolve_folder(self, delivery: Delivery) -> str:
        """
        Return the folder a delivery goes to, creating it if the delivery
        asks for that. A missing folder that may not be created, or a
        deleted (`\\Noselect`) one, means the inbox. The inbox is created
        if it is missing.
        """
        name = delivery.folder
        if name == DEFAULT_FOLDER:
            if not self.server.folder_exists(name):
                await asyncio.to_thread(self.server.mailbox.add_folder, name)
            return name

        if await self._selectable(name):
            return name
        if delivery.create:
            try:
                await Mailbox.create(name, self.server)
            except MailboxExists:
                pass
            except InvalidMailbox as e:
                logger.warning("spool: can not create '%s': %s", name, e)
            if await self._selectable(name):
                return name
        logger.warning(
            "spool: folder '%s' does not exist, delivering to the inbox", name
        )
        return await self.resolve_folder(Delivery(DEFAULT_FOLDER))

    ####################################################################
    #
    async def _selectable(self, name: str) -> bool:
        """True if the folder exists and is not a deleted placeholder."""
        if not self.server.folder_exists(name):
            return False
        try:
            mbox = await self.server.get_mailbox(name)
        except NoSuchMailbox:
            return False
        return r"\Noselect" not in mbox.attributes

    ####################################################################
    #
    async def _already_imported(self, entry_id: str) -> bool:
        row = await self.server.db.fetchone(
            "SELECT 1 FROM spool_imported WHERE id = ?", (entry_id,)
        )
        return row is not None

    ####################################################################
    #
    async def _record_imported(self, entry_id: str) -> None:
        await self.server.db.execute(
            "INSERT OR IGNORE INTO spool_imported (id, imported_at) "
            "VALUES (?, ?)",
            (entry_id, time.time()),
            commit=True,
        )

    ####################################################################
    #
    def _forget(self, entry_id: str) -> None:
        """Drop the retry state kept for an entry."""
        self.attempts.pop(entry_id, None)
        self.delivered.pop(entry_id, None)

    ####################################################################
    #
    async def _remove(self, path: Path) -> None:
        """
        Remove an imported entry. If that fails the entry stays, and later
        passes skip it because its id is recorded.
        """
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as e:
            if path not in self.unremovable:
                self.unremovable.add(path)
                logger.error("spool: could not remove '%s': %r", path, e)
        else:
            self.unremovable.discard(path)

    ####################################################################
    #
    async def _fail(self, path: Path, reason: str) -> None:
        """Move a spool file to `failed/` and report why."""
        try:
            dest = await asyncio.to_thread(move_to_failed, path)
        except FileNotFoundError:
            return
        except OSError as e:
            logger.error(
                "spool: could not move '%s' to %s/: %r",
                path,
                FAILED_DIR_NAME,
                e,
            )
            return
        logger.error(
            "spool: moved '%s' to '%s': %s",
            path.name,
            os.path.relpath(dest, self.server.maildir),
            reason,
        )
