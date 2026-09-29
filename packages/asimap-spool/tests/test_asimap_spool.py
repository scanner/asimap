"""Test writing, reading, and validating asimap spool entries."""

# system imports
#
import base64
import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 3rd party imports
#
import jsonschema
import pytest
import pytest_check as check

# Project imports
#
from asimap_spool import (
    FAILED_DIR_NAME,
    INCOMING_SUFFIX,
    READY_SUFFIX,
    SPOOL_DIR_NAME,
    Delivery,
    SpoolEntry,
    SpoolFormatError,
    deliver,
    move_to_failed,
    read_entry,
    ready_entries,
    schema,
    stale_incoming,
    uuid7,
    validate_folder_name,
    write_entry,
)

# 8-bit, non-UTF-8 content that a JSON string could not carry verbatim.
#
RAW_MESSAGE = (
    b"From: a@example.com\r\nTo: b@example.com\r\nSubject: caf\xe9\r\n"
    b"Content-Transfer-Encoding: 8bit\r\n\r\nna\xefve body \xff\x00\r\n"
)


####################################################################
#
@pytest.fixture
def maildir(tmp_path: Path) -> Path:
    """An empty MH mail store root."""
    root = tmp_path / "Mail"
    root.mkdir()
    return root


####################################################################
#
@pytest.fixture
def spool(maildir: Path) -> Path:
    """The spool directory inside `maildir`, created."""
    path = maildir / SPOOL_DIR_NAME
    path.mkdir()
    return path


####################################################################
#
@pytest.fixture
def entry_dict() -> Callable[..., dict[str, Any]]:
    """
    Factory for the JSON form of a valid entry. Keyword arguments replace
    top-level fields; a value of None removes the field.
    """

    def make(**changes: Any) -> dict[str, Any]:
        data = SpoolEntry(
            id=uuid7(),
            received_at=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
            message=RAW_MESSAGE,
            deliveries=(Delivery("Lists/python", ("\\Seen", "$Work"), True),),
            source="test",
        ).to_dict()
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        return data

    return make


####################################################################
#
@pytest.fixture
def entry_schema() -> dict[str, Any]:
    """The shipped JSON Schema, checked to be a valid schema itself."""
    result = schema()
    jsonschema.Draft202012Validator.check_schema(result)
    return result


####################################################################
#
@pytest.fixture
def failing_rename(mocker: Any) -> Any:
    """Make `os.rename` in the spool module fail."""
    return mocker.patch(
        "asimap_spool.spool.os.rename", side_effect=OSError("disk gone")
    )


####################################################################
#
def test_deliver_round_trip(maildir: Path) -> None:
    """
    GIVEN: a raw message with 8-bit, non-UTF-8 bytes
    WHEN:  it is delivered with no deliveries and read back
    THEN:  the ready file is the only file, the bytes are identical, and
           it is addressed to the inbox, unseen
    """
    path = deliver(maildir, RAW_MESSAGE, source="test")
    entry = read_entry(path)

    check.equal(path.parent, maildir / SPOOL_DIR_NAME)
    check.equal(sorted(os.listdir(path.parent)), [path.name])
    check.equal(path.name, f"{entry.id}{READY_SUFFIX}")
    check.equal(entry.message, RAW_MESSAGE)
    check.equal(entry.deliveries, (Delivery("inbox", (), False),))
    check.equal(entry.source, "test")
    check.is_not_none(entry.received_at.utcoffset())


####################################################################
#
def test_written_entry_matches_schema(
    spool: Path,
    entry_dict: Callable[..., dict[str, Any]],
    entry_schema: dict[str, Any],
) -> None:
    """
    GIVEN: a spool entry with every field set
    WHEN:  it is written to the spool
    THEN:  the file validates against the shipped schema and reads back
           equal
    """
    entry = SpoolEntry.from_dict(entry_dict())
    path = write_entry(spool, entry)
    data = json.loads(path.read_bytes())

    jsonschema.validate(data, entry_schema)
    assert read_entry(path) == entry


####################################################################
#
@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"id": str(uuid.uuid4())},
        {"id": None},
        {"received_at": "2026-09-28T12:00:00"},
        {"message_encoding": "quoted-printable"},
        {"message": None},
        {"extra": 1},
        {"deliveries": [{"folder": "../etc"}]},
        {"deliveries": [{"folder": "/abs"}]},
        {"deliveries": [{"folder": "a//b"}]},
        {"deliveries": [{"folder": "a/"}]},
        {"deliveries": [{"folder": SPOOL_DIR_NAME}]},
        {"deliveries": [{"flags": ["\\Recent"]}]},
        {"deliveries": [{"flags": ["two words"]}]},
        {"deliveries": [{"create": "yes"}]},
        {"deliveries": [{"folders": "inbox"}]},
    ],
)
def test_invalid_entry_rejected(
    changes: dict[str, Any],
    entry_dict: Callable[..., dict[str, Any]],
    entry_schema: dict[str, Any],
) -> None:
    """
    GIVEN: an entry with one invalid field
    WHEN:  it is checked by the validator and by the shipped schema
    THEN:  both reject it
    """
    data = entry_dict(**changes)

    with check.raises(SpoolFormatError):
        SpoolEntry.from_dict(data)
    with check.raises(jsonschema.ValidationError):
        jsonschema.validate(data, entry_schema)


