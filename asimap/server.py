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

# from aiologger import Logger


logger = logging.getLogger("asimap.server")
# logger = Logger.with_default_handlers(name="asimap.server")

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
        self, user: User, debug: bool = False, logdir: Optional[str] = None
    ):
        """

        Arguments:
        - `user`: The user that has authenticated to us and thus represents
                  the unique identifier for the subprocess we handle. The user
                  is passed to the subprocess so that it can look up which unix
                  user to switch to for handling that user's mailbox.
        """
        if logdir is None:
            logdir = "stderr"
        self.debug = debug
        self.trace_enabled = False
        self.trace_file = None
        self.user = user
        self.logdir = logdir
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
        import q

        cmd = asimap.user_server.USER_SERVER_PROGRAM
        args = []
        if self.logdir:
            args.append(f"--logdir={self.logdir}")
        if self.debug:
            args.append("--debug")
        if self.trace_enabled:
            args.append("--trace")
        if self.trace_file:
            args.append(f"--trace_file={self.options.trace_file}")

        q.q(f"Starting server: {args}")
        print(f"Starting server: {args}")

        logger.error(
            "Starting user server, cmd: %s, as user: '%s', in "
            "directory '%s'"
            % (repr(cmd), self.user.username, self.user.maildir)
        )
        self.subprocess = await asyncio.create_subprocess_exec(
            cmd,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
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
        # XXX This is bad in that our server will block while waiting for this
        #     I wonder if we can use a new asynchat client to handle this
        #     communication.
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
            print(f"Got: {m}")
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


# ##################################################################
# ##################################################################
# #
# #
# class IMAPSubprocessHandle(object):
#     """
#     This is a handle to a multiprocess.Popen instance, the localhost port that
#     instance is listening on, and how many local clients are using a reference
#     to this subprocess.

#     When an IMAP client connects to the server and authenticates if there is no
#     subprocess for the user that the IMAP client authenticated as we create an
#     instance of this class (which in turn creates a subprocess.)

#     This sets the count of clients referring to this object to 1.

#     Every new IMAP client that comes along that authenticates as the same user
#     for which a subprocess exists will increment this count.

#     When the count reaches 0 (by various IMAP clients logging out) the
#     subprocess will be shutdown.

#     When a subprocess starts up it will listen on a port on 'localhost'. It
#     will then tell us (the IMAPSubprocessHandle object) that port. This port is
#     what is used by other parts of the server to talk to the subprocess.
#     """

#     ##################################################################
#     #
#     def __init__(self, user, options):
#         """

#         Arguments:
#         - `user`: The user that has authenticated to us and thus represents
#                   the unique identifier for the subprocess we handle. The user
#                   is passed to the subprocess so that it can look up which unix
#                   user to switch to for handling that user's mailbox.
#         """
#         self.options = options
#         self.user = user
#         self.port = None
#         self.subprocess = None
#         self.rc = None

#     ##################################################################
#     #
#     def start(self):
#         """
#         Start our subprocess. This assumes that we have no subprocess
#         already. If we do then we will be basically creating an orphan process.
#         """
#         cmd = [asimap.user_server.USER_SERVER_PROGRAM]
#         cmd.append(f"--logdir={self.options.logdir}")
#         if self.options.debug:
#             cmd.append("--debug")
#         if self.options.trace_enabled:
#             cmd.append("--trace")
#         if self.options.trace_file:
#             cmd.append(f"--trace_file={self.options.trace_file}")

#         logger.debug(
#             "Starting user server, cmd: %s, as user: '%s', in "
#             "directory '%s'"
#             % (repr(cmd), self.user.local_username, self.user.maildir)
#         )
#         self.subprocess = subprocess.Popen(
#             cmd,
#             preexec_fn=self.setuid_to_user,
#             close_fds=True,
#             cwd=self.user.maildir,
#             stdout=subprocess.PIPE,
#         )

#         # We expect the subprocess to send back to us over its stdout a single
#         # line which has the port it is listening on.
#         #
#         # XXX This is bad in that our server will block while waiting for this
#         #     I wonder if we can use a new asynchat client to handle this
#         #     communication.
#         #
#         logger.debug("Reading port from subprocess.")
#         try:
#             self.port = int(self.subprocess.stdout.read().strip())
#         except ValueError as e:
#             logger.exception(
#                 f"Unable to read port definition from subprocess: {e}"
#             )
#             # XXX Uh.. what do we do here? basically the subprocess start
#             # failed and we need to tell our caller so they can deal with it.
#             #
#             raise
#         logger.debug("Subprocess is listening on port: %d" % self.port)
#         return

#     ##################################################################
#     #
#     def setuid_to_user(self):
#         """
#         This is called as the pre-exec function for subprocess.Popen. It is
#         what changes the user from root (presumably) to the actual owner of the
#         mailbox we are going to be serving up.

#         We also close stdin, because the subprocess will not be reading
#         anything so we want to be tidy.
#         """
#         os.close(0)

#         # If we NOT running as root do not actually try to setuid (it would
#         # fail anyways.)
#         #
#         if os.getuid() == 0:
#             os.setuid(pwd.getpwnam(self.user.local_username)[2])
#         else:
#             p = pwd.getpwuid(os.getuid())
#             logger.info(
#                 "setuid_to_user: Not setting uid, we are running as "
#                 "'%s', uid: %d" % (p[0], p[2])
#             )
#         return

#     ##################################################################
#     #
#     def is_alive(self):
#         """
#         Calls the subprocess 'poll' method on our popen object to see if it is
#         still around. This needs to be called before any attempt to establish
#         communication with the subprocess. If it returns false then 'start()'
#         must be called before attempting to talk to the subprocess.
#         """
#         if self.subprocess is None:
#             return False

#         self.rc = self.subprocess.poll()
#         if self.rc is None:
#             return True

#         self.subprocess = None
#         if self.rc != 0:
#             logger.error("Subprocess had non-zero return code: %d" % self.rc)
#         return False


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
        debug: bool = False,
    ):
        self.address = address
        self.port = port
        self.ssl_context = ssl_context
        self.trace = trace
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
        except asyncio.exceptions.CancelledError:
            pass
        finally:
            # Close any open clients, and await those tasks completing.
            #
            # XXX Make this in to a method we can call as the list of things to
            #     do when shutting down the server will likely grow and we need
            #     to catch exceptions.
            #
            clients = [c.close() for c in self.imap_client_tasks.values()]
            tasks = [task for task in self.imap_client_tasks.keys()]
            await asyncio.gather(*clients, return_exceptions=True)
            await asyncio.gather(*tasks, return_exceptions=True)
            for subp in USER_IMAP_SUBPROCESSES.values():
                if subp.is_alive:
                    subp.terminate()
            await logger.shutdown()

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
        client_handler = IMAPClient(self, peer_name, reader, writer)
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
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        self.name = name
        self.reader = reader
        self.writer = writer
        self.imap_server = imap_server
        self.debug = imap_server.debug
        self.trace_file = None
        self.done = False

        self.reading_string_literal = False
        self.stream_buffer_size = 65536
        self.ibuffer: List[bytes] = []
        self.msg_processor = IMAPSubprocessInterface(self)

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
                client_connected = await self.msg_processor.message(msg)

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
        return f"{self.client_handler.user} from {self.peername}"

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
            await self.push(f"{len(msg)}\n", msg)
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
                    self.writer.close()
                    await self.writer.wait_closed()
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
        (reader, writer) = await asyncio.open_connection(
            "127.0.0.1",
            self.subprocess.port,
            family=socket.AF_INET,
            proto=socket.SOCK_STREAM,
        )
        self.reader = reader
        self.writer = writer

        # We have an authentication key for talking to this subprocess. The
        # first message we send to the subprocess is that authentication key
        # and we expect to get back the string "accepted\n"
        #
        writer.write(self.subprocess.subprocess_key)
        await writer.drain()
        result = await reader.readline()
        assert result and result == b"accepted"

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
                msg = self.reader.readuntil(b"\r\n")
                await self.imap_client.push(msg)
        except asyncio.IncompleteReadError as exc:
            logger.error(
                "Reader connection to IMAPSubprocess %s failed: %s",
                self.subprocess,
                exc,
            )

    ####################################################################
    #
    def msgs_to_client_done(self, task: asyncio.Task):
        """
        Our task for listening for messages from the subprocess has
        finished. Clear up its references.
        """
        self.wait_task = None


