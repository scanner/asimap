#!/usr/bin/env python
#
"""
POP3 client handler for the asimap user subprocess.

This module contains the POP3ClientProxy (network proxy) and
POP3CommandHandler (command state machine) that run inside the per-user
subprocess. POP3 access is restricted to INBOX only.

The POP3 session snapshots the INBOX message list at session start.
Messages marked for deletion via DELE are only expunged on a clean QUIT.
"""

# system imports
#
import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

# Project imports
#
import asimap.trace
from asimap.generator import get_msg_size, msg_as_bytes, msg_headers_as_bytes
from asimap.pop3_parse import BadPOP3Command, parse_pop3_command
from asimap.trace import trace

if TYPE_CHECKING:
    from asimap.mbox import Mailbox
    from asimap.user_server import IMAPUserServer

logger = logging.getLogger(__name__)

# Regex for reading length-framed messages from the root server.
# Format: {<digits>}\n
#
RE_LITERAL_STRING_START = re.compile(rb"\{(\d+)\}")


########################################################################
#
def dot_stuff(data: bytes) -> bytes:
    """
    POP3 dot-stuffing per RFC 1939 Section 3.

    Any line in a multi-line response that begins with '.' (0x2E) gets
    an extra '.' prepended. The termination line '.' is added by the
    caller, not by this function.
    """
    lines = data.split(b"\r\n")
    result = []
    for line in lines:
        if line.startswith(b"."):
            result.append(b"." + line)
        else:
            result.append(line)
    return b"\r\n".join(result)