####################################################################
#
def test_bad_base64_rejected(
    entry_dict: Callable[..., dict[str, Any]],
) -> None:
    """
    GIVEN: an entry whose message is not strict base64
    WHEN:  it is parsed
    THEN:  it is rejected rather than silently dropping characters
    """
    good = base64.b64encode(RAW_MESSAGE).decode("ascii")
    with pytest.raises(SpoolFormatError):
        SpoolEntry.from_dict(entry_dict(message=f"{good[:8]}!{good[8:]}"))


####################################################################
#
def test_read_entry_rejects_mismatched_file_name(
    spool: Path, entry_dict: Callable[..., dict[str, Any]]
) -> None:
    """
    GIVEN: a ready file whose name does not match the entry's id
    WHEN:  it is read
    THEN:  it is rejected
    """
    path = spool / f"{uuid7()}{READY_SUFFIX}"
    path.write_text(json.dumps(entry_dict()))
    with pytest.raises(SpoolFormatError):
        read_entry(path)


####################################################################
#
def test_read_entry_rejects_non_json(spool: Path) -> None:
    """
    GIVEN: a ready file that is not JSON
    WHEN:  it is read
    THEN:  it is rejected with a SpoolFormatError
    """
    path = spool / f"{uuid7()}{READY_SUFFIX}"
    path.write_bytes(b"\xff not json")
    with pytest.raises(SpoolFormatError):
        read_entry(path)


####################################################################
#
@pytest.mark.parametrize(
    "name,expected",
    [
        ("INBOX", "inbox"),
        ("Inbox/Sub", "Inbox/Sub"),
        ("Lists/python", "Lists/python"),
    ],
)
def test_validate_folder_name(name: str, expected: str) -> None:
    """
    GIVEN: a valid folder name
    WHEN:  it is validated
    THEN:  only the whole name 'inbox' is case-folded
    """
    assert validate_folder_name(name) == expected


####################################################################
#
def test_write_entry_cleans_up_on_failure(
    spool: Path,
    entry_dict: Callable[..., dict[str, Any]],
    failing_rename: Any,
) -> None:
    """
    GIVEN: a rename that fails
    WHEN:  an entry is written
    THEN:  the error propagates and no incoming file is left behind
    """
    with pytest.raises(OSError):
        write_entry(spool, SpoolEntry.from_dict(entry_dict()))
    assert os.listdir(spool) == []


####################################################################
#
def test_ready_entries(spool: Path) -> None:
    """
    GIVEN: a spool with ready, incoming, failed, and unrelated files
    WHEN:  the ready entries are listed
    THEN:  only ready files are returned, oldest id first
    """
    ids = [uuid7() for _ in range(3)]
    for i in reversed(ids):
        (spool / f"{i}{READY_SUFFIX}").touch()
    (spool / f"{uuid7()}{INCOMING_SUFFIX}").touch()
    (spool / "notes.txt").touch()
    (spool / FAILED_DIR_NAME).mkdir()

    assert [p.name for p in ready_entries(spool)] == [
        f"{i}{READY_SUFFIX}" for i in sorted(ids)
    ]


####################################################################
#
def test_ready_entries_missing_spool(maildir: Path) -> None:
    """
    GIVEN: a mail store with no spool directory
    WHEN:  the ready entries are listed
    THEN:  there are none
    """
    assert ready_entries(maildir / SPOOL_DIR_NAME) == []


####################################################################
#
def test_stale_incoming(spool: Path) -> None:
    """
    GIVEN: one old and one new incoming file, and an old ready file
    WHEN:  stale incoming files are listed with a one hour limit
    THEN:  only the old incoming file is returned
    """
    now = 1_000_000.0
    old = spool / f"{uuid7()}{INCOMING_SUFFIX}"
    new = spool / f"{uuid7()}{INCOMING_SUFFIX}"
    ready = spool / f"{uuid7()}{READY_SUFFIX}"
    for path, mtime in ((old, now - 7200), (new, now - 60), (ready, 0)):
        path.touch()
        os.utime(path, (mtime, mtime))

    assert stale_incoming(spool, 3600, now=now) == [old]


####################################################################
#
def test_move_to_failed(spool: Path) -> None:
    """
    GIVEN: a spool file
    WHEN:  it is moved to failed
    THEN:  it is in the failed subdirectory and gone from the spool
    """
    path = spool / f"{uuid7()}{READY_SUFFIX}"
    path.write_text("{}")

    dest = move_to_failed(path)

    check.equal(dest, spool / FAILED_DIR_NAME / path.name)
    check.is_true(dest.exists())
    check.is_false(path.exists())


####################################################################
#
def test_uuid7_format_and_order() -> None:
    """
    GIVEN: ids generated in sequence
    WHEN:  they are inspected
    THEN:  each is a lowercase RFC 9562 version 7 uuid, and they sort in
           millisecond order
    """
    first = uuid7()
    ids = [uuid7() for _ in range(50)]
    parsed = uuid.UUID(first)

    check.equal(parsed.version, 7)
    check.equal(parsed.variant, uuid.RFC_4122)
    check.is_true(re.fullmatch(r"[0-9a-f-]{36}", first))
    check.is_true(all(first[:13] <= i[:13] for i in ids))