# ##################################################################
# ##################################################################
# #
# class ServerIMAPMessageProcessor(asynchat.async_chat):
#     """
#     This class is the communication channel to the subprocess that handles all
#     of a specific IMAP client's messages.

#     This class and the IMAPClientHandler form the two parts of communictation
#     between an IMAP client and the subprocess running as a user handling all
#     of that IMAP client's messages.

#     This class is given full IMAP messages from the IMAP client.

#     IMAP messages are passed to us to pass to the subprocess by calling the
#     'message()' method.

#     When we get a full message we either:

#     1) hand the message to an instance of the PreAuthenticated class.
#     2) send it to a subprocess to handle and respond to.

#     The PreAuthenticated class is an IMAP message processor that understands
#     the IMAP commands from the IMAP client that all involve the
#     'before-authentication' steps.

#     Once a client has successfully authenticated with the server we connect to
#     a subprocess that is running as that user and send all further messages
#     from the client to that subprocess to handle.

#     When that subprocess disconnects we move back in to the
#     'before-authentication' state (or if the subprocess crashed, we disconnect
#     from the client.)

#     """

#     ##################################################################
#     #
#     def __init__(self, client_connection):
#         """

#         Arguments:
#         - `client_connection`: An async_chat object that is a connect to the
#                                IMAP client. We can use its 'push()' method to
#                                send messages to the IMAP client.
#         - `options`: The configuration options
#         """
#         self.log = logging.getLogger(
#             "%s.%s" % (__name__, self.__class__.__name__)
#         )
#         asynchat.async_chat.__init__(self)

