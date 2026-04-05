#!/usr/bin/env python
#
"""
Test the top level asimapd server through a series of integration tests.
"""

# system imports
#
import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# 3rd party imports
#
import pytest

# Project imports
#
from ..client import CAPABILITIES
from ..constants import MAX_INPUT_SIZE
from ..server import IMAPClient
from .conftest import EmailFactoryType


####################################################################
#
@pytest.mark.integration
def test_server_capability(imap_server: dict[str, Any]) -> None:
    """
    We want a high level test of the server, but do not want to get into it
    launching the subprocess for an authenticated user. Getting the
    'CAPABILITY' response from the server is good enough for that.
    """
    fixtures = imap_server
    imap = fixtures["client"]
    status, capabilities = imap.capability()
    assert status == "OK"
    assert str(capabilities[0], "ascii") == " ".join(CAPABILITIES)
    imap.logout()


####################################################################
#
@pytest.mark.integration
def test_server_login(
    imap_server: dict[str, Any], imap_user_server_program: None
) -> None:
    """
    Try logging in to the server. This will also launch the subprocess and
    communicate with it.
    """
    fixtures = imap_server
    imap = fixtures["client"]
    status, capabilities = imap.capability()
    assert status == "OK"
    status, resp = imap.login(fixtures["user"].username, fixtures["password"])
    assert status == "OK"
    status, resp = imap.logout()
    assert status == "BYE"


####################################################################
#
@pytest.mark.integration
def test_server_list_status_select(
    bunch_of_email_in_folder: Callable[..., Path],
    imap_server: dict[str, Any],
    imap_user_server_program: None,
) -> None:
    """
    LIST, STATUS INBOX, SELECT INBOX
    """
    fixtures = imap_server
    imap = fixtures["client"]
    status, capabilities = imap.capability()
    assert status == "OK"
    status, resp = imap.login(fixtures["user"].username, fixtures["password"])
    assert status == "OK"
    status, resp = imap.list()
    status, resp = imap.status(
        "INBOX", "(messages recent uidnext uidvalidity unseen)"
    )
    status, resp = imap.select(mailbox="INBOX")
    status, resp = imap.fetch(
        "1:5", "(UID BODY[HEADER.FIELDS (TO FROM SUBJECT DATE)])"
    )
    status, resp = imap.uid(
        "FETCH",
        "1:5",
        "(INTERNALDATE UID RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS (date subject from to cc message-id in-reply-to references content-type x-priority x-uniform-type-identifier x-universally-unique-identifier list-id list-unsubscribe bimi-indicator bimi-location x-bimi-indicator-hash authentication-results dkim-signature x-spam-status x-spam-flag received-spf X-Forefront-Antispam-Report)])",
    )
    status, resp = imap.logout()
    assert status == "BYE"


####################################################################
#
def test_server_append_and_fetch(
    bunch_of_email_in_folder: Callable[..., Path],
    imap_server: dict[str, Any],
    imap_user_server_program: None,
    email_factory: EmailFactoryType,
) -> None:
    """
    Make sure we can append a message to a folder.
    """
    fixtures = imap_server
    imap = fixtures["client"]
    status, resp = imap.login(fixtures["user"].username, fixtures["password"])
    assert status == "OK"
    status, resp = imap.list()
    status, resp = imap.status(
        "INBOX", "(messages recent uidnext uidvalidity unseen)"
    )
    status, resp = imap.select(mailbox="INBOX")
    msg = email_factory()
    now = datetime.now(UTC).astimezone()
    status, resp = imap.append("INBOX", r"\Unseen", now, msg.as_bytes())
    status, resp = imap.status(
        "INBOX", "(messages recent uidnext uidvalidity unseen)"
    )
    status, resp = imap.logout()
    assert status == "BYE"


# ####################################################################
# #
# @pytest.mark.integration
# def test_server_two_clients(
#     bunch_of_email_in_folder, imap_server, imap_user_server_program
# ):
#     """
#     Make sure that if we have multiple clients basic operations work fine
#     """
#     pass


########################################################################
########################################################################
#
def _make_imap_client(
    reader: asyncio.StreamReader,
) -> tuple[IMAPClient, AsyncMock]:
    """
    Create an IMAPClient with the given reader and a mock writer.
    Returns (client, push_mock) where push_mock captures all output.
    """
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.write = MagicMock()
    writer.drain = AsyncMock()

    imap_server = MagicMock()
    imap_server.debug = False

    client = IMAPClient(
        imap_server, "test:1234", "127.0.0.1", 1234, reader, writer
    )
    push_mock = AsyncMock()
    client.push = push_mock  # type: ignore[method-assign]
    return client, push_mock


def _get_push_messages(push_mock: AsyncMock) -> list[bytes]:
    """Extract all messages sent via push() calls."""
    messages: list[bytes] = []
    for call in push_mock.call_args_list:
        for arg in call.args:
            if isinstance(arg, bytes):
                messages.append(arg)
            elif isinstance(arg, str):
                messages.append(arg.encode("latin-1"))
    return messages


