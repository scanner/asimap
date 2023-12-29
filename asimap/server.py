"""
This is the heart of the main server. This is what accepts incoming
network connections, authenticates users, spawns userserver's, and
relays IMAP messages between an IMAP client and a userserver.
"""
# system imports
#
import asyncio
import logging
import os
import random
import re
import socket
import ssl
import string
import time
from typing import Dict, List, Optional, Union

# 3rd party imports
#
import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration

# asimap imports
#
import asimap.user_server

from .auth import User
from .client import CAPABILITIES, PreAuthenticated
from .parse import BadCommand, parse_cmd_from_msg

logger = logging.getLogger("asimap.server")

BACKLOG = 5

# In the IMAP protocol we get messages that are "literal strings" telling us
# how long they are. These are indicated by:
#
#    `{` <decimal ascii digits> +? `}<crlf>`
#
# The "+" (went sent from the client to server) indicate that the client does
# not need to wait for permission to go ahead to send the contents of the
# string itself. This regexp is for matching these literal string declarations.
#
RE_LITERAL_STRING_START = re.compile(rb"\{(\d+)(\+)?\}$")

# This dict is all of the subprocesses that we have created. One for each
# authenticated user with at least one active connection.
#
# The key is the username. The value is an IMAPSubprocess
#
USER_IMAP_SUBPROCESSES: Dict[str, "IMAPSubprocess"] = {}


##################################################################
##################################################################
#
#
class IMAPSubprocess:
    """
    This is a handle to asyncio.subprocess.Process instance, the localhost
    port that instance is listening on, and how many local clients are using a
    reference to this subprocess.

    When an IMAP client connects to the server and authenticates if there is no
    subprocess for the user that the IMAP client authenticated as we create an
    instance of this class (which in turn creates a subprocess.)

    This sets the count of clients referring to this object to 1.

    Every new IMAP client that comes along that authenticates as the same user
    for which a subprocess exists will increment this count.

    When the count reaches 0 (by various IMAP clients logging out) the
    subprocess will be shutdown.

    When a subprocess starts up it will listen on a port on 'localhost'. It
    will then tell us (the IMAPSubprocessHandle object) that port. This port is
    what is used by other parts of the server to talk to the subprocess.
    """

    ##################################################################
    #
    def __init__(
        self, user: User, debug: bool = False, log_config: Optional[str] = None
    ):
        """

        Arguments:
        - `user`: The user that has authenticated to us and thus represents
                  the unique identifier for the subprocess we handle. The user
                  is passed to the subprocess so that it can look up which unix
                  user to switch to for handling that user's mailbox.
        """
        self.log_config = log_config
        self.debug = debug
        self.trace_enabled = False
        self.trace_file = None
        self.user = user
        self.is_alive = False
        self.port: int
        self.subprocess: asyncio.subprocess.Process
        self.subprocess_key: bytes
        self.wait_task: Optional[asyncio.Task] = None
        self.rc: int

    ##################################################################
    #
    async def start(self):
        """
        Start our subprocess. This assumes that we have no subprocess
        already. If we do then we will be basically creating an orphan process.
        """
        cmd = asimap.user_server.USER_SERVER_PROGRAM
        args = []
        if self.log_config:
            args.append(f"--log-config={self.log_config}")
        if self.debug:
            args.append("--debug")
        if self.trace_enabled:
            args.append("--trace")
        if self.trace_file:
            args.append(f"--trace_file={self.options.trace_file}")
        args.append(self.user.username)

        logger.info(
            f"Starting user server, cmd: '{cmd} {' '.join(args)}', in "
            f"directory '{self.user.maildir}'"
        )
        self.subprocess = await asyncio.create_subprocess_exec(
            cmd,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            close_fds=True,
            cwd=self.user.maildir,
        )

        self.is_alive = True
        # Start a task that waits on the subprocess and cleans up after it
        # terminates.
        #
        self.wait_task = asyncio.create_task(self.subprocess_wait())
        self.wait_task.add_done_callback(self.subprocess_wait_done)

        if self.subprocess.stdin is None:
            raise RuntimeError("unable to connect to subprocess stdin")
        if self.subprocess.stdout is None:
            raise RuntimeError("unable to connect to subprocess stdout")

        # We expect the subprocess to send back to us over its stdout a single
        # line which has the port it is listening on.
        #
        sk = "".join(
            random.SystemRandom().choice(string.ascii_uppercase + string.digits)
            for _ in range(32)
        )
        self.subprocess_key = bytes(sk, "latin-1")
        self.subprocess.stdin.write(self.subprocess_key + b"\n")
        await self.subprocess.stdin.drain()
        self.subprocess.stdin.close()
        await self.subprocess.stdin.wait_closed()
        logger.debug("Reading port from subprocess.")
        try:
            m = await self.subprocess.stdout.readline()
            self.port = int(str(m, "latin-1").strip())
        except ValueError as e:
            logger.exception(
                "Unable to read port definition from subprocess: %s", e
            )
            # XXX Uh.. what do we do here? basically the subprocess start
            # failed and we need to tell our caller so they can deal with it.
            #
            raise
        logger.debug("Subprocess is listening on port: %d" % self.port)

    ####################################################################
    #
    def subprocess_wait_done(self, task: asyncio.Task):
        """
        When the `subprocess_wait` task is done, remove the reference to it
        so it can be gc'd.
        """
        self.wait_task = None

    ####################################################################
    #
    async def subprocess_wait(self):
        """
        A task that waits for the subprocess to exist and sets a flag when
        that happens.
        """
        rc = await self.subprocess.wait()
        self.rc = rc
        self.is_alive = False
        if self.rc != 0:
            logger.warning("Subprocess had non-zero return code: %d" % self.rc)

    ####################################################################
    #
    def terminate(self):
        """
        Terminate the subprocess.
        """
        if self.subprocess:
            self.subprocess.terminate()


