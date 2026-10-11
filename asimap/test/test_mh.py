"""
Tests for our subclass of `mailbox.MH` that adds some async methods
"""

# system imports
#
import mailbox
import os
import shutil
import stat
from collections import defaultdict
from collections.abc import Callable, Generator
from email import message_from_bytes
from email.policy import SMTP
from mailbox import NoSuchMailboxError
from pathlib import Path

# 3rd party imports
#
import pytest

# Project imports
#
from .. import mh as mh_module
from ..constants import Sequences
from ..mh import MH, assign_unseen_to_new_keys, sequences_as_text


####################################################################
#
@pytest.fixture
def enable_file_locking() -> Generator[None, None, None]:
    """Enable MH file locking for the duration of the test."""
    mh_module.set_file_locking(True)
    yield
    mh_module.set_file_locking(False)


####################################################################
#
@pytest.mark.asyncio
async def test_mh_lock_folder(
    tmp_path: Path, enable_file_locking: None
) -> None:
    """
    GIVEN: FILE_LOCKING_ENABLED is True
    WHEN:  lock_folder() is used as a context manager
    THEN:  it acquires and releases the file lock normally

    XXX To do a proper test we need to fork a separate process and validate

        that it blocks while we hold this lock. Tested this by hand and it
        worked so going to leave the full test for later.

    For now we are testing that this does not outright fail.
    """
    mh_dir = tmp_path / "Mail"
    mh = MH(mh_dir)
    inbox = mh.add_folder("inbox")
    assert inbox._locked is False
    async with inbox.lock_folder():
        assert inbox._locked is True
    assert inbox._locked is False

    # Locks are not lost with an exception.
    #
    with pytest.raises(RuntimeError):
        async with inbox.lock_folder():
            assert inbox._locked is True
            raise RuntimeError("Woop")
    assert inbox._locked is False


####################################################################
#
@pytest.mark.asyncio
async def test_mh_lock_folder_noop(tmp_path: Path) -> None:
    """
    GIVEN: FILE_LOCKING_ENABLED is False (default)
    WHEN:  lock_folder() is used as a context manager
    THEN:  it yields without opening any files or setting _locked
    """
    assert mh_module.FILE_LOCKING_ENABLED is False

    mh_dir = tmp_path / "Mail"
    mh = MH(mh_dir)
    inbox = mh.add_folder("inbox")

    assert inbox._locked is False
    async with inbox.lock_folder():
        # When file locking is disabled, _locked remains False
        assert inbox._locked is False
    assert inbox._locked is False


####################################################################
#
@pytest.mark.asyncio
async def test_mh_lock_folder_noop_deleted_folder(tmp_path: Path) -> None:
    """
    GIVEN: FILE_LOCKING_ENABLED is False (default)
    WHEN:  lock_folder() is called on a folder that no longer exists
    THEN:  NoSuchMailboxError is still raised
    """
    assert mh_module.FILE_LOCKING_ENABLED is False

    mh_dir = tmp_path / "Mail"
    mh = MH(mh_dir)
    inbox = mh.add_folder("inbox")

    # Remove the folder from disk
    shutil.rmtree(mh_dir / "inbox")

    with pytest.raises(NoSuchMailboxError):
        async with inbox.lock_folder():
            pass


####################################################################
#
def test_set_file_locking() -> None:
    """
    GIVEN: default state (FILE_LOCKING_ENABLED is False)
    WHEN:  set_file_locking() is called
    THEN:  FILE_LOCKING_ENABLED is updated accordingly
    """
    assert mh_module.FILE_LOCKING_ENABLED is False
    mh_module.set_file_locking(True)
    assert mh_module.FILE_LOCKING_ENABLED is True
    mh_module.set_file_locking(False)
    assert mh_module.FILE_LOCKING_ENABLED is False


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aclear(bunch_of_email_in_folder: Callable[..., Path]) -> None:
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    inbox_folder = mh.get_folder("inbox")
    inbox_dir = mh_dir / "inbox"
    dir_keys = sorted(
        [int(x.name) for x in inbox_dir.iterdir() if x.name.isdigit()]
    )
    assert dir_keys

    await inbox_folder.aclear()

    dir_keys = sorted(
        [int(x.name) for x in inbox_dir.iterdir() if x.name.isdigit()]
    )
    assert len(dir_keys) == 0


