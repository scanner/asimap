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
import pytest_asyncio
import pytest_check as check
from pytest_mock import MockerFixture

# Project imports
#
from ..client import CAPABILITIES, ClientState
from ..constants import (
    MAX_APPEND_SIZE,
    MAX_DISCARD_SIZE,
    MAX_INPUT_SIZE,
    MAX_NON_APPEND_SIZE,
)
from ..server import IMAPClient
from .conftest import EmailFactoryType

type InputLimitClient = tuple[asyncio.StreamReader, IMAPClient, AsyncMock]

# The literal size that makes a complete 'A001 SEARCH TEXT {n}' command exactly
# MAX_NON_APPEND_SIZE. The command line has a 6 digit size in it.
#
SEARCH_LITERAL_AT_LIMIT = MAX_NON_APPEND_SIZE - len(
    "A001 SEARCH TEXT {123456}\r\n"
)


####################################################################
#
@pytest_asyncio.fixture
async def input_limit_client() -> InputLimitClient:
    """
    An IMAPClient reading from a StreamReader that a test feeds, with the
    mock that captures everything it sends.
    """
    reader = asyncio.StreamReader()
    client, push_mock = _make_imap_client(reader)
    return reader, client, push_mock


####################################################################
#
@pytest.fixture
def authenticated(input_limit_client: InputLimitClient) -> None:
    """Put the client from 'input_limit_client' in the logged in state."""
    _, client, _ = input_limit_client
    client.subprocess_intf.client_handler.state = ClientState.AUTHENTICATED


####################################################################
#
@pytest.fixture
def subprocess_message(
    input_limit_client: InputLimitClient, mocker: MockerFixture
) -> AsyncMock:
    """
    Replace the method that hands complete commands to the next layer, and
    return the mock so a test can see what got through.
    """
    _, client, _ = input_limit_client
    return mocker.patch.object(
        client.subprocess_intf,
        "message",
        new_callable=AsyncMock,
        return_value=True,
    )