########################################################################
########################################################################
#
class TestIMAPClientInputLimits:
    """Tests for input size limit enforcement in IMAPClient."""

    ####################################################################
    #
    @pytest.mark.asyncio
    async def test_oversized_literal_rejected(self) -> None:
        """
        GIVEN: a client that declares a literal larger than MAX_INPUT_SIZE
        WHEN:  the literal declaration is received
        THEN:  the server responds with BAD and does not read the literal
        """
        reader = asyncio.StreamReader()
        client, push_mock = _make_imap_client(reader)

        oversized = MAX_INPUT_SIZE + 1
        # Feed the literal declaration and its trailing CRLF, then a
        # normal command so the loop can complete, then EOF.
        #
        reader.feed_data(f"A001 APPEND INBOX {{{oversized}}}\r\n".encode())
        # The server reads one more line after rejecting (to drain the
        # literal declaration's trailing CRLF / next line).
        #
        reader.feed_data(b"\r\n")
        reader.feed_data(b"A002 LOGOUT\r\n")
        reader.feed_eof()

        await client.start()

        messages = _get_push_messages(push_mock)
        bad_responses = [
            m for m in messages if b"BAD" in m and b"literal size" in m
        ]
        assert len(bad_responses) == 1

    ####################################################################
    #
    @pytest.mark.asyncio
    async def test_oversized_literal_sync_no_continuation(self) -> None:
        """
        GIVEN: a client that declares a synchronizing literal (no +)
               larger than MAX_INPUT_SIZE
        WHEN:  the literal declaration is received
        THEN:  no continuation response is sent before the BAD
        """
        reader = asyncio.StreamReader()
        client, push_mock = _make_imap_client(reader)

        oversized = MAX_INPUT_SIZE + 1
        reader.feed_data(f"A001 APPEND INBOX {{{oversized}}}\r\n".encode())
        reader.feed_data(b"\r\n")
        reader.feed_data(b"A002 LOGOUT\r\n")
        reader.feed_eof()

        await client.start()

        messages = _get_push_messages(push_mock)
        # Filter out the initial OK capability greeting.
        #
        non_greeting = [m for m in messages if not m.startswith(b"* OK")]
        # The first non-greeting response should be the BAD, not a
        # continuation "+".
        #
        assert len(non_greeting) >= 1
        assert b"+ " not in non_greeting[0]
        assert b"BAD" in non_greeting[0]

    ####################################################################
    #
    @pytest.mark.asyncio
    async def test_ibuffer_overflow_via_many_lines(self) -> None:
        """
        GIVEN: a client that sends many lines without completing a command
               that accumulate past MAX_INPUT_SIZE
        WHEN:  the ibuffer size exceeds the limit
        THEN:  the server responds with BAD and resets the buffer
        """
        reader = asyncio.StreamReader()
        client, push_mock = _make_imap_client(reader)

        # Build a command that starts with a tag and then accumulates
        # data via multiple literals that together exceed the limit.
        # Each literal is under the limit individually but the sum
        # exceeds it.
        #
        chunk_size = MAX_INPUT_SIZE // 2 + 1
        # First literal
        reader.feed_data(f"A001 APPEND INBOX {{{chunk_size}+}}\r\n".encode())
        reader.feed_data(b"X" * chunk_size)
        # Second literal pushes ibuffer over the limit
        reader.feed_data(f"{{{chunk_size}+}}\r\n".encode())
        reader.feed_data(b"X" * chunk_size)
        # Need a line terminator for the loop to check ibuffer size
        reader.feed_data(b"\r\n")
        reader.feed_data(b"A002 LOGOUT\r\n")
        reader.feed_eof()

        await client.start()

        messages = _get_push_messages(push_mock)
        bad_responses = [
            m
            for m in messages
            if b"BAD" in m and b"command exceeds maximum" in m
        ]
        assert len(bad_responses) >= 1

    ####################################################################
    #
    @pytest.mark.asyncio
    async def test_normal_literal_accepted(self) -> None:
        """
        GIVEN: a client that sends a literal within the size limit
        WHEN:  the literal is received
        THEN:  no BAD response is generated for the literal
        """
        reader = asyncio.StreamReader()
        client, push_mock = _make_imap_client(reader)

        # A small literal should be accepted without error. The command
        # itself will fail (no subprocess) but should not trigger a
        # size limit BAD.
        #
        literal_data = b"X" * 100
        reader.feed_data(
            f"A001 APPEND INBOX {{{len(literal_data)}}}\r\n".encode()
        )
        # Continuation is sent for synchronizing literal, then data.
        reader.feed_data(literal_data)
        reader.feed_data(b"\r\n")
        reader.feed_data(b"A002 LOGOUT\r\n")
        reader.feed_eof()

        await client.start()

        messages = _get_push_messages(push_mock)
        bad_size_responses = [
            m
            for m in messages
            if b"BAD" in m and (b"literal size" in m or b"command exceeds" in m)
        ]
        assert len(bad_size_responses) == 0
