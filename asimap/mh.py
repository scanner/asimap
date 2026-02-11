"""
Re-implement some of the methods on mailbox.MH using aiofiles for async access
"""

# System imports
#
import asyncio
import errno
import logging
import mailbox
import os
import stat
from contextlib import asynccontextmanager
from mailbox import NoSuchMailboxError, _lock_file
from pathlib import Path
from typing import TYPE_CHECKING

# 3rd party imports
#
import aiofiles
import aiofiles.os

# from charset_normalizer import from_bytes

if TYPE_CHECKING:
    from _typeshed import StrPath

LINESEP = str(mailbox.linesep, "ascii")

logger = logging.getLogger("asimap.mh")

# By default, MH advisory file locking is disabled. This avoids FD
# exhaustion on systems with large numbers of mailboxes (1,200+). Set to
# True via set_file_locking() if external MH command-line clients are
# actively modifying the same mail store.
#
FILE_LOCKING_ENABLED: bool = False


####################################################################
#
def set_file_locking(enabled: bool) -> None:
    """Enable or disable MH advisory file locking."""
    global FILE_LOCKING_ENABLED
    FILE_LOCKING_ENABLED = enabled


########################################################################
########################################################################
#
class MH(mailbox.MH):
    """
    Replace some of the mailbox.MH methods with ones that use aiofiles
    """

    ####################################################################
    #
    def __init__(self, path: "StrPath", factory=None, create=True):
        self._locked: bool = False
        path = str(path)
        super().__init__(path, factory=factory, create=create)

    ####################################################################
    #
    def get_folder(self, folder: "StrPath"):
        """Return an MH instance for the named folder."""
        return MH(
            os.path.join(self._path, str(folder)),
            factory=self._factory,
            create=False,
        )

    ####################################################################
    #
    def add_folder(self, folder: "StrPath"):
        """Create a folder and return an MH instance representing it."""
        return MH(os.path.join(self._path, str(folder)), factory=self._factory)

    ####################################################################
    #
    def lock(self, dotlock: bool = False):
        """
        Lock the mailbox. We turn off dotlock'ing because it updates the
        folder's mtime, which will causes unnecessary resyncs. We still expect
        whatever is dropping mail in to the folder to use dotlocking, but that
        is fine.
        """
        if not FILE_LOCKING_ENABLED:
            return
        if not self._locked:
            mh_seq_fname = os.path.join(self._path, ".mh_sequences")
            if not os.path.exists(mh_seq_fname):
                f = open(mh_seq_fname, "a")
                f.close()
                os.chmod(mh_seq_fname, stat.S_IRUSR | stat.S_IWUSR)
            self._file = open(mh_seq_fname, "rb+")
            _lock_file(self._file, dotlock=dotlock)
            self._locked = True

    ####################################################################
    #
    def unlock(self):
        """
        Unlock the mailbox. When file locking is disabled, lock() is a
        no-op so there is nothing to unlock.
        """
        if not FILE_LOCKING_ENABLED:
            return
        super().unlock()

    ####################################################################
    #
    @asynccontextmanager
    async def lock_folder(
        self,
        timeout: int | float = 2,
        fail: bool = False,
    ):
        """
        Implement an asyncio contextmanager for locking a folder.  This
        only protects against other _processes_ that obey the advisory locking.

        Use this when you need to modify the MH folder, or guarantee that the
        message you are adding to the folder does not conflict with one being
        added by another system, or want to make sure that the sequences file
        does not change.

        NOTE: Since this also uses dot-locking this will cause the mtime on the
              folder to change.
        """
        # NOTE: The locking at the process level, so if this process has
        #       already locked the folder there is nothing for us to do. The
        #       code that has the folder already locked will properly release
        #       it when done with it.
        #
        if not os.path.exists(self._path):
            raise NoSuchMailboxError(self._path)

        if not FILE_LOCKING_ENABLED:
            yield
            return

        if self._locked:
            yield
        else:
            while timeout > 0:
                try:
                    self.lock()
                    break
                except mailbox.ExternalClashError:
                    if fail:
                        raise
                    timeout -= 0.1
                    await asyncio.sleep(0.1)
            try:
                yield
            finally:
                self.unlock()

    ####################################################################
    #
    def get_message_path(self, key: int) -> Path:
        return Path(os.path.join(self._path, str(key)))

    ####################################################################
    #
    async def aclear(self):
        for key in [int(x) for x in self.keys()]:
            try:
                self.remove(str(key))
                await asyncio.sleep(0)
            except KeyError:
                pass

    ####################################################################
    #
    async def aremove(self, key: int):
        """Remove the keyed message; raise KeyError if it doesn't exist."""
        path = os.path.join(self._path, str(key))
        try:
            # Why do calls for "exists", "isfile", and "access" when we can
            # just try to open the file for reading.
            #
            async with aiofiles.open(path, "rb+"):
                pass
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise KeyError(f"No message with key: {key}") from e
            else:
                raise
        else:
            await aiofiles.os.remove(path)