####################################################################
#
@pytest.mark.asyncio
async def test_mh_aremove(
    bunch_of_email_in_folder: Callable[..., Path],
) -> None:
    mh_dir = bunch_of_email_in_folder()
    mh = MH(mh_dir)
    inbox_folder = mh.get_folder("inbox")
    inbox_dir = mh_dir / "inbox"

    dir_keys = sorted(
        [int(x.name) for x in inbox_dir.iterdir() if x.name.isdigit()]
    )
    assert dir_keys

    async with inbox_folder.lock_folder():
        folder_keys = inbox_folder.keys()
        for key in folder_keys:
            await inbox_folder.aremove(int(key))

    dir_keys = sorted(
        [int(x.name) for x in inbox_dir.iterdir() if x.name.isdigit()]
    )
    assert len(dir_keys) == 0


####################################################################
#
def test_mh_add_multipart_missing_start_boundary(
    tmp_path: Path, problematic_email_factory_bytes: Callable[[int], bytes]
) -> None:
    """
    GIVEN: a parsed message, as APPEND produces, whose inner multipart never
           has its start boundary and holds 8bit UTF-8 text
    WHEN:  the message is added to an MH folder
    THEN:  the file on disk holds that text as its original bytes
    """
    msg = message_from_bytes(problematic_email_factory_bytes(6), policy=SMTP)
    mh = MH(tmp_path / "inbox")

    msg_key = mh.add(msg)

    expected = "Café crème brûlée — naïve résumé".encode()
    assert expected in (tmp_path / "inbox" / str(msg_key)).read_bytes()


