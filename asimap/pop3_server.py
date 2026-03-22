#!/usr/bin/env python
#
"""
POP3 server for the root asimapd process.

This module handles POP3 client connections at the root server level.
It authenticates users via USER/PASS, then routes commands to the
per-user subprocess (the same subprocess used by IMAP connections).

Structurally parallel to server.py (IMAPServer/IMAPClient/
IMAPSubprocessInterface).
"""

# system imports
#
import asyncio
import logging
import ssl
from typing import TYPE_CHECKING, Optional

# Project imports
#
from .auth import BadAuthentication, NoSuchUser, PWUser, authenticate
from .pop3_parse import BadPOP3Command, parse_pop3_command
from .server import (
    USER_IMAP_SUBPROCESSES,
    USER_IMAP_SUBPROCESSES_LOCK,
    IMAPSubprocess,
)
from .throttle import check_allow, login_failed

if TYPE_CHECKING:
    from _typeshed import StrPath

logger = logging.getLogger("asimap.pop3_server")

BACKLOG = 5


########################################################################
########################################################################
#
class POP3Server:
    """
    The POP3 server dispatcher. Listens for POP3S connections and hands
    them off to POP3Client instances.
    """

    ####################################################################
    #
    def __init__(
        self,
        address: str,
        port: int,
        ssl_context: ssl.SSLContext,
        trace: bool | None = False,
        trace_dir: Optional["StrPath"] = None,
        log_config: str | None = None,
        debug: bool = False,
    ):
        self.address = address
        self.port = port
        self.ssl_context = ssl_context
        self.trace = trace
        self.trace_dir = trace_dir
        self.log_config = log_config
        self.debug = debug
        self.asyncio_server: asyncio.Server
        self.pop3_client_tasks: dict[asyncio.Task, POP3Client] = {}

    ####################################################################
    #
    async def run(self) -> None:
        """
        Create and start the asyncio server to handle POP3 clients.
        """
        self.asyncio_server = await asyncio.start_server(
            self.new_client,
            self.address,
            self.port,
            ssl=self.ssl_context,
        )
        try:
            async with self.asyncio_server:
                await self.asyncio_server.serve_forever()
        except asyncio.exceptions.CancelledError as exc:
            logger.info("POP3 Server main loop cancelled %s", exc)
        except Exception as exc:
            logger.exception("POP3 Server exited with exception: %s", exc)
            raise
        finally:
            logger.debug("POP3 Server exiting")
            clients = [c.close() for c in self.pop3_client_tasks.values()]
            tasks = list(self.pop3_client_tasks.keys())
            await asyncio.gather(*clients, return_exceptions=True)
            await asyncio.gather(*tasks, return_exceptions=True)

    ####################################################################
    #
    def new_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """New POP3 client connection."""
        rem_addr, port = writer.get_extra_info("peername")
        peer_name = f"{rem_addr}:{port}"
        client_handler = POP3Client(
            self, peer_name, rem_addr, port, reader, writer
        )
        task = asyncio.create_task(
            client_handler.start(),
            name=f"pop3_client_handler({peer_name})",
        )
        task.add_done_callback(self.client_done)
        self.pop3_client_tasks[task] = client_handler
        logger.debug(
            "New POP3 client: %s, number of clients: %d",
            peer_name,
            len(self.pop3_client_tasks),
        )

    ####################################################################
    #
    def client_done(self, task: asyncio.Task) -> None:
        """Remove completed POP3 client task."""
        if task in self.pop3_client_tasks:
            del self.pop3_client_tasks[task]

        task_name = task.get_name()
        logger.info(
            "%s: POP3 Client task done, number of clients: %d",
            task_name,
            len(self.pop3_client_tasks),
        )


