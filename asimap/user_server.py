"""
The heart of the asimap server process to handle a single user's
mailbox for multiple IMAP clients.

We get all of our data relayed to us from the main asimapd server via
connections on localhost.
"""
# system imports
#
import asyncio
import email
import errno
import logging
import mailbox
import os
import os.path
import re
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Union

# 3rd party imports
#
import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration

# asimap imports
#
import asimap
import asimap.mbox
import asimap.message_cache
import asimap.parse
from asimap.client import Authenticated
from asimap.db import Database
from asimap.exceptions import MailboxInconsistency, MailboxLock
from asimap.trace import trace

if TYPE_CHECKING:
    from _typeshed import StrPath

# By default every file is its own logging module. Kind of simplistic
# but it works for now.
#
logger = logging.getLogger("asimap.user_server")

BACKLOG = 5
USER_SERVER_PROGRAM: str = ""
RE_LITERAL_STRING_START = re.compile(rb"\{(\d+)(\+)?\}$")


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
        rem_addr: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        self.log = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        self.name = name
        self.rem_addr = rem_addr
        self.reader = reader
        self.writer = writer
        self.server = server
        self.cmd_processor = Authenticated(self, self.server)

    ####################################################################
    #
    async def close(self):
        """
        Shutdown our proxy connection to the IMAP client
        """
        try:
            if not self.writer.is_closing():
                self.writer.close()
            await self.writer.wait_closed()
            await self.trace("CLOSE", {})
        except socket.error:
            pass
        except Exception as exc:
            self.log.error("Exception when closing %s: %s", self, exc)

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
            await self.trace("CONNECT", {})
            client_connected = True
            while client_connected:
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
                    self.log.info(f"Client sent invalid message start: {msg!r}")
                    client_connected = False
                    break
                self.log.debug(f"start: message: {str(msg,'latin-1')}")
                length = int(m.group(1))
                msg = await self.reader.readexactly(length)
                imap_msg = str(msg, "latin-1")
                await self.trace("RECEIVED", {"data": imap_msg})

                # We special case if the client is idling. In this state we
                # look for ONLY a 'DONE' non-tagged message and when we get
                # that we call the 'do_done()' method on the client command
                # processor.
                #
                if self.cmd_processor.idling:
                    if imap_msg.lower().strip() != "done":
                        await self.push(
                            f"* BAD Expected 'DONE' not: {imap_msg}\r\n"
                        )
                    else:
                        self.cmd_processor.do_done(None)
                    return

                try:
                    imap_cmd = asimap.parse.IMAPClientCommand(imap_msg)
                    imap_cmd.parse()

                except asimap.parse.BadCommand as e:
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
                await self.cmd_processor.command(imap_cmd)

                # If our state is "logged_out" after processing the command
                # then the client has logged out of the authenticated state. We
                # need to close our connection to the main server process.
                #
                if self.cmd_processor.state == "logged_out":
                    self.log.info(
                        "Client %s has logged out of the subprocess"
                        % self.log_string()
                    )
                    return

        except asyncio.exceptions.IncompleteReadError:
            # We got an EOF while waiting for a line terminator. Client
            # disconnecrted and we do not really care.
            #
            pass
        except Exception as exc:
            self.log.exception("Exception in %s: %s", self, exc)
        finally:
            # We get here when we are no longer supposed to be connected to the
            # client. Close our connection and return which will cause this
            # task to be completed.
            #
            await self.close()

    ####################################################################
    #
    async def trace(self, msg_type, msg):
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
        msg["msg_type"] = msg_type
        await trace(msg)

    ####################################################################
    #
    async def push(self, *data: Union[bytes, str]):
        """
        Write data to the IMAP client by sending it up to the main process,
        which in turn sends it to the IMAP client.
        """
        for d in data:
            if isinstance(d, str):
                d = bytes(d, "latin-1")
            self.writer.write(d)
        await self.writer.drain()

        msg = [str(d, "latin-1") if isinstance(d, bytes) else d for d in data]
        await self.trace("SEND", {"data": "".join(msg)})

    ##################################################################
    #
    def log_string(self) -> str:
        """
        format the username/remote address/port as a string
        """
        return f"from {self.name}"


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
        trace_enabled: Optional[bool] = False,
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

        self.maildir = maildir

        # We setup our MH mailbox to return email.message.EmailMessage's
        # (instead of MHMessage's). Lets us use the most modern email model at
        # this time.
        #
        self.mailbox = mailbox.MH(
            self.maildir,
            factory=lambda x: email.message_from_binary_file(
                x, policy=email.policy.default
            ),
            create=True,
        )

        # A global counter for the next available uid_vv is stored in the user
        # server object. Mailboxes will get this value and increment it when
        # they need a new uid_vv.
        #
        self.uid_vv = 0

        # A handle to the sqlite3 database where we store our persistent
        # information.
        #
        self.db = Database(maildir)

        # A dict of the active mailboxes. An active mailbox is one that has an
        # instance of an asimap.mbox.Mailbox class.
        #
        # We keep active mailboxes around when IMAP clients are poking them in
        # some way. Active mailboxes are gotten rid of after a certain amount
        # of time during which no client pokes it.
        #
        # The key is the mailbox name.
        #
        self.active_mailboxes: Dict[str, asimap.mbox.Mailbox] = {}

        # A dict of the active IMAP clients that are talking to us.
        #
        # The key is the port number of the attached client.
        #
        self.clients: Dict[asyncio.Task, IMAPClientProxy] = {}

        # There is a single message cache per user server instance.
        #
        self.msg_cache = asimap.message_cache.MessageCache()

        # When we have any connected clients self.expiry gets set to
        # None. Otherwise use it to determine when we have hung around long
        # enough with no connected clients and decide to exit.
        #
        self.expiry: Optional[float] = time.time() + 1800

        # and finally restore any pesistent state stored in the db for the user
        # server.
        #
        self._restore_from_db()
        return

    ##################################################################
    #
    def _restore_from_db(self):
        """
        Restores any user server persistent state we may have in the db.
        If there is none saved yet then we save a bunch of default values.
        """
        # c = await self.db.execute("select uid_vv from user_server order by id desc limit 1")
        # results = await c.fetchone()

        c = self.db.cursor()
        c.execute("select uid_vv from user_server order by id desc limit 1")
        results = c.fetchone()
        if results is None:
            c.execute(
                "insert into user_server (uid_vv) values (?)", str(self.uid_vv)
            )
            c.close()
            self.db.commit()
        else:
            self.uid_vv = int(results[0])
            c.close()
        return

    ####################################################################
    #
    async def run(self):
        """
        Create and start the asyncio server to handle IMAP clients proxied
        through the main process. Run until the server exits.
        """
        if "SENTRY_DSN" in os.environ:
            sys.stderr.write("initializing sentry_sdk\n")
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
        else:
            sys.stderr.write("***** SENTRY_DSN not in enviornment")
        self.asyncio_server = await asyncio.start_server(
            self.new_client, "127.0.0.1"
        )
        addrs = [sock.getsockname() for sock in self.asyncio_server.sockets]
        self.port = addrs[0][1]
        sys.stderr.write(f"listening on {addrs}\n")
        sys.stderr.flush()
        self.log.debug("Serving on port %s (addrs: %s)", self.port, addrs)

        try:
            # Before we tell the main server process what port we are listening
            # on we will do a find and check of all the folders.
            #
            await self.find_all_folders()
            await self.check_all_folders()
            self.last_full_check = time.time()
            self.folder_scan_task = asyncio.create_task(self.folder_scan())

            # Print the port we are listening on to stdout so that the parent
            # process gets this information.
            #
            sys.stdout.write(f"{self.port}\n")
            sys.stdout.flush()
            sys.stderr.write(f"***** SUBPROCESS: Wrote port: {self.port}\n")

            async with self.asyncio_server:
                await self.asyncio_server.serve_forever()

        except asyncio.exceptions.CancelledError:
            pass
        finally:
            self.folder_scan_task.cancel()
            await self.folder_scan_task  # ?? do we need to do this?
            clients = [c.close() for c in self.clients.values()]
            await asyncio.gather(*clients, return_exceptions=True)

            self.db.commit()
            self.db.close()
            self.mailbox.close()

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
        name = f"{rem_addr}:{port}"
        self.log.debug(f"New IMAP client proxy: {name}")
        client_handler = IMAPClientProxy(self, name, rem_addr, reader, writer)
        task = asyncio.create_task(client_handler.start(), name=name)
        task.add_done_callback(self.client_done)
        self.clients[task] = client_handler
        self.expiry = None

    ####################################################################
    #
    async def folder_scan(self):
        """
        at regular intervals we need to scan the folders to see if any new
        mail has arrived.
        """
        try:
            while True:
                await asyncio.sleep(30)
                await self.check_all_active_folders()
                await self.expire_inactive_folders()

                # If it has been more than 5 minutes since a full scan, then do
                # a full scan.
                #
                now = time.time()
                if now - self.last_full_check > 300:
                    await self.check_all_folders()
                    self.last_full_check = time.time()

                # At the end of loop see if we have hit our lifetime expiry.
                # This will be None as long as there are active
                # clients. Otherwise it is a time after which the server should
                # exit.
                #
                if self.expiry and self.expiry < now:
                    self.asyncio_server.close()
                    await self.asyncio_server.wait_closed()
                    return
        except asyncio.exceptions.CancelledError:
            pass
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
            mbox.unselected(client.name)
        del self.clients[task]

        # If there are no more clients, then set the IMAPUserServer's expiry
        # time.
        #
        if not self.clients:
            self.expiry = time.time() + 1800
            self.log.debug(
                "No more IMAP clients. Expiry set for %s",
                datetime.fromtimestamp(self.expiry),
            )

        self.log.debug("IMAP Client task done (disconnected): %s", client.name)

    ##################################################################
    #
    async def get_next_uid_vv(self):
        """
        Return the next uid_vv. Also update the underlying database
        so that its uid_vv state remains up to date.
        """
        self.uid_vv += 1
        c = self.db.cursor()
        c.execute("update user_server set uid_vv = ?", (str(self.uid_vv),))
        c.close()
        self.db.commit()
        return self.uid_vv

    ##################################################################
    #
    def log_info(self, message, type="info"):
        """
        Replace the log_info method with one that uses our stderr logger
        instead of trying to write to stdout.

        Arguments:
        - `message`:
        - `type`:
        """
        if type not in self.ignore_log_types:
            if type == "info":
                self.log.info(message)
            elif type == "error":
                self.log.error(message)
            elif type == "warning":
                self.log.warning(message)
            elif type == "debug":
                self.log.debug(message)
            else:
                self.log.info(message)
        return

    ##################################################################
    #
    async def get_mailbox(self, name, expiry=900):
        """
        A factory of sorts.. if we have an active mailbox with the given name
        return it.

        If we do not instantiate an instance of that mailbox and add it to our
        list of active mailboxes.

        Arguments:
        - `name`: The name of the mailbox our caller wants.
        - `expiry`: If we have to instantiate a mailbox give it this expiry
          time. Used so that boxes that are just being updated rarely expire
          and do not take up excess memory in the server. Defaults to 15
          minutes.
        """
        # The INBOX is case-insensitive but it is stored in our file system in
        # a case sensitive lower case fashion..
        #
        if name.lower() == "inbox":
            name = "inbox"
        if name in self.active_mailboxes:
            return self.active_mailboxes[name]

        # otherwise.. make an instance of this mailbox.
        #
        mbox = asimap.mbox.Mailbox(name, self, expiry=expiry)
        self.active_mailboxes[name] = mbox
        return mbox

    ##################################################################
    #
    async def check_all_active_folders(self):
        """
        Like 'check_all_folders' except this only checks folders that are
        active and have clients in IDLE listening to them.
        """
        for name, mbox in self.active_mailboxes.items():
            if any(x.idling for x in mbox.clients.values()):
                try:
                    await mbox.resync()
                except (MailboxLock, MailboxInconsistency) as e:
                    # If hit one of these exceptions they are usually
                    # transient.  we will skip it. The command processor in
                    # client.py knows how to handle these better.
                    #
                    self.log.warn(
                        "check-all-active: skipping '%s' due to: "
                        "%s" % (name, str(e))
                    )
        return

    ##################################################################
    #
    async def expire_inactive_folders(self):
        """
        Go through the list of active mailboxes and if any of them are around
        past their expiry time, expire time.
        """
        # And finally check all active mailboxes to see if they have no clients
        # and are beyond their expiry time.
        #
        expired = []
        for mbox_name, mbox in self.active_mailboxes.items():
            if (
                len(mbox.clients) == 0
                and mbox.expiry is not None
                and mbox.expiry < time.time()
            ):
                expired.append(mbox_name)

        for mbox_name in expired:
            await self.active_mailboxes[mbox_name].commit_to_db()
            del self.active_mailboxes[mbox_name]
            self.msg_cache.clear_mbox(mbox_name)

    ##################################################################
    #
    async def find_all_folders(self):
        """
        compare the list of folders on disk with the list of known folders in
        our database.

        For every folder found on disk that does not exist in the database
        create an entry for it.
        """
        start_time = time.time()
        extant_mboxes = {}
        mboxes_to_create = []
        c = self.db.cursor()
        c.execute("select name, mtime from mailboxes order by name")
        for row in c:
            name, mtime = row
            extant_mboxes[name] = mtime
        c.close()

        # The user_server's CWD is the root of our mailboxes.
        #
        for root, dirs, files in os.walk(".", followlinks=True):
            for d in dirs:
                dirname = os.path.normpath(os.path.join(root, d))
                if dirname not in extant_mboxes:
                    mboxes_to_create.append(dirname)

        # Now 'mboxes_to_create' is a list of full mailbox names that were in
        # the file system but not in the database. Instantiate these (with the
        # create flag set so that we will not get any nasty surpises about
        # missing .mh_sequence files)
        #
        for mbox_name in mboxes_to_create:
            await self.get_mailbox(mbox_name, expiry=0)
        self.log.debug(
            "find_all_folders: finished. Took %f seconds"
            % (time.time() - start_time)
        )

    ##################################################################
    #
    async def check_all_folders(self, force=False):
        """
        This goes through all of the folders and sees if any of the mtimes we
        have on disk disagree with the mtimes we have in the database.

        If they do we then do a resync of that folder.

        If the folder is an active folder it may cause messages to be generated
        and sent to clients that are watching it in some way.

        The folder's \\Marked and \\Unmarked attributes maybe set in
        the process of this run.

        - `force` : If True this will force a full resync on all
                    mailbox regardless of their mtimes.
        """
        start_time = time.time()
        # Get all of the folders and mtimes we know about from the sqlite db at
        # the beginning. This takes more memory (not _that_ much really in the
        # grand scheme of things) but it gives the answers in one go-round to
        # the db and we get to deal with the data in an easier format.
        #
        mboxes = []
        c = self.db.cursor()
        c.execute(
            "select name, mtime from mailboxes where attributes "
            "not like '%%ignored%%' order by name"
        )
        for row in c:
            mboxes.append(row)
        c.close()

        # Now go through all of the extant mailboxes and see
        # if their mtimes have changed warranting us to force them to resync.
        #
        # XXX We should probably skip folders that are active and have been
        #     resync'd in the last 30 seconds because those are already checked
        #     by another process.
        #
        for mbox_name, mtime in mboxes:
            # If this mailbox is active and has a client idling on it then we
            # can skip doing a resync here. It has been handled already.
            #
            if mbox_name in self.active_mailboxes and (
                any(
                    x.idling
                    for x in self.active_mailboxes[mbox_name].clients.values()
                )
            ):
                continue

            path = os.path.join(self.mailbox._path, mbox_name)
            seq_path = os.path.join(path, ".mh_sequences")
            try:
                fmtime = await asimap.mbox.Mailbox.get_actual_mtime(
                    self.mailbox, mbox_name
                )
                if (fmtime > mtime) or force:
                    # The mtime differs.. force the mailbox to resync.
                    #
                    self.log.debug(
                        "check_all_folders: doing resync on '%s' "
                        "stored mtime: %d, actual mtime: %d"
                        % (mbox_name, mtime, fmtime)
                    )
                    m = self.get_mailbox(mbox_name, 30)
                    if (m.mtime >= fmtime) and not force:
                        # Looking at the actual mailbox its mtime is NOT
                        # earlier than the mtime of the actual folder so we can
                        # skip this resync. But commit the mailbox data to the
                        # db so that the actual mtime value is stored.
                        #
                        await m.commit_to_db()
                    else:
                        # Yup, we need to resync this folder.
                        await m.resync(force=force)
            except (MailboxLock, MailboxInconsistency) as e:
                # If hit one of these exceptions they are usually
                # transient.  we will skip it. The command processor in
                # client.py knows how to handle these better.
                #
                self.log.warn(
                    "check_all_folders: skipping '%s' due to: "
                    "%s" % (mbox_name, str(e))
                )
            except (OSError, IOError) as e:
                if e.errno == errno.ENOENT:
                    self.log.error(
                        "check_all_folders: One of %s or %s does "
                        "not exist for mtime check" % (path, seq_path)
                    )

        self.log.debug(
            "check_all_folders finished, Took %f seconds"
            % (time.time() - start_time)
        )
        return