########################################################################
########################################################################
#
class TestAssignUnseenToNewKeys:
    """Tests for `assign_unseen_to_new_keys`."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "msg_keys,sequences,expected_unseen,expected_seen",
        [
            # Mail delivery writes the message and nothing else, so the
            # whole folder arrives in no sequence at all.
            ([1, 2, 3], {}, {1, 2, 3}, set()),
            # Messages we have already processed keep their `Seen`.
            ([1, 2, 3], {"Seen": {1, 2}}, {3}, {1, 2}),
            # An existing `unseen` survives and is extended.
            ([1, 2, 3], {"unseen": {1}, "Seen": {2}}, {1, 3}, {2}),
            # A key named by both sequences is unseen.
            ([1, 2], {"unseen": {1}, "Seen": {1, 2}}, {1}, {2}),
            # Keys that have left the folder are dropped from both.
            ([1], {"unseen": {1, 98}, "Seen": {99}}, {1}, set()),
            # An empty folder produces empty sequences.
            ([], {"Seen": {1}}, set(), set()),
        ],
    )
    def test_seen_and_unseen(
        self,
        msg_keys: list[int],
        sequences: dict[str, set[int]],
        expected_unseen: set[int],
        expected_seen: set[int],
    ) -> None:
        """
        GIVEN: a folder's message keys and its sequences
        WHEN:  new keys are assigned to `unseen`
        THEN:  `unseen` holds every unprocessed key and `Seen` the rest
        """
        seqs: Sequences = defaultdict(set, sequences)

        result = assign_unseen_to_new_keys(msg_keys, seqs)

        assert result["unseen"] == expected_unseen
        assert result["Seen"] == expected_seen

    ####################################################################
    #
    def test_other_sequences_are_untouched(self) -> None:
        """
        GIVEN: sequences carrying flags other than `Seen`/`unseen`
        WHEN:  new keys are assigned to `unseen`
        THEN:  those other sequences come back unchanged
        """
        seqs: Sequences = defaultdict(
            set, {"replied": {1}, "flagged": {2}, "Seen": {1, 2}}
        )

        result = assign_unseen_to_new_keys([1, 2, 3], seqs)

        assert result["replied"] == {1}
        assert result["flagged"] == {2}
        assert result["unseen"] == {3}

    ####################################################################
    #
    def test_argument_is_not_modified(self) -> None:
        """
        GIVEN: a sequences dict
        WHEN:  new keys are assigned to `unseen`
        THEN:  the dict passed in is left as it was
        """
        seqs: Sequences = defaultdict(set, {"Seen": {1}})

        assign_unseen_to_new_keys([1, 2], seqs)

        assert seqs == {"Seen": {1}}


########################################################################
########################################################################
#
class TestSetSequences:
    """Tests for writing `.mh_sequences`."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "sequences,expected",
        [
            ({"unseen": [1]}, "unseen: 1\n"),
            # Two in a row is already a range.
            ({"unseen": [1, 2]}, "unseen: 1-2\n"),
            ({"unseen": [1, 2, 3, 5]}, "unseen: 1-3 5\n"),
            ({"unseen": [1, 3, 5]}, "unseen: 1 3 5\n"),
            # Keys are sorted and de-duplicated.
            ({"unseen": [3, 1, 2, 1]}, "unseen: 1-3\n"),
            # A sequence with no keys is left out of the file.
            ({"unseen": []}, ""),
            ({"Seen": [1], "unseen": []}, "Seen: 1\n"),
            ({}, ""),
        ],
    )
    def test_sequences_as_text(
        self, sequences: dict[str, list[int]], expected: str
    ) -> None:
        """
        GIVEN: a mapping of sequence name to message keys
        WHEN:  it is rendered for `.mh_sequences`
        THEN:  consecutive runs collapse and empty sequences are dropped
        """
        assert sequences_as_text(sequences) == expected

    ####################################################################
    #
    def test_round_trips_through_stock_mailbox_mh(self, tmp_path: Path) -> None:
        """
        GIVEN: sequences written by our MH subclass
        WHEN:  stock `mailbox.MH` reads them back
        THEN:  it returns what we wrote
        """
        folder = MH(tmp_path / "inbox")
        for _ in range(10):
            folder.add(b"From: a@b.c\n\nbody\n")

        sequences = {
            "unseen": [1, 2, 3, 5, 9, 10],
            "Seen": [4],
            "replied": [7],
        }
        folder.set_sequences(sequences)

        stock = mailbox.MH(str(tmp_path / "inbox"), create=False)
        assert stock.get_sequences() == sequences

    ####################################################################
    #
    def test_write_is_atomic(self, tmp_path: Path) -> None:
        """
        GIVEN: a folder with sequences already written
        WHEN:  they are written again
        THEN:  the file is replaced rather than truncated in place

        A reader holding the old file keeps seeing the whole old file, which
        is what stops a torn read (ASIMAP-68).
        """
        folder = MH(tmp_path / "inbox")
        for _ in range(3):
            folder.add(b"From: a@b.c\n\nbody\n")
        folder.set_sequences({"unseen": [1, 2, 3]})

        seq_path = tmp_path / "inbox" / ".mh_sequences"
        before_inode = seq_path.stat().st_ino
        with open(seq_path, encoding="ASCII") as reader:
            folder.set_sequences({"Seen": [1]})
            # The open handle still sees the file as it was.
            #
            assert reader.read() == "unseen: 1-3\n"

        assert seq_path.stat().st_ino != before_inode
        assert seq_path.read_text(encoding="ASCII") == "Seen: 1\n"

    ####################################################################
    #
    def test_leaves_no_stray_files_in_the_folder(self, tmp_path: Path) -> None:
        """
        GIVEN: a folder with messages in it
        WHEN:  sequences are written
        THEN:  no temp file is left behind and no message key is invented
        """
        folder = MH(tmp_path / "inbox")
        for _ in range(3):
            folder.add(b"From: a@b.c\n\nbody\n")

        folder.set_sequences({"unseen": [1, 2, 3]})

        assert sorted(folder.keys()) == [1, 2, 3]
        assert sorted(os.listdir(tmp_path / "inbox")) == [
            ".mh_sequences",
            "1",
            "2",
            "3",
        ]

    ####################################################################
    #
    def test_preserves_the_file_mode(self, tmp_path: Path) -> None:
        """
        GIVEN: a `.mh_sequences` with a non-default mode
        WHEN:  sequences are written over it
        THEN:  the mode is kept

        A rename brings the temp file's mode with it unless we carry the
        old one across.
        """
        folder = MH(tmp_path / "inbox")
        folder.add(b"From: a@b.c\n\nbody\n")
        folder.set_sequences({"unseen": [1]})

        seq_path = tmp_path / "inbox" / ".mh_sequences"
        os.chmod(seq_path, 0o640)

        folder.set_sequences({"Seen": [1]})

        assert stat.S_IMODE(seq_path.stat().st_mode) == 0o640