########################################################################
########################################################################
#
class POP3Client:
    """
    Handles network I/O with a single POP3 client at the root server
    level.

    POP3 is line-oriented (CRLF terminated), with no literal strings
    unlike IMAP. This simplifies parsing significantly.
    """

    LINE_TERMINATOR = b"\r\n"

    ####################################################################
    #
    def __init__(
        self,
        pop3_server: POP3Server,
        name: str,
        rem_addr: str,
        port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        self.name = name
        self.rem_addr = rem_addr
        self.port = port
        self.reader = reader
        self.writer = writer
        self.pop3_server = pop3_server
        self.debug = pop3_server.debug
        self.subprocess_intf = POP3SubprocessInterface(self)

    ####################################################################
    #
    def __str__(self) -> str:
        return f"{type(self)}:{self.name}"

    ####################################################################
    #
    async def push(self, *data: bytes | str) -> None:
        """Write data to the POP3 client."""
        for d in data:
            if isinstance(d, str):
                d = bytes(d, "latin-1")
            self.writer.write(d)
        await self.writer.drain()

    ####################################################################
    #
    async def start(self) -> None:
        """
        Entry point for the asyncio task handling a POP3 client.

        Sends the greeting, reads CRLF-terminated command lines, and
        passes them to the POP3SubprocessInterface.
        """
        try:
            await self.push("+OK asimap POP3 server ready\r\n")
            client_connected = True
            while client_connected:
                msg = await self.reader.readuntil(self.LINE_TERMINATOR)
                msg = msg.rstrip()
                if not msg:
                    await self.push("-ERR empty command\r\n")
                    continue
                client_connected = await self.subprocess_intf.message(msg)

        except (
            OSError,
            asyncio.exceptions.IncompleteReadError,
            ConnectionResetError,
            ssl.SSLError,
        ):
            pass
        except Exception as exc:
            logger.exception("Exception in %s: %s", self, exc)
        finally:
            if self.subprocess_intf.wait_task:
                self.subprocess_intf.wait_task.cancel()
                try:
                    await self.subprocess_intf.wait_task
                except asyncio.CancelledError:
                    pass
            await self.close()

    ####################################################################
    #
    async def close(self) -> None:
        """Close the POP3 client connection."""
        try:
            if not self.writer.is_closing():
                self.writer.close()
            await self.writer.wait_closed()
        except OSError:
            pass
        except Exception as exc:
            logger.error("Exception when closing %s: %s", self, exc)


########################################################################
########################################################################
#
class POP3SubprocessInterface:
    """
    Routes POP3 commands between the POP3 client and the user
    subprocess.

    In AUTHORIZATION state, handles USER/PASS/CAPA/QUIT locally.
    After authentication, forwards commands to the subprocess and
    relays responses back.
    """

    ####################################################################
    #
    def __init__(self, pop3_client: POP3Client):
        self.pop3_client = pop3_client
        self.peername = self.pop3_client.writer.get_extra_info("peername")
        self.debug = pop3_client.debug

        # POP3 AUTHORIZATION state
        #
        self.state = "authorization"
        self.username: str | None = None

        # Subprocess connection
        #
        self.subprocess: IMAPSubprocess | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.reader: asyncio.StreamReader | None = None
        self.wait_task: asyncio.Task | None = None

    ##################################################################
    #
    def log_string(self) -> str:
        """Common log info string."""
        if self.username:
            return f"pop3:{self.username} from {self.peername}"
        return f"pop3:unauthenticated from {self.peername}"

    ####################################################################
    #
    async def close(self) -> None:
        """Close the subprocess connection."""
        try:
            if self.writer:
                if not self.writer.is_closing():
                    self.writer.close()
                await self.writer.wait_closed()
        except OSError:
            pass
        except Exception as exc:
            logger.error("Exception when closing %s: %s", self, exc)

    ####################################################################
    #
    async def push_to_subprocess(self, *data: bytes | str) -> None:
        """Write data to the subprocess using length-framed protocol."""
        if not self.writer:
            return
        for d in data:
            if isinstance(d, str):
                d = bytes(d, "latin-1")
            self.writer.write(d)
        await self.writer.drain()

    ####################################################################
    #
    async def message(self, msg: bytes) -> bool:
        """
        Handle a POP3 command from the client.

        In AUTHORIZATION state, handle locally. In TRANSACTION state,
        forward to the subprocess.
        """
        if self.state == "transaction":
            # Forward the command to the subprocess.
            #
            await self.push_to_subprocess(f"{{{len(msg)}}}\n", msg)
            return True

        # AUTHORIZATION state: handle USER, PASS, CAPA, QUIT.
        #
        return await self.handle_authorization(msg)

    ####################################################################
    #
    async def handle_authorization(self, msg: bytes) -> bool:
        """Handle POP3 commands in the AUTHORIZATION state."""
        try:
            pop3_cmd = parse_pop3_command(msg)
        except BadPOP3Command as e:
            await self.pop3_client.push(f"-ERR {e.value}\r\n")
            return True

        cmd = pop3_cmd.command

        if cmd == "USER":
            if not pop3_cmd.args:
                await self.pop3_client.push("-ERR missing username\r\n")
                return True
            self.username = pop3_cmd.args
            await self.pop3_client.push("+OK\r\n")
            return True

        elif cmd == "PASS":
            if not self.username:
                await self.pop3_client.push(
                    "-ERR USER command must come first\r\n"
                )
                return True
            if not pop3_cmd.args:
                await self.pop3_client.push("-ERR missing password\r\n")
                return True

            return await self._do_pass(pop3_cmd.args)

        elif cmd == "CAPA":
            lines = [
                "+OK Capability list follows\r\n",
                "USER\r\n",
                "UIDL\r\n",
                "TOP\r\n",
                "IMPLEMENTATION asimap\r\n",
                ".\r\n",
            ]
            await self.pop3_client.push("".join(lines))
            return True

        elif cmd == "QUIT":
            await self.pop3_client.push("+OK Bye\r\n")
            return False

        else:
            await self.pop3_client.push(
                f"-ERR command not valid in this state: {cmd}\r\n"
            )
            return True

    ####################################################################
    #
    async def _do_pass(self, password: str) -> bool:
        """
        Authenticate the user and connect to the subprocess.
        """
        assert self.username is not None
        remote_ip = self.pop3_client.rem_addr

        if not check_allow(self.username, remote_ip):
            await self.pop3_client.push(
                "-ERR too many failed attempts, try again later\r\n"
            )
            return False

        try:
            user = await authenticate(self.username, password)
        except (NoSuchUser, BadAuthentication):
            login_failed(self.username, remote_ip)
            await self.pop3_client.push("-ERR invalid username or password\r\n")
            return True

        # Verify that the user's maildir exists.
        #
        if not user.maildir.is_dir():
            logger.error(
                "POP3 login for '%s': maildir '%s' does not exist",
                self.username,
                user.maildir,
            )
            await self.pop3_client.push("-ERR mailbox not available\r\n")
            return False

        # Authentication succeeded. Connect to the subprocess.
        #
        try:
            await self.get_and_connect_subprocess(user)
        except Exception as e:
            logger.exception(
                "POP3 exception starting/connecting subprocess for %s: %s",
                self.log_string(),
                e,
            )
            await self.pop3_client.push("-ERR internal server error\r\n")
            if self.writer:
                self.writer.close()
                await self.writer.wait_closed()
                self.writer = None
            if self.wait_task:
                self.wait_task.cancel()
                self.wait_task = None
            return False

        self.state = "transaction"
        await self.pop3_client.push("+OK maildrop ready\r\n")
        return True

    ####################################################################
    #
    async def get_and_connect_subprocess(self, user: PWUser) -> None:
        """
        Get or create a subprocess for the authenticated user and
        connect to it.

        Reuses the same USER_IMAP_SUBPROCESSES dict and IMAPSubprocess
        class as IMAP connections.
        """
        async with USER_IMAP_SUBPROCESSES_LOCK.read_lock():
            async with USER_IMAP_SUBPROCESSES_LOCK.write_lock():
                if user.username in USER_IMAP_SUBPROCESSES:
                    self.subprocess = USER_IMAP_SUBPROCESSES[user.username]
                    logger.debug(
                        "POP3 %s, username '%s' reusing existing "
                        "subprocess: %s",
                        self.pop3_client.name,
                        user.username,
                        self.subprocess,
                    )
                else:
                    self.subprocess = IMAPSubprocess(
                        user,
                        debug=self.debug,
                        log_config=self.pop3_client.pop3_server.log_config,
                        trace=self.pop3_client.pop3_server.trace,
                        trace_dir=self.pop3_client.pop3_server.trace_dir,
                    )
                    USER_IMAP_SUBPROCESSES[user.username] = self.subprocess
                    logger.debug(
                        "POP3 %s, username '%s' creating new subprocess: %s",
                        self.pop3_client.name,
                        user.username,
                        self.subprocess,
                    )

                if not self.subprocess.is_alive:
                    await self.subprocess.start()

        # Wait for the subprocess to be ready (listening on its port).
        #
        async with asyncio.timeout(300):
            await self.subprocess.has_port.wait()

        # Connect to the subprocess.
        #
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", self.subprocess.port
        )

        # Send the POP3 protocol identifier as the first framed message.
        # The subprocess will detect this and create a POP3ClientProxy
        # instead of an IMAPClientProxy.
        #
        pop3_ident = b"POP3"
        self.writer.write(f"{{{len(pop3_ident)}}}\n".encode("latin-1"))
        self.writer.write(pop3_ident)
        await self.writer.drain()

        # Start a task to relay subprocess responses to the POP3 client.
        #
        self.wait_task = asyncio.create_task(
            self.msgs_to_client(),
            name=f"POP3SubprocessInterface({user.username},{self.peername})",
        )
        self.wait_task.add_done_callback(self.msgs_to_client_done)

    ####################################################################
    #
    async def msgs_to_client(self) -> None:
        """
        Relay messages from the subprocess to the POP3 client.

        The subprocess sends CRLF-terminated lines (same as IMAP).
        We read each line and forward it to the POP3 client.
        """
        try:
            while True:
                if self.reader is None or self.reader.at_eof():
                    break
                msg = await self.reader.readuntil(b"\r\n")
                await self.pop3_client.push(msg)
        except (OSError, asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except asyncio.LimitOverrunError as exc:
            logger.warning(
                "Hit limit overrun on reader from user subprocess for "
                "sending to POP3 Client: %s",
                exc,
            )
        except Exception:
            logger.exception(
                "error either reading or pushing message to POP3 client"
            )
        finally:
            await self.close()
            await self.pop3_client.close()

    ####################################################################
    #
    def msgs_to_client_done(self, task: asyncio.Task) -> None:
        """Subprocess relay task completed."""
        logger.debug("POP3 msgs_to_client task done for %s", self.log_string())