#         self.client_connection = client_connection
#         self.options = client_connection.options

#         # If we are in test-mode then use the 'simple_auth' auth system.  This
#         # basically has only one user, 'test', and its mailbox is in
#         # '/var/tmp/testmaildir'
#         #
#         if self.options.test_mode:
#             auth_system = "test_auth"
#         else:
#             auth_system = "simple_auth"

#         # The IMAP message processor that handles all of the IMAP commands
#         # from the client when we are in the not-authenticated state.
#         #
#         self.client_handler = PreAuthenticated(
#             self.client_connection, AUTH_SYSTEMS[auth_system]
#         )
#         self.subprocess = None

#         # We do not buffer and process data from the subprocess. As soon as we
#         # get it, we send it on to the IMAP client.
#         #
#         self.set_terminator(None)
#         return

#     ##################################################################
#     #
#     def log_string(self):
#         """
#         A bit of DRY: returns a string with common information that we like to
#         have in our log messages.
#         """
#         return "%s from %s:%d" % (
#             self.client_handler.user,
#             self.client_connection.rem_addr,
#             self.client_connection.port,
#         )

#     ##################################################################
#     #
#     def message(self, msg):
#         """
#         Handle an IMAP message from a client.

#         If the client is NOT authenticated then we parse this message and hand
#         it to a local IMAP message processor to deal with.

#         If the client IS authenticated then we send it on to the subprocess
#         that is dealing with the user's actual mail spool.

#         Arguments: - `msg`: A full IMAP message from an IMAP client
#         """

#         # If the IMAP client is authenticated then we can just push the IMAP
#         # messages off to the subprocess to handle.
#         #
#         # XXX What are the failure modes here that we need to worry about?  Can
#         #     we push messages before the connection has actually been
#         #     established?
#         #
#         if self.client_handler.state == "authenticated":
#             self.push("%d\n" % len(msg))
#             self.push(msg)
#             return

#         # The user has not authenticated we need to locally parse the message
#         # and deal with all of the IMAP protocol interactions required for a
#         # user to authenticate...
#         #
#         try:
#             imap_cmd = asimap.parse.IMAPClientCommand(msg)
#             imap_cmd.parse()

#         except asimap.parse.BadCommand as e:
#             # The command we got from the client was bad...  If we at least
#             # managed to parse the TAG out of the command the client sent us we
#             # use that when sending our response to the client so it knows what
#             # message we had problems with.
#             #
#             if imap_cmd.tag is not None:
#                 msg = "%s BAD %s\r\n" % (imap_cmd.tag, str(e))
#             else:
#                 msg = "* BAD %s\r\n" % str(e)
#             self.client_connection.push(msg)
#             return

