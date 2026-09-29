"""Test writing, reading, and validating asimap spool entries."""

# system imports
#
import base64
import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from typing import Any

# 3rd party imports
#
import factory
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
    SpoolUnavailableError,
    deliver,
    read_entry,
    ready_entries,
    schema,
    stale_incoming,
    uuid7,
    validate_folder_name,
    write_entry,
)
from pytest_mock import MockerFixture

# 8-bit, non-UTF-8 content that a JSON string could not carry verbatim.
#
RAW_MESSAGE = (
    b"From: a@example.com\r\nTo: b@example.com\r\nSubject: caf\xe9\r\n"
    b"Content-Transfer-Encoding: 8bit\r\n\r\nna\xefve body \xff\x00\r\n"
)


########################################################################
########################################################################
#
class DeliveryFactory(factory.Factory[Delivery]):
    class Meta:
        model = Delivery
        # `create` would shadow factory-boy's own `create()` method.
        rename = {"create_folder": "create"}

    folder = factory.Faker("word")
    flags = ("\\Seen", "$Work")
    create_folder = True


########################################################################
########################################################################
#
class SpoolEntryFactory(factory.Factory[SpoolEntry]):
    class Meta:
        model = SpoolEntry

    id = factory.LazyFunction(uuid7)
    received_at = factory.Faker("date_time", tzinfo=UTC)
    message = RAW_MESSAGE
    deliveries = factory.LazyFunction(lambda: (DeliveryFactory.build(),))
    source = factory.Faker("word")


####################################################################
#
@pytest.fixture
def maildir(tmp_path: Path) -> Path:
    """An empty MH mail store root, with no spool directory yet."""
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
        data = SpoolEntryFactory.build().to_dict()
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
def failing_rename(mocker: MockerFixture) -> Any:
    """Make the rename from incoming to ready fail."""
    return mocker.patch(
        "asimap_spool.spool.os.rename", side_effect=OSError("disk gone")
    )


####################################################################
#
@pytest.mark.parametrize(
    "deliveries,expected",
    [
        ((), (Delivery("inbox", (), False),)),
        (
            (Delivery("Lists/python", ("\\Seen", "$Work"), True),),
            (Delivery("Lists/python", ("\\Seen", "$Work"), True),),
        ),
    ],
)
def test_deliver_round_trip(
    deliveries: tuple[Delivery, ...],
    expected: tuple[Delivery, ...],
    maildir: Path,
    entry_schema: dict[str, Any],
) -> None:
    """
    GIVEN: a raw message with 8-bit, non-UTF-8 bytes and no spool directory
    WHEN:  it is delivered and read back
    THEN:  the spool is created holding only the ready file, which matches
           the schema and returns the identical bytes and deliveries (the
           inbox when none are given)
    """
    path = deliver(maildir, RAW_MESSAGE, deliveries, source="test")
    entry = read_entry(path)

    check.equal(os.listdir(maildir / SPOOL_DIR_NAME), [path.name])
    check.is_none(
        jsonschema.validate(json.loads(path.read_bytes()), entry_schema)
    )
    check.equal(entry.message, RAW_MESSAGE)
    check.equal(entry.deliveries, expected)
    check.equal(entry.source, "test")
    check.is_not_none(entry.received_at.utcoffset())


####################################################################
#
@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"id": str(uuid.uuid4())},
        {"received_at": "2026-09-28T12:00:00"},
        {"message_encoding": "quoted-printable"},
        {"message": None},
        {"extra": 1},
        {"deliveries": [{"folder": "../etc"}]},
        {"deliveries": [{"folder": "/abs"}]},
        {"deliveries": [{"folder": "a//b"}]},
        {"deliveries": [{"folder": "a/"}]},
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
    GIVEN: an entry whose message is not strict base64 (which the schema
           can not express)
    WHEN:  it is parsed
    THEN:  it is rejected rather than silently dropping characters
    """
    good = base64.b64encode(RAW_MESSAGE).decode("ascii")
    with pytest.raises(SpoolFormatError):
        SpoolEntry.from_dict(entry_dict(message=f"{good[:8]}!{good[8:]}"))


####################################################################
#
@pytest.mark.parametrize(
    "content",
    [
        # A valid entry whose id is not the file name.
        json.dumps(SpoolEntryFactory.build().to_dict()).encode(),
        b"\xff not json",
    ],
)
def test_read_entry_rejects(content: bytes, spool: Path) -> None:
    """
    GIVEN: a ready file that is not JSON, or whose id does not match its
           file name
    WHEN:  it is read
    THEN:  it is rejected with a SpoolFormatError
    """
    path = spool / f"{uuid7()}{READY_SUFFIX}"
    path.write_bytes(content)
    with pytest.raises(SpoolFormatError):
        read_entry(path)


####################################################################
#
@pytest.mark.parametrize(
    "name,expected", [("INBOX", "inbox"), ("Inbox/Sub", "Inbox/Sub")]
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
    spool: Path, failing_rename: Any
) -> None:
    """
    GIVEN: a rename that fails after the incoming file is written
    WHEN:  an entry is written
    THEN:  it raises SpoolUnavailableError and no incoming file is left
    """
    with pytest.raises(SpoolUnavailableError):
        write_entry(spool, SpoolEntryFactory.build())
    assert os.listdir(spool) == []


####################################################################
#
def test_deliver_to_missing_mail_store(tmp_path: Path) -> None:
    """
    GIVEN: a mail store root that does not exist, so the spool can not be
           created
    WHEN:  a message is delivered
    THEN:  it raises SpoolUnavailableError
    """
    with pytest.raises(SpoolUnavailableError):
        deliver(tmp_path / "nobody", RAW_MESSAGE)


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
def test_uuid7_format_and_order() -> None:
    """
    GIVEN: ids generated in sequence
    WHEN:  they are inspected
    THEN:  each is an RFC 9562 version 7 uuid, and their timestamps never
           go backwards
    """
    ids = [uuid7() for _ in range(50)]
    parsed = [uuid.UUID(i) for i in ids]

    check.is_true(all(p.version == 7 for p in parsed))
    check.is_true(all(p.variant == uuid.RFC_4122 for p in parsed))
    check.equal([i[:13] for i in ids], sorted(i[:13] for i in ids))
