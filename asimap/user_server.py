"""
The heart of the asimap server process to handle a single user's
mailbox for multiple IMAP clients.

We get all of our data relayed to us from the main asimapd server via
connections on localhost.
"""

# system imports
#
import asyncio
import email.policy
import errno
import logging
import os
import os.path
import re
import signal
import socket
import sys
import time
from collections import Counter, defaultdict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from email import message_from_binary_file
from mailbox import NoSuchMailboxError
from pathlib import Path
from statistics import fmean, median, stdev
from typing import TYPE_CHECKING, Dict, List, Optional, Union

# 3rd party imports
#
import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration

# asimap imports
#
import asimap
import asimap.mbox
import asimap.trace

from .client import Authenticated
from .db import Database
from .exceptions import MailboxInconsistency
from .mbox import Mailbox, NoSuchMailbox
from .mh import MH
from .parse import BadCommand, IMAPClientCommand
from .trace import toggle_trace, trace

if TYPE_CHECKING:
    from _typeshed import StrPath

# By default every file is its own logging module. Kind of simplistic
# but it works for now.
#
logger = logging.getLogger("asimap.user_server")

BACKLOG = 5
USER_SERVER_PROGRAM: str = ""
RE_LITERAL_STRING_START = re.compile(rb"\{(\d+)(\+)?\}$")

TIME_BETWEEN_FULL_FOLDER_SCANS = 120
TIME_BETWEEN_METRIC_DUMPS = 60


####################################################################
#
def set_user_server_program(prg: "StrPath"):
    """
    Sets the 'USER_SERVER_PROGRAM' attribute on this module (so other modules
    will known how to launch the user server.)

    Arguments:
    - `prg`: An absolute path to the user server program.
    """
    prg = Path(prg)
    if not prg.is_file():
        raise ValueError(f"User server '{prg}' does not exist.")
    module = sys.modules[__name__]
    setattr(module, "USER_SERVER_PROGRAM", str(prg))