#         # This hands the IMAP command to be processed by the client handler
#         # (dealing with everything before the client is in the authenticated
#         # state.)
#         #
#         try:
#             self.client_handler.command(imap_cmd)
#         except Exception as e:
#             # We catch all exceptions because we do not want the server
#             # unceremoniously exiting.
#             #
#             # XXX However an exception making it to this level probably means
#             #     we should disconnect the client?
#             #
#             tb = traceback.format_exc()
#             self.logger.error(
#                 "Exception handling IMAP command %s(%s) for %s: "
#                 "%s\n%s"
#                 % (
#                     imap_cmd.command,
#                     imap_cmd.tag,
#                     self.log_string(),
#                     str(e),
#                     tb,
#                 )
#             )

#         # After processing that command see if we are in the authenticated or
#         # logged out state and take the appropriate action.
#         #
#         if self.client_handler.state == "authenticated":
#             # The client has authenticated to us.. connect a subprocess
#             # that will handle the client's messages from now until it
#             # logs out.
#             #
#             try:
#                 self.get_and_connect_subprocess(self.client_handler.user)
#             except Exception as e:
#                 # If we fail to launch our subprocess then tell the client we
#                 # had an internal error and log it for us to figure out what
#                 # went wrong.
#                 #
#                 tb = traceback.format_exc()
#                 self.logger.error(
#                     "Exception starting subprocess for %s: "
#                     "%s\n%s" % (self.log_string(), str(e), tb)
#                 )
#                 msg = "* BAD Internal error launching user mail spool\r\n"
#                 self.client_connection.push(msg)
#                 self.client_connection.close()
#                 if self.socket is not None:
#                     self.close()
#                 return
#         elif self.client_handler.state == "logged_out":
#             # The client has logged out. We need to close our connection to the
#             # subprocess if we have it, and close our connection to the
#             # client. Doing these two things should cause this object to be
#             # removed from the asyncore dispatcher loop.
#             #
#             if self.socket is not None:
#                 self.close()
#             self.client_connection.close()
#         return

#     ##################################################################
#     #
#     def collect_incoming_data(self, data):
#         """
#         We have received data from the subprocess handling the IMAP client's
#         messages.
#         """
#         if self.client_connection is not None:
#             self.client_connection.push(data)
#         return

#     ##################################################################
#     #
#     def get_and_connect_subprocess(self, user):
#         """
#         At this point the IMAP client has authenticated to us and we know what
#         user they authenticated as. We need to see if there is an existing
#         subprocess handler for this user.

#         If there is none then we create one.
#         """
#         if user.imap_username in user_imap_subprocesses:
#             self.subprocess = user_imap_subprocesses[user.imap_username]
#         else:
#             self.subprocess = IMAPSubprocessHandle(user, self.options)
#             user_imap_subprocesses[user.imap_username] = self.subprocess

#         if not self.subprocess.is_alive():
#             self.subprocess.start()

#         # And initiate a connection to the subprocess.
#         #
#         self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
#         self.connect(("127.0.0.1", self.subprocess.port))
#         return

#     ##################################################################
#     #
#     def handle_connect(self):
#         """
#         We have established a connection to the subprocess for this user.
#         Yay.
#         """
#         return

#     ##################################################################
#     #
#     def handle_close(self):
#         """
#         This gets called when the subprocess handling this user's mailspool has
#         disconnected from us.

#         This can happen for two reasons:

#         1) The IMAP client has gone through the process of de-authenticating
#            and they are no longer authenticated. The subprocess signals the
#            success of this IMAP protocol transaction by closing its TCP
#            connection to us.

#         2) However, this is also what will happen if the subprocess crashes for
#            some reason. Crashes happen. In this case we need to send some
#            signal back to the IMAP client so that it does not sit there
#            thinking it can send messages to us expecting us to be in some IMAP
#            state that we are not in. So, in this case we need to close our
#            connection to the IMAP client as if we had indeed crashed.

#            THe question is: how do we know that this is the case, not (1)?
#            Although this is not perfect what we do is see if the subprocess is
#            still alive, and if it is not, see if its exit code is non-zero.

#            If it is not alive and its exit code is non-zero then we know it
#            crashed.
#         """