########################################################################
#
class POP3ClientProxy:
    """
    Proxy for a POP3 client connection in the user subprocess.

    Receives length-framed POP3 commands from the root server and
    dispatches them to the POP3CommandHandler. Sends POP3 responses
    (CRLF-terminated lines) back to the root server for relay to the
    client.
    """

    LINE_TERMINATOR = b"\n"

    ##################################################################
    #
    def __init__(
        self,
        server: "IMAPUserServer",
        name: str,
        client_num: int,
        rem_addr: str,
        port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        """
        Args:
            server: The :class:`~asimap.user_server.IMAPUserServer` this
                proxy belongs to.
            name: Human-readable identifier for the connection.
            client_num: Monotonically increasing client index for logging.
            rem_addr: Remote IP address of the originating POP3 client.
            port: Remote port of the originating POP3 client.
            reader: Async stream reader connected to the root server.
            writer: Async stream writer connected to the root server.
        """
        self.log = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.client_num = client_num
        self.name = name
        self.rem_addr = rem_addr
        self.port = port
        self.reader = reader
        self.writer = writer
        self.server = server
        self.client_connected = False
        self.cmd_handler: POP3CommandHandler | None = None

    ####################################################################
    #
    async def close(self, cancel_reader: bool = True) -> None:
        """Shutdown the proxy connection."""
        self.client_connected = False
        try:
            if not self.writer.is_closing():
                self.writer.close()
            await self.writer.wait_closed()

            if cancel_reader:
                for task, client in self.server.clients.items():
                    if client == self:
                        if not task.done():
                            task.cancel()
                            await task
                        break
        except OSError:
            pass
        except asyncio.CancelledError:
            self.log.info("Cancelled: %s", self)
            raise
        except Exception as exc:
            self.log.error("Exception when closing %s: %s", self, exc)
        finally:
            self.trace("CLOSE", {})

    ####################################################################
    #
    async def run(self) -> None:
        """
        Main loop for handling POP3 commands from the root server.

        Initializes the POP3 session (snapshots INBOX), then reads
        length-framed commands and dispatches them to the command
        handler.
        """
        msg: bytes
        try:
            self.trace("CONNECT", {})
            self.client_connected = True

            # Initialize the POP3 session — snapshot the INBOX.
            #
            self.cmd_handler = POP3CommandHandler(self, self.server)
            await self.cmd_handler.init_session()

            while self.client_connected:
                msg = await self.reader.readuntil(self.LINE_TERMINATOR)
                m = RE_LITERAL_STRING_START.search(msg)
                if not m:
                    self.log.warning(
                        "POP3 client sent invalid message start: %r", msg
                    )
                    self.client_connected = False
                    break
                length = int(m.group(1))
                msg = await self.reader.readexactly(length)
                cmd_line = msg.decode("latin-1")
                self.trace("RECEIVED", {"data": cmd_line})

                try:
                    pop3_cmd = parse_pop3_command(cmd_line)
                except BadPOP3Command as e:
                    await self.push(f"-ERR {e.value}\r\n")
                    continue

                should_continue = await self.cmd_handler.command(pop3_cmd)
                if not should_continue:
                    return

        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            OSError,
        ):
            pass
        except asyncio.CancelledError:
            self.log.info("POP3 client task cancelled: %s", self.name)
            raise
        except Exception:
            self.log.exception("POP3 client error: %s", self.name)
        finally:
            # NOTE: No expunge on unclean disconnect. Only QUIT expunges.
            await self.close(cancel_reader=False)

    ####################################################################
    #
    def trace(self, msg_type: str, msg: dict[str, Any]) -> None:
        """Log a trace message for this POP3 connection."""
        msg["connection"] = self.name
        msg["remote"] = f"{self.rem_addr}:{self.port}"
        msg["msg_type"] = msg_type
        msg["protocol"] = "pop3"
        trace(msg)

    ####################################################################
    #
    async def push(self, *data: bytes | str) -> None:
        """
        Write data to the POP3 client by sending it to the root server,
        which relays it to the POP3 client.
        """
        for d in data:
            try:
                d = d.encode("latin-1") if isinstance(d, str) else d
            except UnicodeEncodeError:
                logger.warning("Unable to encode string using `latin-1`: %s", d)
                d = d.encode("utf-8", "replace") if isinstance(d, str) else d
            try:
                self.writer.write(d)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ConnectionError(
                    f"unable to write message: {exc!r}"
                ) from exc

        if not self.writer.is_closing():
            try:
                async with asyncio.timeout(2):
                    await self.writer.drain()
            except TimeoutError as exc:
                logger.warning(
                    "Closing writer stream for %s, %s, reason: timed out "
                    "attempting push: %s",
                    self.name,
                    self.rem_addr,
                    exc,
                )
                self.writer.close()

        if asimap.trace.TRACE_ENABLED:
            try:
                for d in data:
                    msg = str(d, "latin-1") if isinstance(d, bytes) else d
                    self.trace("SEND", {"data": msg})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Error sending trace: %s", e)

    ##################################################################
    #
    def log_string(self) -> str:
        """Format the remote address/port as a string."""
        return f"pop3:{self.rem_addr}:{self.port}"

    def __str__(self) -> str:
        return self.log_string()


