"""
Re-implement some of the methods on mailbox.MH using aiofiles for async access
"""
# System imports
#
import asyncio
import email
import email.generator
import errno
import mailbox
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from mailbox import FormatError, MHMessage, NotEmptyError, _lock_file
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, TypeAlias, Union

# 3rd party imports
#
import aiofiles
import aiofiles.os
from charset_normalizer import from_bytes

# project imports
#
from .utils import utime

if TYPE_CHECKING:
    from _typeshed import StrPath

Sequences: TypeAlias = Dict[str, List[int]]

LINESEP = str(mailbox.linesep, "ascii")


####################################################################
#
def update_message_sequences(
    msg_key: int, msg: MHMessage, sequences: Sequences
):
    """
    Updates the sequences attached to the specific MHMessage.

    Does not update the .mh_sequences file.
    """
    msg_sequences: List[str] = []
    for name, key_list in sequences.items():
        if msg_key in key_list:
            msg_sequences.append(name)
    msg.set_sequences(msg_sequences)


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
        self._locked: bool
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
    async def atouch(self):
        """
        Update the mtime on folder and .mh_sequences file. This is intended
        to mark the folder as recently updated which will cause
        Mailbox.resync() to give this folder more than a cursory glance.
        """
        mtime = time.time()
        seq_path = os.path.join(self._path, ".mh_sequences")
        await utime(self._path, (mtime, mtime))
        await utime(seq_path, (mtime, mtime))

    ####################################################################
    #
    def lock(self):
        """
        Lock the mailbox. We turn off dotlock'ing because it updates the
        folder's mtime, which will causes unnecessary resyncs. We still export
        whatever is dropping mail in to the folder to use dotlocking, but that
        is fine.
        """
        if not self._locked:
            self._file = open(os.path.join(self._path, ".mh_sequences"), "rb+")
            _lock_file(self._file, dotlock=False)
            self._locked = True

    ####################################################################
    #
    @asynccontextmanager
    async def lock_folder(
        self,
        timeout: Union[int | float] = 2,
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
    async def akeys(self) -> list[int]:
        """Return a list of keys."""
        result = []
        for entry in await aiofiles.os.listdir(self._path):
            if entry.isdigit():
                result.append(int(entry))
        return sorted(result)

    ####################################################################
    #
    async def alist_folders(self):
        """Return a list of folder names."""
        result = []
        entries = await aiofiles.os.listdir(self._path)
        for entry in entries:
            if await aiofiles.os.path.isdir(os.path.join(self._path, entry)):
                result.append(entry)
        return sorted(result)

    ####################################################################
    #
    def get_message_path(self, key: int) -> Path:
        return Path(os.path.join(self._path, str(key)))

    ####################################################################
    #
    async def aget_message(self, key: int) -> MHMessage:
        """
        Use aiofiles to get a message from disk and return it as an
        MHMessage.
        """
        path = os.path.join(self._path, str(key))
        try:
            async with aiofiles.open(path, mode="rb") as f:
                # NOTE: We are using the magic of `charset_normalizer` because
                #       not all messages are nicely decodable into unicode.  We
                #       look at the encoding of the best guess and if it is one
                #       of the acceptable ones, we pass it unconverted.
                #
                contents = await f.read()
                result = from_bytes(contents).best()

                if result and result.encoding not in (
                    "ascii",
                    "latin_1",
                    "iso2022_jp",
                ):
                    contents = str(result)

        except OSError as e:
            if e.errno == errno.ENOENT:
                raise KeyError(f"No message with key: {key}")
            else:
                raise

        msg = MHMessage(contents)
        sequences = await self.aget_sequences()
        for name, key_list in sequences.items():
            if key in key_list:
                msg.add_sequence(name)
        return msg

    ####################################################################
    #
    async def aget_bytes(self, key: int) -> bytes:
        """
        Use aiofiles to get a message from disk and return it as bytes.
        """
        path = os.path.join(self._path, str(key))
        try:
            async with aiofiles.open(path, mode="rb") as f:
                contents = await f.read()
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise KeyError("No message with key: %s" % key)
            else:
                raise
        return contents

    ####################################################################
    #
    async def aclear(self):
        for key in await self.akeys():
            try:
                await self.aremove(key)
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
                raise KeyError("No message with key: %s" % key)
            else:
                raise
        else:
            await aiofiles.os.remove(path)

    ####################################################################
    #
    async def aadd(self, message: MHMessage) -> int:
        """Add message and return assigned key."""
        # NOTE: mailbox.MH uses _dump_message to write the message. However we
        #       do not need its generic power.. we can safely just rely on
        #       getting the message as bytes, then calling aiofiles.write.
        #
        #       Also not going to do the same work 'write carefully' because
        #       that will start blocking this on file io which we are trying to
        #       avoid. Writing files is pretty safe unless something so serious
        #       happens that you can not write at all.
        #
        async with self.lock_folder():
            keys = await self.akeys()
            new_key = max(keys) + 1 if keys else 1
            new_path = os.path.join(self._path, str(new_key))
            # data = message.as_bytes(policy=email.policy.default)
            data = message.as_string(policy=email.policy.default)

            async with aiofiles.open(new_path, mode="w") as f:
                await f.write(data)
                if not data.endswith(LINESEP):
                    await f.write(LINESEP)

            # A MHMessage object has MH folder sequence data attached to it.
            # So, when we write it, we have to update the `.mh_sequences` file
            # such that this message's sequences are saved.
            #
            await self._adump_sequences(message, new_key)
            return new_key

    ####################################################################
    #
    async def asetitem(self, key, message) -> None:
        """Replace the keyed message; raise KeyError if it doesn't exist."""
        async with self.lock_folder():
            path = os.path.join(self._path, str(key))
            if not await aiofiles.os.path.exists(path):
                raise KeyError(f"No message with key: {key}")

            # data = message.as_bytes(policy=email.policy.default)
            data = message.as_string(policy=email.policy.default)
            # async with aiofiles.open(path, "wb") as f:
            async with aiofiles.open(path, "w") as f:
                await f.write(data)
                if not data.endswith(LINESEP):
                    await f.write(LINESEP)
            await self._adump_sequences(message, key)

    ####################################################################
    #
    async def _adump_sequences(self, message: MHMessage, key: int):
        """Inspect a new MHMessage and update sequences appropriately."""
        pending_sequences = message.get_sequences()
        all_sequences = await self.aget_sequences()
        for name, key_list in all_sequences.items():
            if name in pending_sequences:
                key_list.append(key)
            elif key in key_list:
                key_list.remove(key)
        for sequence in pending_sequences:
            if sequence not in all_sequences:
                all_sequences[sequence] = [key]
        await self.aset_sequences(all_sequences)

    ####################################################################
    #
    async def aget_sequences(self) -> Sequences:
        """Return a name-to-key-list dictionary to define each sequence."""
        results = defaultdict(list)
        seq_path = os.path.join(self._path, ".mh_sequences")
        async with self.lock_folder():
            all_keys = set(await self.akeys())
            async with aiofiles.open(seq_path, "r", encoding="ASCII") as f:
                async for line in f:
                    try:
                        name, contents = line.split(":")
                        keys = set()
                        for spec in contents.split():
                            if spec.isdigit():
                                keys.add(int(spec))
                            else:
                                start, stop = (int(x) for x in spec.split("-"))
                                keys.update(range(start, stop + 1))
                        results[name] = [
                            key for key in sorted(keys) if key in all_keys
                        ]
                        if len(results[name]) == 0:
                            del results[name]
                    except ValueError:
                        raise FormatError(
                            f"Invalid sequence specification: {line.rstrip()}"
                        )
            return results

    ####################################################################
    #
    async def aset_sequences(self, sequences: Sequences):
        """Set sequences using the given name-to-key-list dictionary."""
        seq_file = os.path.join(self._path, ".mh_sequences")
        async with self.lock_folder():
            async with aiofiles.open(seq_file, "r+", encoding="ASCII") as f:
                await f.truncate()
                for name, keys in sequences.items():
                    if len(keys) == 0:
                        continue
                    await f.write(name + ":")
                    prev = None
                    completing = False
                    for key in sorted(set(keys)):
                        if key - 1 == prev:
                            if not completing:
                                completing = True
                                await f.write("-")
                        elif completing:
                            completing = False
                            await f.write(f"{prev} {key}")
                        else:
                            await f.write(f" {key}")
                        prev = key
                    if completing:
                        await f.write(str(prev) + "\n")
                    else:
                        await f.write("\n")
                await f.flush()

    ####################################################################
    #
    async def aremove_folder(self, folder: str):
        """Delete the named folder, which must be empty."""
        path = os.path.join(self._path, folder)
        entries = await aiofiles.os.listdir(path)
        if entries == [".mh_sequences"]:
            await aiofiles.os.remove(os.path.join(path, ".mh_sequences"))
        elif entries == []:
            pass
        else:
            raise NotEmptyError("Folder not empty: %s" % self._path)
        await aiofiles.os.rmdir(path)

    ####################################################################
    #
    async def apack(self):
        """Re-name messages to eliminate numbering gaps. Invalidates keys."""
        sequences = await self.aget_sequences()
        prev = 0
        changes = []
        async with self.lock_folder():
            for key in await self.akeys():
                if key - 1 != prev:
                    changes.append((key, prev + 1))
                    try:
                        await aiofiles.os.link(
                            os.path.join(self._path, str(key)),
                            os.path.join(self._path, str(prev + 1)),
                        )
                    except (AttributeError, PermissionError):
                        await aiofiles.os.rename(
                            os.path.join(self._path, str(key)),
                            os.path.join(self._path, str(prev + 1)),
                        )
                    else:
                        await aiofiles.os.unlink(
                            os.path.join(self._path, str(key))
                        )
                prev += 1
            self._next_key = prev + 1
            if len(changes) == 0:
                return
            for name, key_list in sequences.items():
                for old, new in changes:
                    if old in key_list:
                        key_list[key_list.index(old)] = new
            await self.aset_sequences(sequences)