#         self.client_handler.state = "non_authenticated"
#         self.logger.info(
#             "Connection with subprocess for %s has closed" % (self.log_string())
#         )
#         # See if the subprocess is alive.. if it is not then it ungraciously
#         # went away and we need to tell the IMAP client to go away too.
#         #
#         if self.subprocess.is_alive is False:
#             self.logger.error(
#                 "Our subprocess for %s went away unexpectedly with "
#                 "the exit code: %d" % (self.log_string, self.subprocess.rc)
#             )
#         if self.socket is not None:
#             self.close()

#         # Since we lost the connection to our subprocess close the
#         # connection to the IMAP client too.
#         #
#         self.client_connection.close()
#         self.client_connection = None
#         self.client_handler.user = None
#         self.client_handler = None
#         return

#     ##################################################################
#     #
#     def client_disconnected(self):
#         """
#         This is called when the IMAP client has disconnected from us.

#         We close our connection to the subprocess and do various cleanups.
#         """
#         self.logger.info(
#             "IMAP client for %s has disconnected" % self.log_string()
#         )
#         self.client_connection = None
#         self.client_handler.state = "non_authenticated"
#         self.client_handler.user = None
#         self.subprocess = None

#         # If we have a connection to the subprocess then close it too.
#         #
#         if self.socket is not None:
#             self.close()
#         return

# ##################################################################
# ##################################################################
# #
# class IMAPServer(asyncore.dispatcher):
#     """
#     The IMAPServer dispatcher. This really just listens for connections and
#     when we accept one we hand it off to an IMAPClientHandler to deal with.
#     """

#     ##################################################################
#     #
#     def __init__(self, interface, port, options, ssl_cert=None):
#         """
#         Setup our dispatcher.. listen on the port we are supposed to accept
#         connections on. When something connects to it create an
#         IMAPClientHandler and pass it the socket.

#         Arguments:
#         - `options` : The options set on the command line
#         """
#         self.log = logging.getLogger(
#             "%s.%s" % (__name__, self.__class__.__name__)
#         )
#         asyncore.dispatcher.__init__(self)

#         self.options = options
#         self.interface = interface
#         self.port = port
#         self.ssl_cert = ssl_cert
#         self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
#         self.set_reuse_addr()
#         self.bind((interface, port))
#         self.listen(BACKLOG)
#         self.logger.info(
#             "IMAP Server listening on %s:%d" % (self.interface, self.port)
#         )
#         return

#     ##################################################################
#     #
#     def handle_accept(self):
#         """
#         A client has connected to us. Create the IMAPClientHandler object to
#         handle that client and let it deal with it.
#         """

#         pair = self.accept()
#         if pair is not None:
#             sock, addr = pair
#             self.logger.info("Incoming connection from %s:%s" % addr)
#             try:
#                 # NOTE: The creation of the IMAPClientHandler object
#                 # registers it into the asyncore dispatch loop.
#                 #
#                 IMAPClientHandler(sock, addr, self.options, self.ssl_cert)
#             except ssl.SSLError as e:
#                 self.logger.error(
#                     "Error accepting connection from %s: %s" % (addr, str(e))
#                 )
#         return


# ##################################################################
# ##################################################################
# #
# class IMAPClientHandler(asynchat.async_chat):
#     """
#     This class is a communication channel to a specific IMAP client.

#     This class and the ServerIMAPMessageProcessor form the two parts of
#     communictation between an IMAP client and the subprocess running as a user
#     handling all of that IMAP client's messages.

#     A handler for a connection with an IMAP client.

#     This will suck in messages from the client, sending back continuation
#     strings so that it gets an entire message.

#     When an entire message has been received we pass it off to a
#     ServerIMAPMessageProcessor to deal with.

#     That ServerIMAPMessageProcessor will call our '.push()' method to send
#     messages back to the IMAP client.
#     """

#     LINE_TERMINATOR = b"\r\n"

#     ##################################################################
#     #
#     def __init__(self, sock, addr, options, ssl_cert=None, trace_file=None):
#         self.trace_file = trace_file
#         self.options = options
#         self.log = logging.getLogger(
#             "%s.%s" % (__name__, self.__class__.__name__)
#         )
#         self.ssl_cert = ssl_cert
#         self.rem_addr = addr[0]
#         self.port = addr[1]