####################################################################
#
@pytest.fixture
def message_of_size() -> Callable[[int], bytes]:
    """A factory for an RFC 5322 message of exactly the requested size."""

    def factory(size: int) -> bytes:
        headers = (
            b"From: sender@example.com\r\nTo: rcpt@example.com\r\n"
            b"Subject: A large message\r\n\r\n"
        )
        line = b"A" * 76 + b"\r\n"
        body_size = size - len(headers)
        body = line * (body_size // len(line) + 1)
        return headers + body[:body_size]

    return factory


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


####################################################################
#
@pytest.mark.integration
def test_server_append_enforces_advertised_limit(
    bunch_of_email_in_folder: Callable[..., Path],
    imap_server: dict[str, Any],
    imap_user_server_program: None,
    message_of_size: Callable[[int], bytes],
) -> None:
    """
    GIVEN: a logged in client that reads APPENDLIMIT from CAPABILITY
    WHEN:  it appends a message of exactly that size, and then one byte larger
    THEN:  the first is stored, the second is rejected with NO [TOOBIG], and
           the connection keeps working
    """
    fixtures = imap_server
    imap = fixtures["client"]
    status, resp = imap.login(fixtures["user"].username, fixtures["password"])
    assert status == "OK"
    status, capabilities = imap.capability()
    assert status == "OK"
    prefix = "APPENDLIMIT="
    limit = next(
        int(cap.removeprefix(prefix))
        for cap in str(capabilities[0], "ascii").split()
        if cap.startswith(prefix)
    )
    check.equal(limit, MAX_APPEND_SIZE, "advertises the message size constant")

    status, resp = imap.status("INBOX", "(MESSAGES)")
    assert status == "OK"
    count_before = int(str(resp[0], "ascii").split()[-1].rstrip(")"))

    status, resp = imap.append("INBOX", None, None, message_of_size(limit))
    check.equal(status, "OK", "message of the advertised size is accepted")

    status, resp = imap.append("INBOX", None, None, message_of_size(limit + 1))
    check.equal(status, "NO", "message one byte over is rejected")
    check.is_true(resp and resp[0].startswith(b"[TOOBIG]"), repr(resp))

    status, resp = imap.status("INBOX", "(MESSAGES)")
    assert status == "OK"
    count_after = int(str(resp[0], "ascii").split()[-1].rstrip(")"))
    check.equal(count_after, count_before + 1, "only one message was stored")
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


####################################################################
#
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


####################################################################
#
def _responses_after_greeting(push_mock: AsyncMock) -> list[bytes]:
    """Everything the server sent other than its opening greeting."""
    return _get_push_messages(push_mock)[1:]


########################################################################
########################################################################
#
class TestIMAPClientInputLimits:
    """Tests for input size limit enforcement in IMAPClient."""

    ####################################################################
    #
    @pytest.mark.asyncio
    @pytest.mark.parametrize("plus", ["", "+"])
    async def test_message_at_limit_accepted(
        self,
        input_limit_client: InputLimitClient,
        authenticated: None,
        subprocess_message: AsyncMock,
        plus: str,
    ) -> None:
        """
        GIVEN: a logged in client using either literal type
        WHEN:  it sends an APPEND literal of exactly MAX_APPEND_SIZE
        THEN:  the whole command is passed on and not rejected
        """
        reader, client, push_mock = input_limit_client
        line = f"A001 APPEND INBOX {{{MAX_APPEND_SIZE}{plus}}}\r\n".encode()
        literal = b"X" * MAX_APPEND_SIZE

        reader.feed_data(line + literal + b"\r\n")
        reader.feed_eof()
        await client.start()

        subprocess_message.assert_awaited_once_with(line + literal)
        assert not any(
            b"exceeds" in m for m in _get_push_messages(push_mock)
        ), "a message of the advertised size must not be rejected"

    ####################################################################
    #
    @pytest.mark.asyncio
    async def test_non_append_command_at_limit_accepted(
        self,
        input_limit_client: InputLimitClient,
        authenticated: None,
        subprocess_message: AsyncMock,
    ) -> None:
        """
        GIVEN: a logged in client
        WHEN:  it sends a SEARCH of exactly MAX_NON_APPEND_SIZE
        THEN:  the whole command is passed on
        """
        reader, client, _ = input_limit_client
        line = f"A001 SEARCH TEXT {{{SEARCH_LITERAL_AT_LIMIT}}}\r\n".encode()
        literal = b"X" * SEARCH_LITERAL_AT_LIMIT
        assert len(line + literal) == MAX_NON_APPEND_SIZE

        reader.feed_data(line + literal + b"\r\n")
        reader.feed_eof()
        await client.start()

        subprocess_message.assert_awaited_once_with(line + literal)

    ####################################################################
    #
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "state,command,size,expected",
        [
            (
                ClientState.AUTHENTICATED,
                "A001 SEARCH TEXT ",
                SEARCH_LITERAL_AT_LIMIT + 1,
                b"A001 BAD ",
            ),
            (
                ClientState.NOT_AUTHENTICATED,
                "A001 APPEND INBOX ",
                MAX_NON_APPEND_SIZE + 1,
                b"A001 NO [TOOBIG] ",
            ),
        ],
        ids=["search-logged-in", "append-before-login"],
    )
    async def test_over_non_append_limit_rejected(
        self,
        input_limit_client: InputLimitClient,
        subprocess_message: AsyncMock,
        state: ClientState,
        command: str,
        size: int,
        expected: bytes,
    ) -> None:
        """
        GIVEN: a command that only gets the smaller limit: not an APPEND, or
               sent before login
        WHEN:  its literal takes it over MAX_NON_APPEND_SIZE
        THEN:  it is rejected, though it is well under MAX_APPEND_SIZE
        """
        reader, client, push_mock = input_limit_client
        client.subprocess_intf.client_handler.state = state
        reader.feed_data(f"{command}{{{size}}}\r\n".encode())
        reader.feed_eof()

        await client.start()

        subprocess_message.assert_not_awaited()
        responses = _responses_after_greeting(push_mock)
        assert responses[0].startswith(expected)

    ####################################################################
    #
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command,expected",
        [
            ("A001 APPEND INBOX ", b"A001 NO [TOOBIG] "),
            ("A001 LOGIN ", b"A001 BAD "),
            ("", b"* BAD "),
        ],
    )
    async def test_oversized_synchronizing_literal_rejected(
        self,
        input_limit_client: InputLimitClient,
        command: str,
        expected: bytes,
    ) -> None:
        """
        GIVEN: a client that declares a synchronizing literal one byte over
               MAX_APPEND_SIZE and so is waiting for our go-ahead
        WHEN:  the declaration is received
        THEN:  the command is rejected with no continuation, and the next
               line the client sends is handled as its next command
        """
        reader, client, push_mock = input_limit_client
        oversized = MAX_APPEND_SIZE + 1
        reader.feed_data(f"{command}{{{oversized}}}\r\n".encode())
        reader.feed_data(b"A002 LOGOUT\r\n")
        reader.feed_eof()

        await client.start()

        responses = _responses_after_greeting(push_mock)
        check.is_true(responses[0].startswith(expected), repr(responses[0]))
        check.is_false(
            any(m.startswith(b"+") for m in responses), "no continuation"
        )
        check.is_true(
            any(m.startswith(b"A002 OK") for m in responses),
            "next command is handled",
        )

    ####################################################################
    #
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "rest_of_command",
        [b"\r\n", b" {5+}\r\nhello\r\n", b" {5}\r\n"],
        ids=["ends", "another-non-sync-literal", "sync-literal-not-sent"],
    )
    async def test_oversized_non_synchronizing_literal_discarded(
        self, input_limit_client: InputLimitClient, rest_of_command: bytes
    ) -> None:
        """
        GIVEN: a client that sends a non-synchronizing literal one byte over
               MAX_APPEND_SIZE whose content looks like IMAP commands
        WHEN:  the literal and the rest of the command are received
        THEN:  the literal is dropped without being parsed, only the tagged
               rejection is sent, and the next command is handled
        """
        reader, client, push_mock = input_limit_client
        oversized = MAX_APPEND_SIZE + 1
        filler = b"A003 NOOP\r\n"
        literal = (filler * (oversized // len(filler) + 1))[:oversized]
        reader.feed_data(f"A001 APPEND INBOX {{{oversized}+}}\r\n".encode())
        reader.feed_data(literal + rest_of_command)
        reader.feed_data(b"A002 LOGOUT\r\n")
        reader.feed_eof()

        await client.start()

        responses = _responses_after_greeting(push_mock)
        check.is_true(
            responses[0].startswith(b"A001 NO [TOOBIG] "), repr(responses[0])
        )
        check.is_false(
            any(m.startswith((b"A003", b"* BAD")) for m in responses),
            "nothing else in the command is treated as input",
        )
        check.is_true(
            any(m.startswith(b"A002 OK") for m in responses),
            "next command is handled",
        )

    ####################################################################
    #
    @pytest.mark.asyncio
    async def test_discard_limit_disconnects(
        self, input_limit_client: InputLimitClient
    ) -> None:
        """
        GIVEN: a client that declares a non-synchronizing literal larger than
               MAX_DISCARD_SIZE
        WHEN:  the declaration is received
        THEN:  the server sends BYE and stops without reading further input
        """
        reader, client, push_mock = input_limit_client
        declared = MAX_DISCARD_SIZE + 1
        reader.feed_data(f"A001 APPEND INBOX {{{declared}+}}\r\n".encode())
        reader.feed_data(b"A002 LOGOUT\r\n")

        await client.start()

        responses = _responses_after_greeting(push_mock)
        assert len(responses) == 1
        assert responses[0].startswith(b"* BYE [TOOBIG] ")

    ####################################################################
    #
    @pytest.mark.asyncio
    async def test_literals_accumulating_past_limit_rejected(
        self,
        input_limit_client: InputLimitClient,
        authenticated: None,
        subprocess_message: AsyncMock,
    ) -> None:
        """
        GIVEN: a logged in client that sends two literals, each within MAX_APPEND_SIZE
               but together over MAX_INPUT_SIZE
        WHEN:  the second literal is declared
        THEN:  the command is rejected with a tagged NO [TOOBIG]
        """
        reader, client, push_mock = input_limit_client
        chunk_size = MAX_INPUT_SIZE // 2 + 1
        reader.feed_data(f"A001 APPEND {{{chunk_size}+}}\r\n".encode())
        reader.feed_data(b"X" * chunk_size)
        reader.feed_data(f" {{{chunk_size}+}}\r\n".encode())
        reader.feed_data(b"X" * chunk_size + b"\r\n")
        reader.feed_data(b"A002 LOGOUT\r\n")
        reader.feed_eof()

        await client.start()

        responses = _responses_after_greeting(push_mock)
        check.is_true(
            responses[0].startswith(b"A001 NO [TOOBIG] "), repr(responses[0])
        )
        subprocess_message.assert_awaited_once_with(b"A002 LOGOUT")

    ####################################################################
    #
    @pytest.mark.asyncio
    async def test_rest_of_command_past_limit_rejected(
        self,
        input_limit_client: InputLimitClient,
        authenticated: None,
        subprocess_message: AsyncMock,
    ) -> None:
        """
        GIVEN: a logged in client whose literals fit within MAX_INPUT_SIZE
        WHEN:  the line after the last literal takes it over the limit
        THEN:  the command is rejected with a tagged NO [TOOBIG]
        """
        reader, client, push_mock = input_limit_client
        first_literal = 60_000
        reader.feed_data(f"A001 APPEND {{{first_literal}+}}\r\n".encode())
        reader.feed_data(b"X" * first_literal)
        reader.feed_data(f" {{{MAX_APPEND_SIZE}+}}\r\n".encode())
        reader.feed_data(b"X" * MAX_APPEND_SIZE)
        reader.feed_data(b" " + b"Y" * 6_000 + b"\r\n")
        reader.feed_eof()

        await client.start()

        subprocess_message.assert_not_awaited()
        responses = _responses_after_greeting(push_mock)
        assert responses[0].startswith(b"A001 NO [TOOBIG] ")
