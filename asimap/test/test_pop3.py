#!/usr/bin/env python
#
"""Test the POP3 server components."""

# system imports
#
import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

# 3rd party imports
#
import pytest
import pytest_asyncio
from faker import Faker
from pytest_mock import MockerFixture

# Project imports
#
from asimap.pop3_client import POP3ClientProxy, POP3CommandHandler, dot_stuff
from asimap.pop3_parse import BadPOP3Command, parse_pop3_command
from asimap.user_server import IMAPUserServer


########################################################################
#
class TestPOP3Parse:
    """Tests for POP3 command parsing."""

    ################################################################
    #
    @pytest.mark.parametrize(
        "input_val,expected_cmd,expected_args",
        [
            ("STAT", "STAT", ""),
            ("stat", "STAT", ""),
            ("LIST 1", "LIST", "1"),
            ("RETR 5", "RETR", "5"),
            ("TOP 1 10", "TOP", "1 10"),
            ("DELE 3", "DELE", "3"),
            ("UIDL", "UIDL", ""),
            ("NOOP", "NOOP", ""),
            ("RSET", "RSET", ""),
            ("QUIT", "QUIT", ""),
            ("USER alice@example.com", "USER", "alice@example.com"),
            ("PASS my secret password", "PASS", "my secret password"),
            ("CAPA", "CAPA", ""),
            (b"STAT\r\n", "STAT", ""),  # bytes input
        ],
    )
    def test_parse_valid_commands(
        self,
        input_val: str | bytes,
        expected_cmd: str,
        expected_args: str,
    ) -> None:
        """
        GIVEN: a valid POP3 command as string or bytes
        WHEN:  parse_pop3_command is called
        THEN:  the command and args are correctly parsed
        """
        cmd = parse_pop3_command(input_val)
        assert cmd.command == expected_cmd
        assert cmd.args == expected_args

    ################################################################
    #
    @pytest.mark.parametrize(
        "bad_input",
        [
            "",
            "BOGUS",
            "XYZZY 1 2 3",
            "LOGIN user pass",
        ],
    )
    def test_parse_invalid_raises(self, bad_input: str) -> None:
        """
        GIVEN: an empty or unknown POP3 command string
        WHEN:  parse_pop3_command is called
        THEN:  BadPOP3Command is raised
        """
        with pytest.raises(BadPOP3Command):
            parse_pop3_command(bad_input)


########################################################################
#
class TestDotStuff:
    """Tests for POP3 dot-stuffing."""

    ################################################################
    #
    @pytest.mark.parametrize(
        "input_bytes,expected",
        [
            (b"hello\r\n.world\r\n", b"hello\r\n..world\r\n"),
            (b"no dots here\r\n", b"no dots here\r\n"),
            (b".\r\n", b"..\r\n"),
            (b"..already dotted\r\n", b"...already dotted\r\n"),
            (b"", b""),
        ],
    )
    def test_dot_stuffing(self, input_bytes: bytes, expected: bytes) -> None:
        """
        GIVEN: message bytes that may contain lines starting with '.'
        WHEN:  dot_stuff is applied
        THEN:  lines starting with '.' get an extra '.' prepended
        """
        assert dot_stuff(input_bytes) == expected


########################################################################
#
@pytest_asyncio.fixture
async def pop3_client_proxy(
    faker: Faker, mocker: MockerFixture, imap_user_server: IMAPUserServer
) -> Callable[..., Any]:
    """
    Creates a POP3ClientProxy with a mocked push method for testing
    command handling without network I/O.
    """
    writers: list[asyncio.StreamWriter] = []

    async def _make_pop3_client_proxy() -> POP3ClientProxy:
        rem_addr = "127.0.0.1"
        port = faker.pyint(min_value=1024, max_value=65535)
        name = f"pop3-{rem_addr}:{port}"
        server = imap_user_server

        loop = asyncio.get_event_loop()
        devnull_writer = open("/dev/null", "wb")
        writer_transport, writer_protocol = await loop.connect_write_pipe(
            lambda: asyncio.streams.FlowControlMixin(loop=loop),
            devnull_writer,
        )

        writer = asyncio.StreamWriter(
            writer_transport, writer_protocol, None, loop
        )
        reader = asyncio.StreamReader()
        proxy = POP3ClientProxy(
            server,
            name,
            server.next_client_num,
            rem_addr,
            port,
            reader,
            writer,
        )
        server.next_client_num += 1
        mocker.patch.object(proxy, "push", AsyncMock())
        writers.append(writer)
        return proxy

    yield _make_pop3_client_proxy

    for writer in writers:
        writer.close()