########################################################################
#
class POP3CommandHandler:
    """
    Handles POP3 commands in the TRANSACTION state within the user
    subprocess. INBOX-only. Snapshot-based message list.

    POP3 message numbers are 1-based indices into the snapshot taken
    at session start. Messages marked for deletion via DELE are excluded
    from STAT, LIST, RETR, etc. and are only actually expunged on QUIT.
    """

    ##################################################################
    #
    def __init__(
        self,
        client: POP3ClientProxy,
        server: "IMAPUserServer",
    ):
        """
        Args:
            client: The :class:`POP3ClientProxy` that receives responses.
            server: The :class:`~asimap.user_server.IMAPUserServer` used to
                access the user's mailboxes.
        """
        self.client = client
        self.server = server
        self.mbox: Mailbox | None = None

        # Snapshot taken at session start.
        #
        self.snapshot_msg_keys: list[int] = []
        self.snapshot_uids: list[int] = []
        self.msg_count: int = 0

        # Message sizes keyed by POP3 message number (1-based).
        # Computed lazily to avoid slow startup on large mailboxes.
        #
        self.msg_sizes: dict[int, int] = {}

        # POP3 message numbers marked for deletion (1-based).
        #
        self.deleted: set[int] = set()

    ##################################################################
    #
    async def init_session(self) -> None:
        """
        Initialize the POP3 session by getting the INBOX mailbox and
        snapshotting its message list.
        """
        self.mbox = await self.server.get_mailbox("inbox")
        self.snapshot_msg_keys = list(self.mbox.msg_keys)
        self.snapshot_uids = list(self.mbox.uids)
        self.msg_count = len(self.snapshot_msg_keys)

    ##################################################################
    #
    def _get_msg_size(self, pop3_num: int) -> int:
        """
        Get the size of a message in octets, computing lazily and
        caching the result.
        """
        if pop3_num not in self.msg_sizes:
            assert self.mbox is not None
            msg_key = self.snapshot_msg_keys[pop3_num - 1]
            try:
                msg = self.mbox.get_msg(msg_key)
                self.msg_sizes[pop3_num] = get_msg_size(msg)
            except (KeyError, FileNotFoundError):
                # Message disappeared (concurrent modification).
                self.msg_sizes[pop3_num] = 0
        return self.msg_sizes[pop3_num]

    ##################################################################
    #
    def _valid_msg_num(self, num_str: str) -> int | None:
        """
        Parse and validate a POP3 message number.

        Returns the message number if valid and not deleted, else None.
        """
        try:
            n = int(num_str)
        except ValueError:
            return None
        if n < 1 or n > self.msg_count:
            return None
        if n in self.deleted:
            return None
        return n

    ##################################################################
    #
    async def command(self, pop3_cmd: "asimap.pop3_parse.POP3Command") -> bool:
        """
        Dispatch a POP3 command.

        Returns True to continue the session, False to disconnect.
        """
        handler = getattr(self, f"do_{pop3_cmd.command.lower()}", None)
        if handler is None:
            await self.client.push(
                f"-ERR unknown command: {pop3_cmd.command}\r\n"
            )
            return True
        return await handler(pop3_cmd.args)

    ##################################################################
    #
    async def do_stat(self, args: str) -> bool:
        """STAT: return count and total size of non-deleted messages."""
        count = 0
        total = 0
        for num in range(1, self.msg_count + 1):
            if num not in self.deleted:
                count += 1
                total += self._get_msg_size(num)
        await self.client.push(f"+OK {count} {total}\r\n")
        return True

    ##################################################################
    #
    async def do_list(self, args: str) -> bool:
        """LIST [msg]: message number and size listing."""
        if args:
            n = self._valid_msg_num(args)
            if n is None:
                await self.client.push("-ERR no such message\r\n")
                return True
            await self.client.push(f"+OK {n} {self._get_msg_size(n)}\r\n")
        else:
            count = 0
            total = 0
            lines: list[str] = []
            for num in range(1, self.msg_count + 1):
                if num not in self.deleted:
                    size = self._get_msg_size(num)
                    count += 1
                    total += size
                    lines.append(f"{num} {size}\r\n")
            result = f"+OK {count} messages ({total} octets)\r\n"
            result += "".join(lines)
            result += ".\r\n"
            await self.client.push(result)
        return True

    ##################################################################
    #
    async def do_retr(self, args: str) -> bool:
        """RETR msg: retrieve full message."""
        n = self._valid_msg_num(args)
        if n is None:
            await self.client.push("-ERR no such message\r\n")
            return True

        assert self.mbox is not None
        msg_key = self.snapshot_msg_keys[n - 1]
        try:
            msg = self.mbox.get_msg(msg_key)
        except (KeyError, FileNotFoundError):
            await self.client.push("-ERR message not available\r\n")
            return True

        msg_bytes = msg_as_bytes(msg)
        size = len(msg_bytes)
        msg_bytes = dot_stuff(msg_bytes)
        await self.client.push(
            f"+OK {size} octets\r\n".encode("latin-1")
            + msg_bytes
            + b"\r\n.\r\n"
        )
        return True

    ##################################################################
    #
    async def do_dele(self, args: str) -> bool:
        """DELE msg: mark message for deletion."""
        n = self._valid_msg_num(args)
        if n is None:
            await self.client.push("-ERR no such message\r\n")
            return True
        self.deleted.add(n)
        await self.client.push(f"+OK message {n} deleted\r\n")
        return True

    ##################################################################
    #
    async def do_quit(self, args: str) -> bool:
        """
        QUIT: enter UPDATE state, expunge deleted messages, disconnect.

        Only a clean QUIT causes expunge. Disconnecting without QUIT
        leaves all messages intact.
        """
        assert self.mbox is not None
        if self.deleted:
            uids_to_delete = [
                self.snapshot_uids[n - 1] for n in sorted(self.deleted)
            ]
            try:
                await self.mbox.expunge(
                    uid_msg_set=uids_to_delete,
                    check_deleted=False,
                )
            except Exception:
                logger.exception("Error expunging messages on POP3 QUIT")
                await self.client.push(
                    "-ERR some deleted messages not removed\r\n"
                )
                return False

        await self.client.push("+OK Bye\r\n")
        return False

    ##################################################################
    #
    async def do_uidl(self, args: str) -> bool:
        """UIDL [msg]: unique ID listing using IMAP UIDs."""
        if args:
            n = self._valid_msg_num(args)
            if n is None:
                await self.client.push("-ERR no such message\r\n")
                return True
            uid = self.snapshot_uids[n - 1]
            await self.client.push(f"+OK {n} {uid}\r\n")
        else:
            lines = ["+OK\r\n"]
            for num in range(1, self.msg_count + 1):
                if num not in self.deleted:
                    uid = self.snapshot_uids[num - 1]
                    lines.append(f"{num} {uid}\r\n")
            lines.append(".\r\n")
            await self.client.push("".join(lines))
        return True

    ##################################################################
    #
    async def do_top(self, args: str) -> bool:
        """TOP msg n: headers plus first n lines of body."""
        parts = args.split()
        if len(parts) != 2:
            await self.client.push("-ERR usage: TOP msg n\r\n")
            return True

        n = self._valid_msg_num(parts[0])
        if n is None:
            await self.client.push("-ERR no such message\r\n")
            return True

        try:
            num_lines = int(parts[1])
        except ValueError:
            await self.client.push("-ERR invalid number of lines\r\n")
            return True

        if num_lines < 0:
            await self.client.push("-ERR invalid number of lines\r\n")
            return True

        assert self.mbox is not None
        msg_key = self.snapshot_msg_keys[n - 1]
        try:
            msg = self.mbox.get_msg(msg_key)
        except (KeyError, FileNotFoundError):
            await self.client.push("-ERR message not available\r\n")
            return True

        headers = msg_headers_as_bytes(msg)
        body = msg_as_bytes(msg, render_headers=False)

        # Body starts with a blank line separator; split into lines and
        # take only the first num_lines.
        #
        body_lines = body.split(b"\r\n")

        # Skip leading blank line if present (header/body separator).
        #
        if body_lines and body_lines[0] == b"":
            body_lines = body_lines[1:]

        truncated_body = b"\r\n".join(body_lines[:num_lines])
        result = headers + b"\r\n" + truncated_body
        result = dot_stuff(result)
        await self.client.push(b"+OK\r\n" + result + b"\r\n.\r\n")
        return True

    ##################################################################
    #
    async def do_noop(self, args: str) -> bool:
        """NOOP: no-op."""
        await self.client.push("+OK\r\n")
        return True

    ##################################################################
    #
    async def do_rset(self, args: str) -> bool:
        """RSET: clear all deletion marks."""
        self.deleted.clear()
        await self.client.push("+OK\r\n")
        return True

    ##################################################################
    #
    async def do_capa(self, args: str) -> bool:
        """CAPA: list server capabilities (RFC 2449)."""
        lines = [
            "+OK Capability list follows\r\n",
            "USER\r\n",
            "UIDL\r\n",
            "TOP\r\n",
            "IMPLEMENTATION asimap\r\n",
            ".\r\n",
        ]
        await self.client.push("".join(lines))
        return True