#         asynchat.async_chat.__init__(self, sock=sock)

#         self.reading_string_literal = False
#         self.ibuffer = []
#         self.set_terminator(self.LINE_TERMINATOR)
#         self.msg_processor = ServerIMAPMessageProcessor(self)
#         self.in_ssl_handshake = False

#         if self.ssl_cert:
#             self.socket = ssl.wrap_socket(
#                 sock,
#                 server_side=True,
#                 certfile=self.ssl_cert,
#                 do_handshake_on_connect=False,
#             )
#             self.in_ssl_handshake = True
#         else:
#             self.push("* OK [CAPABILITY %s]\r\n" % " ".join(CAPABILITIES))

#         return

#     ####################################################################
#     #
#     def push(self, data):
#         """
#         We have our own version of push that logs sent messages to
#         our trace file if we have one.

#         Keyword Arguments:
#         data -- (str) data that is being sent to the client and that we
#                       need to log.
#         """

#         # XXX asyncore.dispatcher which asynchat.async_chat is a
#         #     subclass of is an old-style class and thus we can not
#         #     use 'super()' (all the more reason to move off of this
#         #     and use something more modern.)
#         #
#         asynchat.async_chat.push(self, data)
#         if self.trace_file:
#             self.trace_file(
#                 {"time": time.time(), "data": data, "msg_type": "SEND_DATA"}
#             )

#     ##################################################################
#     #
#     def log_string(self):
#         """
#         A bit of DRY: returns a string with common information that we like to
#         have in our log messages.
#         """
#         if self.msg_processor:
#             return "%s from %s:%d" % (
#                 str(self.msg_processor.client_handler.user),
#                 self.rem_addr,
#                 self.port,
#             )
#         else:
#             return "from %s:%d" % (self.rem_addr, self.port)

#     ##################################################################
#     #
#     def handle_read(self):
#         """
#         We have to wrap the handle_read class because the server will lock up
#         during SSL handshake if the remote end, like apple's mail client,
#         blocks asking the user to authorizing a self-signed certificate.
#         """
#         try:
#             if self.in_ssl_handshake:
#                 try:
#                     self.socket.do_handshake()
#                     self.in_ssl_handshake = False
#                     self.push(
#                         "* OK [CAPABILITY %s]\r\n" % " ".join(CAPABILITIES)
#                     )
#                 except ssl.SSLError as err:
#                     # If we are wanting read or wanting write then we
#                     # return and wait for the next time we are called.
#                     #
#                     if err.args[0] in (
#                         ssl.SSL_ERROR_WANT_READ,
#                         ssl.SSL_ERROR_WANT_WRITE,
#                     ):
#                         return
#                     else:
#                         raise
#                 return

#             # We are not in ssl handshake.. Just call the function we are
#             # overriding.
#             #
#             # XXX asyncore.dispatcher which asynchat.async_chat is a
#             #     subclass of is an old-style class and thus we can not
#             #     use 'super()' (all the more reason to move off of this
#             #     and use something more modern.)
#             #
#             asynchat.async_chat.handle_read(self)
#         except ssl.SSLWantReadError:
#             # If we are wanting read then we return and wait for the
#             # next time we are called.
#             #
#             return
#         except ssl.SSLError as err:
#             self.logger.error(
#                 "handle_read: %s, ssl error: %s" % (self.log_string(), str(err))
#             )
#             # Maybe we should just close the connection instead of
#             # raising the exception?
#             #
#             raise
#         return

#     ##################################################################
#     #
#     def handle_write(self):
#         """
#         Ditto handle_read...
#         """
#         try:
#             if self.in_ssl_handshake:
#                 try:
#                     self.socket.do_handshake()
#                     self.in_ssl_handshake = False
#                     self.push(
#                         "* OK [CAPABILITY %s]\r\n" % " ".join(CAPABILITIES)
#                     )
#                 except ssl.SSLError as err:
#                     # If we are wanting read or wanting write then we
#                     # return and wait for the next time we are called.
#                     #
#                     if err.args[0] in (
#                         ssl.SSL_ERROR_WANT_READ,
#                         ssl.SSL_ERROR_WANT_WRITE,
#                     ):
#                         return
#                     else:
#                         raise
#                 return