########################################################################
#
def client_push_responses(
    client: POP3ClientProxy,
) -> list[str]:
    """
    Extract all push responses from a mocked POP3ClientProxy since
    the last call to this function.
    """
    results: list[str] = []
    for args, _ in client.push.call_args_list:
        for d in args:
            if isinstance(d, bytes):
                results.append(d.decode("latin-1"))
            else:
                results.append(d)
    client.push.reset_mock()
    return results


########################################################################
#
class TestPOP3CommandHandler:
    """Tests for POP3 command handling in the user subprocess."""

    pytestmark = pytest.mark.asyncio

    ################################################################
    #
    @pytest.mark.asyncio
    async def test_stat(
        self,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: an INBOX with messages
        WHEN:  STAT command is issued
        THEN:  response is "+OK count total_size"
        """
        bunch_of_email_in_folder(num_emails=5, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        result = await handler.command(parse_pop3_command("STAT"))
        assert result is True

        responses = client_push_responses(proxy)
        assert len(responses) == 1
        parts = responses[0].strip().split()
        assert parts[0] == "+OK"
        assert int(parts[1]) == 5
        assert int(parts[2]) > 0

    ################################################################
    #
    @pytest.mark.asyncio
    async def test_list_all(
        self,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: an INBOX with messages
        WHEN:  LIST command is issued without arguments
        THEN:  multi-line response with all message numbers and sizes
        """
        bunch_of_email_in_folder(num_emails=3, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        await handler.command(parse_pop3_command("LIST"))

        responses = client_push_responses(proxy)
        text = responses[0]
        assert text.startswith("+OK 3 messages")
        lines = text.strip().split("\r\n")
        assert lines[-1] == "."
        for line in lines[1:-1]:
            num, size = line.split()
            assert int(num) >= 1
            assert int(size) > 0

    ################################################################
    #
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command,expected_prefix",
        [
            ("LIST 1", "+OK 1 "),
            ("LIST 99", "-ERR"),
            ("LIST 0", "-ERR"),
            ("UIDL 1", "+OK 1 "),
            ("UIDL 99", "-ERR"),
            ("RETR 99", "-ERR"),
            ("DELE 99", "-ERR"),
            ("NOOP", "+OK"),
        ],
    )
    async def test_single_line_responses(
        self,
        command: str,
        expected_prefix: str,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: an INBOX with 5 messages
        WHEN:  various commands are issued
        THEN:  the response starts with the expected prefix
        """
        bunch_of_email_in_folder(num_emails=5, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        await handler.command(parse_pop3_command(command))

        responses = client_push_responses(proxy)
        assert responses[0].startswith(expected_prefix)

    ################################################################
    #
    @pytest.mark.asyncio
    async def test_retr(
        self,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: an INBOX with messages
        WHEN:  RETR is issued for a valid message
        THEN:  full message content is returned with terminating dot
        """
        bunch_of_email_in_folder(num_emails=3, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        await handler.command(parse_pop3_command("RETR 1"))

        responses = client_push_responses(proxy)
        assert len(responses) == 1
        text = responses[0]
        assert text.startswith("+OK")
        assert text.endswith("\r\n.\r\n")

    ################################################################
    #
    @pytest.mark.asyncio
    async def test_dele_and_quit_expunges(
        self,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: an INBOX with messages, some marked for deletion
        WHEN:  QUIT is issued
        THEN:  marked messages are expunged from the mailbox
        """
        bunch_of_email_in_folder(num_emails=5, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        assert handler.msg_count == 5

        await handler.command(parse_pop3_command("DELE 1"))
        client_push_responses(proxy)
        await handler.command(parse_pop3_command("DELE 3"))
        client_push_responses(proxy)

        assert handler.deleted == {1, 3}

        result = await handler.command(parse_pop3_command("QUIT"))
        assert result is False

        responses = client_push_responses(proxy)
        assert responses[0].startswith("+OK")

        assert handler.mbox is not None
        assert handler.mbox.num_msgs == 3

    ################################################################
    #
    @pytest.mark.asyncio
    async def test_quit_without_dele_no_expunge(
        self,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: an INBOX with messages, none deleted
        WHEN:  QUIT is issued
        THEN:  no messages are expunged
        """
        bunch_of_email_in_folder(num_emails=5, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        result = await handler.command(parse_pop3_command("QUIT"))
        assert result is False

        assert handler.mbox is not None
        assert handler.mbox.num_msgs == 5

    ################################################################
    #
    @pytest.mark.asyncio
    async def test_dele_and_rset_clears(
        self,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: messages marked for deletion
        WHEN:  RSET is issued
        THEN:  all deletion marks are cleared
        """
        bunch_of_email_in_folder(num_emails=5, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        await handler.command(parse_pop3_command("DELE 1"))
        await handler.command(parse_pop3_command("DELE 2"))
        client_push_responses(proxy)
        assert len(handler.deleted) == 2

        await handler.command(parse_pop3_command("RSET"))

        responses = client_push_responses(proxy)
        assert responses[0].startswith("+OK")
        assert len(handler.deleted) == 0

    ################################################################
    #
    @pytest.mark.asyncio
    async def test_uidl_all(
        self,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: an INBOX with messages
        WHEN:  UIDL is issued without arguments
        THEN:  multi-line response maps POP3 msg nums to IMAP UIDs
        """
        bunch_of_email_in_folder(num_emails=3, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        await handler.command(parse_pop3_command("UIDL"))

        responses = client_push_responses(proxy)
        text = responses[0]
        assert text.startswith("+OK")
        lines = text.strip().split("\r\n")
        assert lines[-1] == "."
        for line in lines[1:-1]:
            num, uid = line.split()
            assert int(num) >= 1
            assert int(uid) > 0

    ################################################################
    #
    @pytest.mark.asyncio
    async def test_top(
        self,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: an INBOX with messages
        WHEN:  TOP msg n is issued
        THEN:  headers plus first n lines of body are returned
        """
        bunch_of_email_in_folder(num_emails=3, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        await handler.command(parse_pop3_command("TOP 1 5"))

        responses = client_push_responses(proxy)
        text = responses[0]
        assert "+OK" in text
        assert text.endswith("\r\n.\r\n")

    ################################################################
    #
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_args",
        [
            "1",  # Missing n
            "abc 5",  # Non-numeric msg
            "1 -1",  # Negative n
        ],
    )
    async def test_top_invalid_args(
        self,
        bad_args: str,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: invalid TOP arguments
        WHEN:  TOP command is issued
        THEN:  error response is returned
        """
        bunch_of_email_in_folder(num_emails=3, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        await handler.command(parse_pop3_command(f"TOP {bad_args}"))

        responses = client_push_responses(proxy)
        assert responses[0].startswith("-ERR")

    ################################################################
    #
    @pytest.mark.asyncio
    async def test_capa(
        self,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: an active POP3 session
        WHEN:  CAPA is issued
        THEN:  capability list is returned with expected capabilities
        """
        bunch_of_email_in_folder(num_emails=1, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        await handler.command(parse_pop3_command("CAPA"))

        responses = client_push_responses(proxy)
        text = responses[0]
        assert "+OK" in text
        assert "USER" in text
        assert "UIDL" in text
        assert "TOP" in text

    ################################################################
    #
    @pytest.mark.asyncio
    async def test_deleted_msg_invisible(
        self,
        bunch_of_email_in_folder: Callable[..., Path],
        imap_user_server: IMAPUserServer,
        pop3_client_proxy: Callable[..., Any],
    ) -> None:
        """
        GIVEN: message 2 is DELEted
        WHEN:  STAT, LIST 2, RETR 2 are issued
        THEN:  STAT excludes msg 2; LIST 2 and RETR 2 return errors
        """
        bunch_of_email_in_folder(num_emails=5, folder="inbox")
        proxy = await pop3_client_proxy()
        handler = POP3CommandHandler(proxy, imap_user_server)
        await handler.init_session()

        await handler.command(parse_pop3_command("DELE 2"))
        client_push_responses(proxy)

        # STAT should exclude msg 2.
        #
        await handler.command(parse_pop3_command("STAT"))
        responses = client_push_responses(proxy)
        parts = responses[0].strip().split()
        assert int(parts[1]) == 4  # 5 - 1 deleted

        # LIST 2 and RETR 2 should fail.
        #
        for cmd in ("LIST 2", "RETR 2", "UIDL 2", "DELE 2"):
            await handler.command(parse_pop3_command(cmd))
            responses = client_push_responses(proxy)
            assert responses[0].startswith("-ERR"), f"{cmd} should fail"