########################################################################
########################################################################
#
class IMAPServer:
    """
    The IMAPServer dispatcher. This really just listens for TCP
    connections from IMAP Clients and when we accept one we hand it
    off to an IMAPClient to deal with.
    """

    ####################################################################
    #
    def __init__(
        self,
        address: str,
        port: int,
        ssl_context: ssl.SSLContext,
        trace: Optional[str] = None,
        log_config: Optional[str] = None,
        debug: bool = False,
    ):
        self.address = address
        self.port = port
        self.ssl_context = ssl_context
        self.trace = trace
        self.log_config = log_config
        self.debug = debug
        self.asyncio_server: asyncio.Server
        self.imap_client_tasks: Dict[asyncio.Task, IMAPClient] = {}

    ####################################################################
    #
    async def run(self):
        """
        Create and start the asyncio server to handle IMAP clients. Run
        until server exits.
        """
        if "SENTRY_DSN" in os.environ:
            sentry_sdk.init(
                dsn=os.environ["SENTRY_DSN"],
                # Set traces_sample_rate to 1.0 to capture 100%
                # of transactions for performance monitoring.
                traces_sample_rate=1.0,
                profiles_sample_rate=1.0,
                integrations=[
                    AsyncioIntegration(),
                ],
                environment="devel",
            )

        self.asyncio_server = await asyncio.start_server(
            self.new_client, self.address, self.port, ssl=self.ssl_context
        )
        addrs = ", ".join(
            str(sock.getsockname()) for sock in self.asyncio_server.sockets
        )
        logger.debug("Serving on %s", addrs)
        try:
            async with self.asyncio_server:
                await self.asyncio_server.serve_forever()
        except asyncio.exceptions.CancelledError as exc:
            logger.info("IMAP Server main loop cancelled %s", exc)
        except Exception as exc:
            logger.exception("IMAP Server exited with exception: %s", exc)
            raise
        finally:
            # Close any open clients, and await those tasks completing.
            #
            # XXX Make this in to a method we can call as the list of things to
            #     do when shutting down the server will likely grow and we need
            #     to catch exceptions.
            #
            logger.debug("IMAP Server exiting for some reason")
            clients = [c.close() for c in self.imap_client_tasks.values()]
            tasks = [task for task in self.imap_client_tasks.keys()]
            await asyncio.gather(*clients, return_exceptions=True)
            await asyncio.gather(*tasks, return_exceptions=True)
            for subp in USER_IMAP_SUBPROCESSES.values():
                if subp.is_alive:
                    subp.terminate()

    ####################################################################
    #
    def new_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        """
        New client connection. Create a new IMAPClient
        with the reader and writer. Create a new task to handle all
        future communications with the new client.
        """
        rem_addr, port = writer.get_extra_info("peername")
        peer_name = f"{rem_addr}:{port}"
        logger.debug(f"New client: {peer_name}")
        client_handler = IMAPClient(
            self, peer_name, rem_addr, port, reader, writer
        )
        task = asyncio.create_task(client_handler.start(), name=peer_name)
        task.add_done_callback(self.client_done)
        self.imap_client_tasks[task] = client_handler

    ####################################################################
    #
    def client_done(self, task):
        """
        When the asyncio task represented by the IMAPClient has
        exited this call back is invoked.

        Remove the task from the server's dict of IMAPClient tasks.
        """
        if task in self.imap_client_tasks:
            del self.imap_client_tasks[task]

        task_name = task.get_name()
        logger.debug(f"{task_name}: IMAP Client task done")