#             # We are not in ssl handshake.. Just call the function we are
#             # overriding.
#             #
#             asynchat.async_chat.handle_write(self)
#         except ssl.SSLError as err:
#             self.logger.error(
#                 "handle_write: %s, ssl error: %s"
#                 % (self.log_string(), str(err))
#             )
#             # Maybe we should just close the connection instead of
#             # raising the exception?
#             #
#             raise
#         return

#     ##########################################################################
#     #
#     def readable(self):
#         if isinstance(self.socket, ssl.SSLSocket):
#             while self.socket.pending() > 0:
#                 self.handle_read_event()
#         return True

#     ##########################################################################
#     #
#     def collect_incoming_data(self, data):
#         """
#         Buffer data read from the connect for later processing.
#         """
#         self.ibuffer.append(data)
#         return

#     ##################################################################
#     #
#     def found_terminator(self):
#         """
#         We have come across a message terminator from the IMAP client talking
#         to us.

#         This is invoked in two different states:

#         1) we have hit LINE_TERMINATOR and we were waiting for it.

#         2) we have are reading a literal string and we have read the requisite
#            number of characters for a complete message.

#         If (2) then we exit the state where we are reading a string literal and
#         set the terminator back to LINE_TERMINATOR so that we can read the rest
#         of the message from the IMAP client.

#         Otherwise we see if the message so far ends in the regexp
#         '{[0-9]+}'. If it does then that means we have encountered a literal
#         string in the input from the IMAP client.

#         If that is the case we switch our terminator to be the number of
#         characters to read from the client to complete that literal string.

#         If that is NOT the case then we have read a complete IMAP message from
#         our client and we pass it off to an ServerIMAPClient object to deal
#         with.
#         """
#         if self.reading_string_literal:
#             # If we were reading a string literal, then we switch back to
#             # reading lines.
#             #
#             self.reading_string_literal = False
#             self.set_terminator(self.LINE_TERMINATOR)
#             return

#         # We have just read a full line. This could be an IMAP command
#         # unless the line ends in '{[0-9]+}' which means that the
#         # line contains a string literal. We have to switch to reading
#         # a string literal for the number of characters defined by
#         # the integer inside of the '{}'
#         #
#         if len(self.ibuffer) == 0:
#             # Empty messages are bad too!
#             self.push("* BAD We do not accept empty messages.\r\n")
#             return

#         m = RE_LITERAL_STRING_START.search(self.ibuffer[-1])
#         if m:
#             # Set how many characters to read
#             #
#             self.set_terminator(int(m.group(1)))
#             self.reading_string_literal = True

#             # If the literal ended with "+}" then this is a non-synchronizing
#             # literal and we do not tell the client it can send more data.. it
#             # will already be on its way.
#             #
#             if self.ibuffer[-1][-2:] != "+}":
#                 self.push("+ Ready for more input\r\n")
#             else:
#                 # Remove the '+' from the end of our non-synchronizing
#                 # literal. Our subprocess will be confused by this since we
#                 # already did everything in a non-synchronizing literal
#                 # fashion.
#                 #
#                 self.ibuffer[-1] = self.ibuffer[-1][:-2] + self.ibuffer[-1][-1:]

#             # We also tack on a \r\n to the ibuffer so that whatever parses
#             # the message knows how to parse the literal string corrctly.
#             #
#             self.ibuffer.append("\r\n")
#             return

#         # Pass the full IMAP message on to the server IMAP message processor to
#         # deal with.
#         #
#         msg = "".join(self.ibuffer)
#         self.ibuffer = []
#         if self.msg_processor is None:
#             self.logger.error(
#                 "We have no message processor to send a message to."
#             )
#         else:
#             self.msg_processor.message(msg)
#         return

#     ##################################################################
#     #
#     def handle_close(self):
#         """
#         What to do when the IMAP client we are talking to closes their
#         connection to us.

#         Basically tell the ServerIMAPMessageProcessor that its services will no
#         longer be needed and various bits of cleanup.
#         """
#         log_msg = ["Client disconnected"]
#         if self.msg_processor is not None:
#             log_msg.append(self.log_string())
#             self.msg_processor.client_disconnected()
#             self.msg_processor = None
#         if self.socket is not None:
#             self.close()
#         self.logger.info(" ".join(log_msg))
#         return