##################################################################
##################################################################
#
class IMAPClientProxy:
    """
    An IMAP Client out in the net sends messages to the main IMAP Server
    process. That main IMAP Server process sends them on to this
    per-authenticated user subprocess.

    This class has the asyncio.StreamReader and asyncio.StreamWriter's for
    receiving these messages and passing messages from this per-user subprocess
    back to the IMAP client out in the net.

    All of the messages we receive will be for an IMAP client that has
    successfully authenticated with the main server.

    The messages will be in the form of a decimal ascii integer followed by a
    new line that represents the length of the entire IMAP message we are being
    sent.

    After that will be the IMAP message (of the pre-indicated length.)

    To send messages back to the IMAP client we follow the same protocol.
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
        self.log = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.client_num = client_num
        self.name = name
        self.rem_addr = rem_addr
        self.port = port
        self.reader = reader
        self.writer = writer
        self.server = server
        # XXX if the main server got a client's id info via ID, it should pass
        #     it on to the subprocess like it should pass on the original
        #     source ip & port.
        self.cmd_processor = Authenticated(self, self.server)

        # used by the `run()` to continue reading from the client.
        #
        self.client_connected = False

    ####################################################################
    #
    async def close(self, cancel_reader: bool = True):
        """
        Shutdown our proxy connection to the IMAP client
        """
        self.client_connected = False
        try:
            if not self.writer.is_closing():
                self.writer.close()
            await self.writer.wait_closed()

            # Find the task in the server's list of clients and attempt to
            # cancel it.
            #
            # NOTE: We can get in to a deadlock because the reader task itself
            # will call close(). When the reader task is calling close, pass
            # `cancel_reader=False` so that we do not try to cancel and wait on
            # the reader task.
            #
            if cancel_reader:
                for task, client in self.server.clients.items():
                    if client == self:
                        if not task.done():
                            task.cancel()
                            await task
                        break

        except socket.error:
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
    async def run(self):
        """
        Entry point for the asyncio task for handling the network
        connection from an IMAP client.

        We read complete messages from the IMAP Client. Once we have a complete
        message, we parse it into an IMAP command then process it.

        NOTE: This client proxy will block until this command completes so
              every connection an actual IMAP client can only process one
              command at a time.

        XXX This needs to handle all exceptions since it is the root
            of an asyncio task, and shutdown and exit on CancelledError.
        """
        msg: bytes
        try:
            # We know the server is sending us complete messages that are
            # always terminated with self.LINE_TERMINATOR for the message
            # length.
            #
            # So read message length and terminator. Then read proscribed
            # number of bytes.
            #
            # We expect messages of the format:
            #
            # {\d+}\n< ... \d octects
            #
            self.trace("CONNECT", {})
            self.client_connected = True
            while self.client_connected:
                # Read until b'\n'. Trim off the '\n'. If the message is
                # not of 0 length then append it to our incremental buffer.
                #
                # XXX We should check to make sure that the message in the
                #     right format, ie: '{\d+}\n'
                #
                msg = await self.reader.readuntil(self.LINE_TERMINATOR)
                m = RE_LITERAL_STRING_START.search(msg)
                if not m:
                    # Messages from the server MUST start with '{\d}\n' If they
                    # do not conform to this then just disconnect this client.
                    #
                    self.log.warning(
                        "Client sent invalid message start: %r", msg
                    )
                    self.client_connected = False
                    break
                length = int(m.group(1))
                msg = await self.reader.readexactly(length)
                imap_msg = str(msg, "latin-1")
                self.trace("RECEIVED", {"data": imap_msg})

                # We special case if the client is idling. In this state we
                # look for ONLY a 'DONE' non-tagged message and when we get
                # that we call the 'do_done()' method on the client command
                # processor.
                #
                if self.cmd_processor.idling:
                    ls_imap_msg = imap_msg.lower().strip()
                    if ls_imap_msg.endswith("idle"):
                        await self.push("+ idling")
                    elif ls_imap_msg != "done":
                        await self.push(
                            f"* NO Expected 'DONE' not: {imap_msg}\r\n"
                        )
                    else:
                        await self.cmd_processor.do_done()
                    continue

                try:
                    imap_cmd = IMAPClientCommand(imap_msg)
                    imap_cmd.parse()

                except BadCommand as e:
                    # The command we got from the client was bad...  If we at
                    # least managed to parse the TAG out of the command the
                    # client sent us we use that when sending our response to
                    # the client so it knows what message we had problems with.
                    #
                    if imap_cmd.tag is not None:
                        await self.push(f"{imap_cmd.tag} BAD {e}\r\n")
                    else:
                        await self.push(f"* BAD {e}\r\n")
                    return

                # Pass the command on to the command processor to handle.
                #
                try:
                    self.server.commands_in_progress += 1
                    self.server.active_commands.append(imap_cmd)
                    # This is what actually executes the IMAP command from the
                    # IMAP client.
                    # command (or fails).
                    #

                    await self.cmd_processor.command(imap_cmd)
                finally:
                    try:
                        self.server.active_commands.remove(imap_cmd)
                    except asyncio.CancelledError:
                        logger.info("Cancelled: %s, %s", self, imap_cmd.qstr())
                        raise
                    except Exception as e:
                        logger.exception(
                            "Command %s had an exception: %s",
                            imap_cmd.qstr(),
                            e,
                        )
                        pass
                    self.server.commands_in_progress -= 1
                    if self.server.commands_in_progress > 0:
                        logger.debug(
                            "Commands in progress: %d, %s",
                            self.server.commands_in_progress,
                            ", ".join(
                                f"'{x.qstr()}'"
                                for x in self.server.active_commands
                            ),
                        )
                # If our state is "logged_out" after processing the command
                # then the client has logged out of the authenticated state. We
                # need to close our connection to the main server process.
                #
                if self.cmd_processor.state == "logged_out":
                    self.log.info("Client %s has logged out", self.log_string())
                    return

        except (
            asyncio.exceptions.IncompleteReadError,
            ConnectionError,
            socket.error,
        ):
            # Either we got an EOF while waiting for a line terminator. or the
            # client disconnected and we do not really care.
            #
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.exception("Exception in %s: %s", self, exc)
            raise
        finally:
            # We get here when we are no longer supposed to be connected to the
            # client. Close our connection and return which will cause this
            # task to be completed. Do not try to cancel this task itself.
            #
            await self.close(cancel_reader=False)

    ####################################################################
    #
    def trace(self, msg_type, msg):
        """
        We like to include the 'identity' of the IMAP Client handler in
        our trace messages so we can tie to gether which messages come
        from which connection. To make this easier we provide our own
        trace method that fills in various parts of the message being
        logged automatically.

        Keyword Arguments:
        msg_type -- 'SEND','RECEIVE','EXCEPTION','CONNECT','REMOTE_CLOSE'
        msg -- a dict that contains the rest of the message to trace log
        """
        msg["connection"] = self.name
        msg["remote"] = f"{self.rem_addr}:{self.port}"
        msg["msg_type"] = msg_type
        trace(msg)

    ####################################################################
    #
    async def push(self, *data: Union[bytes, str]):
        """
        Write data to the IMAP client by sending it up to the main process,
        which in turn sends it to the IMAP client.
        """
        for d in data:
            try:
                d = d.encode("latin-1") if isinstance(d, str) else d
            except UnicodeEncodeError:
                # Mnugh.. you think latin-1 would work, but sometimes we just
                # need to go with UTF-8.
                #
                d.encode("utf-8", "replace") if isinstance(d, str) else d
            try:
                self.writer.write(d)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ConnectionError("unable to write message") from exc
        if not self.writer.is_closing():
            # If the drain takes more than 2 seconds something has likely gone
            # wrong. Exit out. This blocking can hold on to locks too long.
            #
            try:
                async with asyncio.timeout(2):
                    await self.writer.drain()
            except asyncio.TimeoutError as exc:
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
        """
        format the username/remote address/port as a string
        """
        return self.name


##################################################################
##################################################################
#
class IMAPUserServer:
    """
    Listen on a port on localhost for connections from the asimapd
    main server that gets connections from actual IMAP clients. When
    we get one create an IMAPClientProxy object that gets the
    new connection (and handles all further IMAP related
    communications with the client.)
    """

    ##################################################################
    #
    def __init__(
        self,
        maildir: Path,
        debug: Optional[bool] = False,
    ):
        """
        Setup our dispatcher.. listen on a port we are supposed to accept
        connections on. When something connects to it create an
        IMAPClientHandler and pass it the socket.

        Arguments:
        - `options` : The options set on the command line
        - `maildir` : The directory our mailspool and database are in
        """
        self.maildir = maildir
        self.debug = debug

        self.log = logging.getLogger(
            "%s.%s" % (__name__, self.__class__.__name__)
        )

        # We expect all of the raw email sitting in storage to use `\n` for
        # line breaks so we use `email.policy.default`. All messages are read
        # in binary mode and use this factory.
        #
        self.mailbox = MH(
            self.maildir,
            create=True,
            factory=lambda f: message_from_binary_file(
                f, policy=email.policy.default
            ),
        )

        # A global counter for the next available uid_vv is stored in the user
        # server object. Mailboxes will get this value and increment it when
        # they need a new uid_vv. NOTE: This value is stored in the database
        # and set when the `user_server` is restored from the db. (If it has
        # never been set its initial value will be 0)
        #
        self.uid_vv = 0

        # A dict of the active mailboxes. An active mailbox is one that has an
        # instance of an Mailbox class.
        #
        # We keep active mailboxes around when IMAP clients are poking them in
        # some way. Active mailboxes are gotten rid of after a certain amount
        # of time during which no client pokes it.
        #
        # The key is the mailbox name.
        #
        self.active_mailboxes: Dict[str, Mailbox] = {}

        # Need to acquire the lock if we are adding or removing a mailbox from
        # the active mailboxes.
        #
        self.active_mailboxes_lock = asyncio.Lock()

        # We also have a dict of asyncio.Event's for mailboxes that are "being
        # activated". If multiple tasks want a mailbox and it has not been
        # activated we use these asyncio.Events so that only one task actually
        # activates the mailbox and then informs all the other waiting tasks
        # that it has been activated.
        #
        self.activating_mailboxes_lock = asyncio.Lock()
        self.activating_mailboxes: Dict[str, asyncio.Event] = {}

        # In order to avoid a race condition when instantiating a mailbox from
        # being expired before it is ever marked in use we use this boolean to
        # tell the `expire_inactive_folders` function to skip doing an expiry
        # check. If this is a positive integer then we should skip folder
        # expiry.
        #
        self.do_not_run_expiry_now = 0

        # A dict of the active IMAP clients that are talking to us.
        #
        # The key is the port number of the attached client.
        #
        self.clients: Dict[asyncio.Task, IMAPClientProxy] = {}

        # When we have any connected clients self.expiry gets set to
        # None. Otherwise use it to determine when we have hung around long
        # enough with no connected clients and decide to exit.
        #
        self.expiry: Optional[float] = time.monotonic() + 1800

        # `self.db` will be setup in the `new()` class method.
        #
        self.db: Database

        self.folder_scan_task: Optional[asyncio.Task] = None

        # Statistics for the `check_all_folders` function
        # key is mbox name, value is a time duration in seconds.
        #
        self.folder_check_durations: Dict[str, float] = {}

        # Updated by the IMAPClientProxy when it is processing commands.
        #
        self.commands_in_progress: int = 0
        self.active_commands: List[IMAPClientCommand] = []

        # We keep track of how many commands of which type we have received,
        # how many have failed, etc. These are for basic stats that will be
        # logged at level INFO
        #
        self.num_rcvd_commands: Counter[str] = Counter()
        self.num_failed_commands: Counter[str] = Counter()
        self.command_durations: defaultdict[str, list] = defaultdict(list)

        # The first time the user server starts up, when it does its initial
        # folder scan, we subject the folders to do a force check to make
        # sure that everything is as it should be. This flag indicates that
        # this check should be done. It will be set to `False` after the
        # initial folder check has finished.
        #
        # self.initial_folder_scan = True
        self.initial_folder_scan = False
        self.last_full_check = 0.0

        # To give individual client connections a more easily read name then
        # '120.0.1:<port number>' we will keep an incrementing integer. We plan
        # to also include the client's actual source address when we add
        # support for that but this is to make our logs easier to read so you
        # can tell which client is sending which command.
        #
        self.next_client_num = 1

    ##################################################################
    #
    async def _restore_from_db(self):
        """
        Restores any user server persistent state we may have in the db.
        If there is none saved yet then we save a bunch of default values.
        """
        results = await self.db.fetchone(
            "SELECT uid_vv FROM user_server ORDER BY id DESC LIMIT 1"
        )
        if results is None:
            await self.db.execute(
                "insert into user_server (uid_vv) values (?)",
                str(self.uid_vv),
                commit=True,
            )
        else:
            self.uid_vv = int(results[0])

    ####################################################################
    #
    @classmethod
    async def new(
        cls,
        maildir: Path,
        debug: Optional[bool] = False,
    ) -> "IMAPUserServer":
        user_server = cls(maildir, debug=debug)

        # A handle to the sqlite3 database where we store our persistent
        # information.
        #
        user_server.db = await Database.new(maildir)
        await user_server._restore_from_db()
        return user_server

    ####################################################################
    #
    async def shutdown(self):
        """
        Close various things when the server is shutting down.
        """
        if self.folder_scan_task and not self.folder_scan_task.done():
            self.folder_scan_task.cancel()
            await self.folder_scan_task

        # Close all client connections
        #
        async with asyncio.TaskGroup() as tg:
            for client in self.clients.values():
                tg.create_task(client.close())

        # Shutdown all active mailboxes
        #
        mboxes = []
        async with self.active_mailboxes_lock:
            for mbox_name, mbox in self.active_mailboxes.items():
                mboxes.append(mbox)
            self.active_mailboxes = {}

        async with asyncio.TaskGroup() as tg:
            for mbox in mboxes:
                tg.create_task(mbox.shutdown())

        await self.db.commit()
        await self.db.close()
        self.mailbox.close()

    ####################################################################
    #
    async def run(self):
        """
        Create and start the asyncio server to handle IMAP clients proxied
        through the main process. Run until the server exits.
        """
        if "SENTRY_DSN" in os.environ:
            traces_sample_rate = float(
                os.environ.get("SENTRY_TRACES_SAMPLE_RATE", 0.1)
            )
            profiles_sample_rate = float(
                os.environ.get("SENTRY_PROFILES_SAMPLE_RATE", 0.1)
            )
            logger.debug("Initializing sentry_sdk")
            sentry_sdk.init(
                dsn=os.environ["SENTRY_DSN"],
                # Set traces_sample_rate to 1.0 to capture 100%
                # of transactions for performance monitoring.
                traces_sample_rate=traces_sample_rate,
                profiles_sample_rate=profiles_sample_rate,
                integrations=[
                    AsyncioIntegration(),
                ],
                environment="devel" if self.debug else "production",
            )
        else:
            logger.debug(
                "Not initializing sentry_sdk: SENTRY_DSN not in enviornment"
            )

        # Listen to SIGUSR1 to toggle tracing on and office
        #
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGUSR1, toggle_trace)

        # Listen on localhost for connections from the main server process.
        #
        self.asyncio_server = await asyncio.start_server(
            self.new_client, "127.0.0.1"
        )
        addrs = [sock.getsockname() for sock in self.asyncio_server.sockets]
        self.port = addrs[0][1]
        logger.debug("Serving on port %s (addrs: %s)", self.port, addrs)

        try:
            # Before we tell the main server process what port we are listening
            # on we will do a find all the folders.
            #
            await self.find_all_folders()

            # Start the task that checks all folders
            #
            self.folder_scan_task = asyncio.create_task(self.folder_scan())

            # Let the initial folder scan begin before we accept any clients to
            # give it a head start.
            #
            await asyncio.sleep(2)

            # Print the port we are listening on to stdout so that the parent
            # process gets this information.
            #
            sys.stdout.write(f"{self.port}\n")
            sys.stdout.flush()

            async with self.asyncio_server:
                await self.asyncio_server.serve_forever()
        except asyncio.CancelledError:
            logger.debug("Server has been cancelled. Exiting.")
        finally:
            await self.shutdown()

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
        client_num = self.next_client_num
        self.next_client_num += 1
        name = f"client-{client_num:08d}"
        self.log.info(f"New IMAP client: {name}({rem_addr}:{port})")
        client_handler = IMAPClientProxy(
            self,
            name,
            client_num,
            rem_addr,
            port,
            reader,
            writer,
        )
        task = asyncio.create_task(client_handler.run(), name=name)
        task.add_done_callback(self.client_done)
        self.clients[task] = client_handler
        self.expiry = None

    ####################################################################
    #
    def dump_metrics(self):
        """
        Dump the metrics we have collected to the logs (and any metrics
        exporter when we hook that up), and reset the counters after dumping
        the metrics.
        """
        cmds = ", ".join(
            [f"{x}: {y}" for x, y in self.num_rcvd_commands.most_common()]
        )
        failed_cmds = ", ".join(
            [f"{x}: {y}" for x, y in self.num_failed_commands.most_common()]
        )
        if cmds:
            logger.info("Count of commands: %s", cmds)
        logger.info(
            "Count of total number of commands: %d",
            self.num_rcvd_commands.total(),
        )
        if failed_cmds:
            logger.info("Count of failed commands: %s", failed_cmds)
        logger.info(
            "Count of total number of failed commands: %d",
            self.num_failed_commands.total(),
        )

        self.num_rcvd_commands.clear()
        self.num_failed_commands.clear()

        logger.info(
            "Number of active mailboxes: %d",
            len(self.active_mailboxes),
        )
        logger.info("Number of clients: %d", len(self.clients))
        total_times = []
        for cmd in sorted(self.command_durations.keys()):
            if not self.command_durations[cmd]:
                continue
            durations = self.command_durations[cmd]
            cmd = cmd.rjust(12)
            total_times.extend(durations)
            if len(durations) == 1:
                logger.info("%s: max duration: %.3fs", cmd, durations[0])
                logger.info("%s: mean duration: %.3fs", cmd, durations[0])
            else:
                mean_time = fmean(durations)
                median_time = median(durations)
                logger.info("%s: max duration: %.3fs", cmd, max(durations))
                logger.info("%s: mean duration: %.3fs", cmd, mean_time)
                logger.info("%s: median duration: %.3fs", cmd, median_time)
                if len(durations) > 2:
                    stddev = stdev(durations, mean_time)
                    logger.info("%s: stddev duration: %.3fs", cmd, stddev)

        self.command_durations.clear()

        if len(total_times) == 1:
            logger.info("max duration: %.3fs", total_times[0])
            logger.info("mean duration: %.3fs", total_times[0])
        elif len(total_times) > 1:
            mean_time = fmean(total_times)
            median_time = median(total_times)
            logger.info("max duration: %.3fs", max(total_times))
            logger.info("mean duration: %.3fs", mean_time)
            logger.info("median duration: %.3fs", median_time)
            if len(total_times) > 2:
                stddev = stdev(total_times, mean_time)
                logger.info("stddev duration: %.3fs", stddev)

    ####################################################################
    #
    async def folder_scan(self):
        """
        at regular intervals we need to scan all the inactive folders to
        see if any new mail has arrived.
        """
        logger.debug("Folder scan task is starting")
        last_metrics_dump = time.monotonic()
        try:
            while True:
                # XXX For now try skipping resyncs to see if we were missing up
                #     exiring folders too early (and also see if we can handle
                #     all mailboxes loaded in to memory always)
                #
                # await self.expire_inactive_folders()

                now = time.monotonic()
                if now - last_metrics_dump > TIME_BETWEEN_METRIC_DUMPS:
                    self.dump_metrics()
                    last_metrics_dump = now

                # If it has been more than <n> seconds since a full scan, then
                # do a full scan.
                #
                if now - self.last_full_check > TIME_BETWEEN_FULL_FOLDER_SCANS:
                    await self.check_all_folders()
                    if self.initial_folder_scan:
                        logger.info("Finished initial scan of all folders")
                        self.initial_folder_scan = False
                    self.last_full_check = time.monotonic()

                # At the end of loop see if we have hit our lifetime expiry.
                # This will be None as long as there are active
                # clients. Otherwise it is a time after which the server should
                # exit.
                #
                if self.expiry and self.expiry < now:
                    self.asyncio_server.close()
                    await self.asyncio_server.wait_closed()
                    return

                # And sleep before we do another folder scan
                #
                await asyncio.sleep(10)
        except asyncio.exceptions.CancelledError:
            logger.info("folder_scan task has been cancelled")
            raise
        finally:
            if self.asyncio_server.is_serving():
                self.asyncio_server.close()
                await self.asyncio_server.wait_closed()

    ####################################################################
    #
    def client_done(self, task):
        """
        When the asyncio task represented by the IMAPClient has
        exited this call back is invoked.

        Remove the task from the server's dict of IMAPClient tasks.
        """
        if task not in self.clients:
            return
        client = self.clients[task]

        # If this client had selected any mailboxes, make sure they are
        # unselected.
        #
        for mbox in self.active_mailboxes.values():
            if client.name in mbox.clients:
                mbox.unselected(client.name)
        del self.clients[task]

        # If there are no more clients, then set the IMAPUserServer's expiry
        # time.
        #
        if not self.clients:
            self.expiry = time.monotonic() + 1800
            expiry = datetime.now() + timedelta(seconds=1800)
            self.log.debug("No more IMAP clients. Expiry set for %s", expiry)

        self.log.info("IMAP Client task done (disconnected): %s", client.name)

    ##################################################################
    #
    async def get_next_uid_vv(self) -> int:
        """
        Return the next uid_vv. Also update the underlying database
        so that its uid_vv state remains up to date.
        """
        self.uid_vv += 1
        await self.db.execute(
            "UPDATE user_server SET uid_vv = ?",
            (str(self.uid_vv),),
            commit=True,
        )
        return self.uid_vv

    ##################################################################
    #
    def folder_exists(self, name: str) -> bool:
        """
        Returns True if the underlying MH folder exists. False otherwise.
        """
        try:
            self.mailbox.get_folder(name)
        except NoSuchMailboxError:
            return False
        return True

    ##################################################################
    #
    @asynccontextmanager
    async def get_mailbox(self, name: str) -> AsyncGenerator[Mailbox]:
        """
        To insure that the mailbox's use count is always positive when we
        get it and that it can not expired due to a race condition between when
        the mailbox is fetched and when it is being used we provide a context
        manager that can guarantee that the mailbox's in-use is always
        positive.
        """
        mbox = None
        try:
            self.do_not_run_expiry_now += 1
            try:
                start = time.monotonic()
                mbox = await self._get_mailbox(name)
                mbox.in_use_count += 1
            finally:
                self.do_not_run_expiry_now -= 1
                # XXX This should never happen.. but just in case it does.
                #
                if self.do_not_run_expiry_now < 0:
                    self.do_not_run_expiry_now = 0

            end = time.monotonic()
            duration = end - start
            if duration > 0.2:
                logger.info(
                    "Done getting for mailbox '%s', took: %.3fs", name, duration
                )

            # Now that we have a mbox, and its use count is positive we can
            # yield it safe in the knowledge that it will not be expired until
            # after this yield returns manager exits.
            #
            yield mbox
        finally:
            if mbox:
                mbox.in_use_count -= 1

    ##################################################################
    #
    async def _get_mailbox(self, name: str) -> Mailbox:
        """
        A factory of sorts.. if we have an active mailbox with the given name
        return it.

        If we do not instantiate an instance of that mailbox and add it to our
        list of active mailboxes.

        Arguments:
        - `name`: The name of the mailbox our caller wants.
        - `expiry`: If we have to instantiate a mailbox give it this expiry
          time. Used so that boxes that are just being updated rarely expire
          and do not take up excess memory in the server. NOTE: As long as a
          mailbox has an active clients, the expiry timer will NOT be active.
        """
        # The INBOX is case-insensitive but it is stored in our file system in
        # a case sensitive lower case fashion..
        #
        if name.lower() == "inbox":
            name = "inbox"

        if not name.strip() or not self.folder_exists(name):
            raise NoSuchMailbox(f"No such mailbox: '{name}'")

        # If the mailbox is active we can return it immediately. If not then,
        # outside of the self.active_mailboxes_lock we will activate the
        # mailbox.
        #
        # NOTE: We can do the check and return if the mailbox without getting
        #       the active_mailbox_lock because we are using asyncio. Nothing
        #       in this loop lets the task swap out so this access to a shared
        #       resource is safe.
        if name in self.active_mailboxes:
            if self.active_mailboxes[name].deleted:
                raise NoSuchMailbox(f"'{name}' has been deleted.")
            return self.active_mailboxes[name]

        # If multiple tasks are trying to activate a mailbox only *ONE* will
        # get `creating=True`.
        #
        # Again, like above, nothing here changes our currently running asyncio
        # task so we do not need to hold the `activating_mailboxes_lock`
        #
        creating = False
        if name in self.activating_mailboxes:
            event = self.activating_mailboxes[name]
        else:
            event = asyncio.Event()
            self.activating_mailboxes[name] = event
            creating = True

        if not creating:
            inst_start = time.monotonic()
            await event.wait()
            duration = time.monotonic() - inst_start
            if duration > 0.1:
                logger.debug(
                    "Done waiting for mailbox '%s', took: %.3fs", name, duration
                )

            # Once the wait completes we are guaranteed that
            # `self.active_mailboxes` has the key `name` in it.
            #
            if self.active_mailboxes[name].deleted:
                raise NoSuchMailbox(f"'{name}' has been deleted.")
            return self.active_mailboxes[name]

        inst_start = time.monotonic()
        # Instantiate the mailbox. Add it to `active_mailboxes`, signal any
        # other task waiting on the event that it can now get the mailbox.
        #
        mbox = await Mailbox.new(
            name,
            self,
        )
        async with self.active_mailboxes_lock:
            self.active_mailboxes[name] = mbox

        async with self.activating_mailboxes_lock:
            event.set()
            del self.activating_mailboxes[name]
        duration = time.monotonic() - inst_start
        if duration > 3:
            logger.debug(
                "Instantiated mailbox '%s', took: %.3fs", name, duration
            )
        return mbox

    ##################################################################
    #
    async def expire_inactive_folders(self):
        """
        Go through the list of active mailboxes and if any of them are around
        past their expiry time, expire them.
        """
        # And finally check all active mailboxes to see if their in-use count
        # is 0 and expire them if it is.
        #
        expired = []
        expired_mboxes = []
        async with self.active_mailboxes_lock:
            # We want to make sure that we do not try to expire mailboxes while
            # a mailbox maybe in a half instantiated state. If that is the case
            # we just skip running expiry now.
            #
            if self.do_not_run_expiry_now > 0:
                logger.info(
                    "Skipping due to `do_not_run_expiry_now` being set, "
                    "count: %d",
                    self.do_not_run_expiry_now,
                )
                return

            for mbox_name, mbox in self.active_mailboxes.items():
                # If the in-use count is positive or the mbox has clients,
                # do not expire it.
                #
                if (
                    mbox.in_use_count > 0
                    or mbox.clients
                    or mbox.executing_tasks
                ):
                    continue
                expired.append(mbox_name)
                expired_mboxes.append(mbox)

            # Remove the to-be expired mailboxees from active_mailboxes.
            #
            for mbox_name in expired:
                del self.active_mailboxes[mbox_name]

        # Go through the mbox's we deleted from `active_mailboxes` and shut
        # them down.
        #
        if expired_mboxes:
            async with asyncio.TaskGroup() as tg:
                for mbox in expired_mboxes:
                    tg.create_task(mbox.shutdown())

            logger.debug(
                "Expiring active %d mailboxes",
                len(expired_mboxes),
            )

    ##################################################################
    #
    async def _get_and_release_mbox(self, mbox_name: str) -> None:
        """
        a helper for `find_all_folders` to use the context manager for
        managing the in_use_count of a mailbox.

        Basically we need to make sure that the in_use_count is properly reset
        when this function exits.
        """
        async with self.get_mailbox(mbox_name):
            pass

    ##################################################################
    #
    async def find_all_folders(self):
        """
        compare the list of folders on disk with the list of known folders in
        our database.

        For every folder found on disk that does not exist in the database
        create an entry for it.
        """
        start_time = time.monotonic()
        extant_mboxes = {}
        async for row in self.db.query(
            "SELECT name, mtime FROM mailboxes ORDER BY name"
        ):
            name, mtime = row
            extant_mboxes[name] = mtime

        maildir_root_len = len(str(self.maildir)) + 1
        async with asyncio.TaskGroup() as tg:
            for root, dirs, files in self.maildir.walk(follow_symlinks=True):
                for dir in dirs:
                    dirname = str(root / dir)[maildir_root_len:]
                    if dirname not in extant_mboxes:
                        tg.create_task(self._get_and_release_mbox(dirname))
                        await asyncio.sleep(0)

        logger.info(
            "Finished. Took %.3f seconds", time.monotonic() - start_time
        )

    ##################################################################
    #
    async def check_folder(
        self,
        mbox_name: str,
        mtime: int,
        force: bool = False,
    ):
        r"""
        Check the mtime for a single folder. If it is newer than the mtime
        passed in then do a resync of that folder.

        If the folder is an active folder it may cause messages to be generated
        and sent to clients that are watching it in some way.

        The folder's \Marked and \Unmarked attributes maybe set in
        the process of this run.

        - `force` : If True this will force a full resync on all
                    mailbox regardless of their mtimes.
        """
        start_time = time.monotonic()
        path = os.path.join(self.mailbox._path, mbox_name)
        seq_path = os.path.join(path, ".mh_sequences")
        try:
            fmtime = await Mailbox.get_actual_mtime(self.mailbox, mbox_name)
            if (fmtime > mtime) or force:
                # Just calling `get_mailbox` on a mailbox that is not active
                # will cause a 'check_new_msgs_and_flags()' to be called, thus
                # checking the folder. Since the expiry time is 0, it will be
                # expired the next time the `expired_inactive_folders()` method
                # runs (and the mailbox is not resyncing)
                #
                async with self.get_mailbox(mbox_name):
                    pass

        except MailboxInconsistency as e:
            # If hit one of these exceptions they are usually
            # transient.  we will skip it. The command processor in
            # client.py knows how to handle these better.
            #
            logger.warning("skipping '%s' due to: %s", mbox_name, str(e))
        except (OSError, IOError) as e:
            if e.errno == errno.ENOENT:
                logger.error(
                    "One of %s or %s does not exist for mtime check",
                    path,
                    seq_path,
                )
        finally:
            self.folder_check_durations[mbox_name] = (
                time.monotonic() - start_time
            )

    ##################################################################
    #
    async def check_all_folders(self, force: bool = False):
        r"""
        This goes through all of the folders and sees if any of the mtimes we
        have on disk disagree with the mtimes we have in the database.

        If they do we then do a resync of that folder.

        If the folder is an active folder it may cause messages to be generated
        and sent to clients that are watching it in some way.

        The folder's \Marked and \Unmarked attributes maybe set in
        the process of this run.

        - `force` : If True this will force a full resync on all
                    mailbox regardless of their mtimes.
        """

        async def check_folder_worker(name: str, queue: asyncio.Queue) -> None:
            """
            An asyncio task worker used to parallelize checking folders to
            a certain extent.
            """
            while True:
                mbox_name, mtime = await queue.get()
                try:
                    # Do not bother checking on an active folder. This loops is
                    # only to check for updates to folders that are not
                    # active. (Active folders try to recheck for updates every
                    # 10 secs.) This is in case the folder became active while
                    # we were running the check.
                    #
                    if mbox_name in self.active_mailboxes:
                        continue

                    try:
                        await self.check_folder(
                            mbox_name, mtime, force=self.initial_folder_scan
                        )
                    except asyncio.CancelledError:
                        logger.info("Cancelled")
                        raise
                    except Exception as e:
                        logger.exception(
                            "Problem checking folder '%s': %s", mbox_name, e
                        )
                finally:
                    queue.task_done()

        start_time = time.time()

        # Go through all of the folders and mtimes we know about from the
        # sqlite db. Put each non-active folder on to our worker queue for the
        # workers process.
        #
        kount = 0
        queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
        async for mbox_name, mtime in self.db.query(
            "SELECT name, mtime FROM mailboxes WHERE attributes "
            "NOT LIKE '%%ignored%%' ORDER BY name"
        ):
            # can skip doing a check since it is already active. They will
            # check themselves while they are active.
            #
            if mbox_name in self.active_mailboxes:
                continue
            kount += 1
            queue.put_nowait((mbox_name, mtime))

        self.folder_check_durations = {}
        # Create 10 asyncio workers to process the folders so that we have 10
        # folders being processed at any one time.
        #
        async with asyncio.TaskGroup() as tg:
            workers = []
            for i in range(10):
                worker = tg.create_task(
                    check_folder_worker(f"check-folder-worker-{i}", queue)
                )
                workers.append(worker)

            worker_start = time.monotonic()
            await queue.join()
            worker_duration = time.monotonic() - worker_start
            for worker in workers:
                worker.cancel()

        # Now point in doing all the math if we are not going to log it.
        # NOTE: In the future we might submit these as metrics.
        #
        logger.info(
            "Finished, Took %.3f seconds to check %d folders",
            (time.time() - start_time),
            kount,
        )
        scan_durations = list(self.folder_check_durations.values())
        if self.debug and len(scan_durations) > 1:
            mean_scan_duration = fmean(scan_durations)
            median_scan_duration = median(scan_durations)
            stddev_scan_duration = (
                stdev(scan_durations, mean_scan_duration)
                if len(scan_durations) > 2
                else 0.0
            )
            by_duration = sorted(
                list(self.folder_check_durations.items()),
                key=lambda x: x[1],
                reverse=True,
            )
            mbox_max_durations = ", ".join(
                f"{x[0]}:{x[1]:.3f}s" for x in by_duration[:10]
            )
            logger.debug("Total worker execution time: %.3f", worker_duration)
            logger.debug(
                "Individual check_folder durations: mean: %.3fs, median: %.3fs, stddev: %.3fs, max durations: %s",
                mean_scan_duration,
                median_scan_duration,
                stddev_scan_duration,
                mbox_max_durations,
            )
        self.folder_check_durations = {}