########################################################################
########################################################################
#
class IMAPClient:
    """
    This class is a communication channel to an IMAP client.

    This class and the IMAPSubprocessInterface form the two parts of
    communictation between an IMAP client and the subprocess running as a user
    handling all of that IMAP client's messages.

    A handler for a connection with an IMAP client.

    This will suck in messages from the client, sending back continuation
    strings so that it gets an entire message.

    When an entire message has been received we pass it off to a
    IMAPSubprocessInterface to deal with.

    That AyncServerIMAPMessageProcessor will call our '.push()' method to send
    messages back to the IMAP client.

    NOTE: The `start()` method is used to create a new asyncio task.
    """

    LINE_TERMINATOR = b"\r\n"

    ####################################################################
    #
    def __init__(
        self,
        imap_server: IMAPServer,
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
        self.imap_server = imap_server
        self.debug = imap_server.debug
        self.trace_file = None
        self.done = False

        self.reading_string_literal = False
        self.stream_buffer_size = 65536
        self.ibuffer: List[bytes] = []
        self.subprocess_intf = IMAPSubprocessInterface(self)

    ####################################################################
    #
    def __str__(self):
        return f"{type(self)}:{self.name}"

    ####################################################################
    #
    async def push(self, *data: Union[bytes, str]):
        """
        Write data to the IMAP Client. Also write it to the trace
        file if we have one.
        """
        for d in data:
            if isinstance(d, str):
                d = bytes(d, "latin-1")
            self.writer.write(d)
        await self.writer.drain()

        if self.trace_file:
            msg = [
                str(d, "latin-1") if isinstance(d, bytes) else d for d in data
            ]
            await self.trace_file(
                {
                    "time": time.time(),
                    "data": "".join(msg),
                    "msg_type": "SEND",
                }
            )

    ####################################################################
    #
    async def start(self):
        """
        Entry point for the asyncio task for handling the network
        connection from an IMAP client.

        We read complete messages from the IMAP Client and once we
        have one we create a new asyncio task to handle it.

        XXX This needs to handle all exceptions since it is the root
            of an asyncio task.
        """
        msg: bytes
        try:
            capabilities = " ".join(CAPABILITIES)
            await self.push(f"* OK [CAPABILITY {capabilities}]\r\n")
            self.ibuffer = []
            client_connected = True
            while client_connected:
                # Read until b'\r\n'. Trim off the '\r\n'. If the message is
                # not of 0 length then append it to our incremental buffer.
                #
                msg = await self.reader.readuntil(self.LINE_TERMINATOR)
                msg = msg[:-2]
                if msg:
                    self.ibuffer.append(msg)

                # If after reading up to a line terminator our incremental
                # buffer is empty then this is an empty message from the client
                # and that is an error.
                #
                if not self.ibuffer:
                    await self.push(
                        b"* BAD We do not accept empty messages.\r\n"
                    )
                    continue

                # Check to see if `msg` ends with a string literal declaration
                #
                m = RE_LITERAL_STRING_START.search(msg)
                if m:
                    literal_str_length = int(m.group(1))

                    # If this is a synchronizing string literal (does not have
                    # '+' as the second to last character in its length prefix)
                    # we need to tell the IMAP client that it can proceed to
                    # send us the string literal.
                    #
                    if not m.group(2):
                        await self.push(b"+ Ready for more input\r\n")

                    # Read the string literal.
                    #
                    msg = await self.reader.readexactly(literal_str_length)
                    self.ibuffer.append(msg)

                    # Loop back to read what is either a b'\r\n' or maybe
                    # another string literal.
                    #
                    continue

                # We only get here if we have read the complete message from
                # the IMAP Client.
                #
                # Send the fully received message from the IMAP Client to the
                # message processor. If the IMAP Client has properly
                # authenticated then the message is sent to a subprocess to
                # work on. Otherwise, we process the IMAP command locally and
                # send a response back.
                #
                msg = b"".join(self.ibuffer)
                self.ibuffer = []
                client_connected = await self.subprocess_intf.message(msg)

        except asyncio.exceptions.IncompleteReadError:
            # We got an EOF while waiting for a line terminator. Client
            # disconnecrted and we do not really care.
            #
            pass
        except Exception as exc:
            logger.exception("Exception in %s: %s", self, exc)
        finally:
            # We get here when we are no longer supposed to be connected to the
            # client. Close our connection and return which will cause this
            # task to be completed.
            #
            await self.close()

    ####################################################################
    #
    async def close(self):
        """
        Close our streams to the client. This may happen after something
        else has failed so swallow any exceptions we get while closing it (but
        do log them as errors)
        """
        try:
            if not self.writer.is_closing():
                self.writer.close()
            await self.writer.wait_closed()
        except socket.error:
            pass
        except Exception as exc:
            logger.error("Exception when closing %s: %s", self, exc)


########################################################################
########################################################################
#
class IMAPSubprocessInterface:
    """
    This class is the communication channel to the subprocess that handles all
    of a specific IMAP client's messages.

    This class and the IMAPClient form the two parts of
    communictation between an IMAP client and the subprocess running
    as a user handling all of that IMAP client's messages.

    This class is given full IMAP messages from the IMAP client.

    IMAP messages are passed to us to pass to the subprocess by calling the
    'message()' method.

    When we get a full message we either:

    1) hand the message to an instance of the PreAuthenticated class.
    2) send it to a subprocess to handle and respond to.

    The PreAuthenticated class is an IMAP message processor that understands
    the IMAP commands from the IMAP client that all involve the
    'before-authentication' steps.

    Once a client has successfully authenticated with the server we connect to
    a subprocess that is running as that user and send all further messages
    from the client to that subprocess to handle.

    When that subprocess disconnects we move back in to the
    'before-authentication' state (or if the subprocess crashed, we disconnect
    from the client.)
    """

    ####################################################################
    #
    def __init__(self, imap_client: IMAPClient):
        self.imap_client = imap_client
        self.peername = self.imap_client.writer.get_extra_info("peername")
        self.debug = imap_client.debug

        # The IMAP message processor that handles all of the IMAP commands
        # from the client when we are in the not-authenticated state.
        #
        self.client_handler = PreAuthenticated(self.imap_client)
        self.subprocess: IMAPSubprocess
        self.writer: Optional[asyncio.StreamWriter] = None
        self.reader: asyncio.StreamReader
        self.wait_task: Optional[asyncio.Task] = None

    ##################################################################
    #
    def log_string(self):
        """
        A bit of DRY: returns a string with common information that we like to
        have in our log messages.
        """
        if self.client_handler.user:
            return f"{self.client_handler.user} from {self.peername}"
        else:
            return f"unauthenticated from {self.peername}"

    ####################################################################
    #
    async def close(self):
        """
        Close our connection to the subprocess.
        """
        try:
            if self.writer:
                if not self.writer.is_closing():
                    self.writer.close()
                await self.writer.wait_closed()
        except socket.error:
            pass
        except Exception as exc:
            logger.error("Exception when closing %s: %s", self, exc)

    ####################################################################
    #
    async def push(self, *data: Union[bytes, str]):
        """
        Write data to IMAP Subprocess.
        """
        if not self.writer:
            return
        for d in data:
            if isinstance(d, str):
                d = bytes(d, "latin-1")
            self.writer.write(d)
        await self.writer.drain()

    ##################################################################
    #
    async def message(self, msg: bytes) -> bool:
        """
        Handle an IMAP message from an IMAP client.

        If the client is NOT authenticated then we parse this message and hand
        it to a local IMAP message processor to deal with.

        If the client IS authenticated then we send it on to the subprocess
        that is dealing with the user's actual mail spool.

        Arguments: - `msg`: A full IMAP message from an IMAP client

        Return : bool
        """
        # If the IMAP client is authenticated then we can just push the IMAP
        # messages off to the subprocess to handle.
        #
        if self.client_handler.state == "authenticated":
            await self.push("{%d}\n" % len(msg), msg)
            return True

        # NOTE: Below this section ONLY is reached before a user authenticates.
        #       This handles the IMAP commands before authentication.
        #
        #
        # The user has not authenticated we need to locally parse the message
        # and deal with all of the IMAP protocol interactions required for a
        # user to authenticate...
        #
        return await self.unauthenticated(msg)

    ####################################################################
    #
    async def unauthenticated(self, msg: bytes) -> bool:
        """
        Handle messages for unauthenticated clients. All we can do are the
        IMAP commands availble before 'login'

        If we fail in such a way that we can not handle the IMAP client, or the
        IMAP client logs out, return `False` so that our calling layers know to
        disconnect the client.
        """
        try:
            imap_cmd = parse_cmd_from_msg(msg)
        except BadCommand as e:
            if imap_cmd.tag is not None:
                bad = f"{imap_cmd.tag} BAD {e}\r\n"
            else:
                bad = f"* BAD {e}\r\n"
            await self.imap_client.push(bad)
            return True

        # Process this IMAP command (dealing with all valid commands before
        # authenticated.)
        #
        try:
            await self.client_handler.command(imap_cmd)
        except Exception as e:
            logger.exception(
                "Exception handling IMAP command %s for %s: %s"
                % (imap_cmd, self.log_string(), str(e))
            )
            try:
                m = f"* BAD Internal error processing command {imap_cmd}\r\n"
                await self.imap_client.push(m)
            except Exception:
                pass
            return False

        # If we are authenticated after processing the IMAP command then we
        # need to connect to the subprocess for the authenticated user. This
        # may launch the subprocess if this is the first connection.
        #
        match self.client_handler.state:
            case "authenticated":
                try:
                    await self.get_and_connect_subprocess(
                        self.client_handler.user
                    )
                except Exception as e:
                    logger.exception(
                        "Exception starting subprocess for %s: "
                        "%s" % (self.log_string(), str(e))
                    )
                    await self.imap_client.push(msg)
                    if self.writer:
                        self.writer.close()
                        await self.writer.wait_closed()
                        self.writer = None
                    return False

            case "logged_out":
                if self.writer:
                    await self.close()
                    self.writer = None
                return False
        return True

    ##################################################################
    #
    async def get_and_connect_subprocess(self, user):
        """
        At this point the IMAP client has authenticated to us and we know what
        user they authenticated as. We need to see if there is an existing
        subprocess for this user.

        If there is none then we create one.

        After we have a handle on the subprocess create a TCP connection to
        that subprocess. This TCP connection is how this specific IMAP Client
        will communicate with the subprocess (and in the subprocess it will
        know which IMAP client is sending it commands based on which TCP
        connection the command comes in on)
        """
        if user.username in USER_IMAP_SUBPROCESSES:
            self.subprocess = USER_IMAP_SUBPROCESSES[user.username]
        else:
            self.subprocess = IMAPSubprocess(user, debug=self.debug)
            USER_IMAP_SUBPROCESSES[user.username] = self.subprocess

        if not self.subprocess.is_alive:
            await self.subprocess.start()

        # And initiate a connection to the subprocess.
        #
        logger.debug("connecting to subprocess on %d", self.subprocess.port)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            self.subprocess.port,
        )
        self.reader = reader
        self.writer = writer

        # # We have an authentication key for talking to this subprocess. The
        # # first message we send to the subprocess is that authentication key
        # # and we expect to get back the string "accepted\n"
        # #
        # # We should also take this opportunity to write the remote address
        # # and socket for this client.
        # #
        # writer.write(self.subprocess.subprocess_key + b"\n")
        # await writer.drain()
        # result = await reader.readline()
        # assert result and result == b"accepted"

        # Now start a task that will listen for data from the subprocess and
        # sent it on to the IMAP Client.
        #
        self.wait_task = asyncio.create_task(self.msgs_to_client())
        self.wait_task.add_done_callback(self.msgs_to_client_done)

    ####################################################################
    #
    async def msgs_to_client(self):
        """
        Listen for messages from the subprocess on `self.reader`. When we
        get them send them on to the IMAP client.
        """
        try:
            while True:
                if self.reader.at_eof():
                    break
                msg = await self.reader.readuntil(b"\r\n")
                await self.imap_client.push(msg)
        except asyncio.IncompleteReadError:
            pass
        finally:
            # either the connection to the subprocess was closed or the
            # connection to the IMAP client was closed. In either case attempt
            # to shutdown both connections.
            #
            await self.close()
            await self.imap_client.close()

    ####################################################################
    #
    def msgs_to_client_done(self, task: asyncio.Task):
        """
        Our task for listening for messages from the subprocess has
        finished.
        """
        logger.debug(
            "msgs_to_client task: either IMAP client or closed connection"
        )
        self.client_handler.state = "not_authenticated"
        self.wait_task = None
