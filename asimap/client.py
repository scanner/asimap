"""
Here we have the classes that represent the server side state for a
single connected IMAP client.
"""

# system imports
#
import asyncio
import logging
import sys
from enum import StrEnum
from itertools import count, groupby
from typing import TYPE_CHECKING, Union

# asimapd imports
#
from asimap import __version__

from .auth import PWUser, authenticate
from .exceptions import AuthenticationException, Bad, MailboxInconsistency, No
from .mbox import Mailbox, NoSuchMailbox
from .parse import (
    IMAPClientCommand,
    IMAPCommand,
    ListReturnOpt,
    ListSelectOpt,
    StatusAtt,
)
from .throttle import check_allow, login_failed

# Allow circular imports for annotations
#
if TYPE_CHECKING:
    from .server import IMAPClient
    from .user_server import IMAPClientProxy, IMAPUserServer

logger = logging.getLogger("asimap.client")

# Local constants
#
CAPABILITIES = (
    "IMAP4REV1",
    "IDLE",
    "ID",
    "MOVE",
    "UNSELECT",
    "UIDPLUS",
    "LITERAL+",
    "CHILDREN",
    "LIST-EXTENDED",
    "LIST-STATUS",
    "NAMESPACE",
)
SERVER_ID = {
    "name": "asimapd",
    "version": __version__,
    "vendor": "Apricot Systematic",
    "support-url": "https://github.com/scanner/asimap/issues",
    "command": "asimapd.py",
    "os": sys.platform,
}

# Client-specific capability exclusions
# Each entry defines a pattern to match against client ID strings and the
# capabilities to exclude for matching clients.
#
# NOTE: Pattern to match iOS 26 clients (iPhone, iPad, Apple Vision Pro) that
#       should not see IDLE in capabilities due to broken IDLE implementation.
#       This pattern will be refined when we have actual client ID data from
#       iOS 26.
#
# Expected client ID fields: "name", "os", "os-version", "vendor"
# Example: {"name": "Mail", "os": "iOS", "os-version": "26.0", ...}
#
# NOTE: We thought IDLE was broken on iOS 18+ Mail clients, but it turns out it
#       is fine, we are leaving this code in as an example of how we can use
#       the client ID to disable certain capabilities.
#
CLIENT_CAPABILITY_EXCLUSIONS: list[dict] = [
    # {
    #     "pattern": re.compile(
    #         r"(?:"
    #         r"(?:iPhone|iPad|Vision\s*Pro).*\b26\b"  # Device type with version 26
    #         r"|"
    #         r"\bOS\s*26\b"  # OS 26
    #         r"|"
    #         r"\biOS.*\b26\b"  # iOS with version 26
    #         r")",
    #         re.IGNORECASE,
    #     ),
    #     "excluded_capabilities": {"IDLE"},
    #     "reason": "iOS 26 broken IDLE implementation",
    # },
]

# How many seconds a command will be left to run before the client consider it
# to have taken too long to run. Basically if commands get stuck this is a
# safety measure. We should make it dynamic (so along as commands are doing
# something it lets them run.)
#
COMMAND_TIMEOUT = 120


########################################################################
########################################################################
#
# States that our IMAP client handler can be in. These reflect the valid states
# from rfc2060.
#
class ClientState(StrEnum):
    NOT_AUTHENTICATED = "not_authenticated"
    AUTHENTICATED = "authenticated"
    SELECTED = "selected"
    LOGGED_OUT = "logged_out"


##################################################################
##################################################################
#
class BaseClientHandler:
    """
    Both the pre-authenticated and authenticated client handlers operate in the
    same manner. So we provide a base class that they both extend to have
    common functionality in one place.
    """

    ##################################################################
    #
    def __init__(self, client: Union["IMAPClient", "IMAPClientProxy"]):
        """
        Arguments:
        - `client`: An asynchat.async_chat object that is connected to the IMAP
                    client we are handling. This lets us send messages to that
                    IMAP client.
        """
        self.client = client
        self.state: ClientState = ClientState.NOT_AUTHENTICATED
        self.name: str = client.name
        self.mbox: Mailbox | None = None

        self.server: IMAPUserServer | None = None

        # If the client sends its IMAP ID we record it as a dict
        #
        self.client_id: dict[str, str] | None = None

        # Set of capabilities to exclude from CAPABILITY response for this client
        # based on known client issues (e.g., iOS 26 broken IDLE implementation)
        #
        self.excluded_capabilities: set[str] = set()

        # Idling is like a sub-state. When we are idling we expect a 'DONE'
        # completion from the IMAP client before it sends us any other
        # message. However during this time the server may still send async
        # messages to the client.
        #
        self.idling: bool = False

        # This is used to keep track of the tag.. useful for when finishing an
        # DONE command when idling.
        #
        self.tag: str | None = None

        # If there are pending expunges that we need to send to the client
        # during its next command (that we can send pending expunges during)
        # they are stored here.
        #
        self.pending_notifications: list[str] = []

    ##################################################################
    #
    def update_capability_exclusions(self) -> None:
        """
        Check the client ID against known problematic client patterns and
        update the set of excluded capabilities accordingly.

        Iterates through CLIENT_CAPABILITY_EXCLUSIONS and applies any
        matching exclusion rules based on the client's ID.
        """
        if not self.client_id:
            return

        # Convert client_id dict to a searchable string
        # Check both keys and values for pattern matches
        client_id_str = " ".join(
            f"{k}:{v}" for k, v in self.client_id.items() if v
        )

        # Check each exclusion rule
        for rule in CLIENT_CAPABILITY_EXCLUSIONS:
            if rule["pattern"].search(client_id_str):
                logger.info(
                    "%s: Client ID matched exclusion pattern: %s. "
                    "Excluding capabilities: %s. Client ID: %s",
                    self.name,
                    rule["reason"],
                    ", ".join(sorted(rule["excluded_capabilities"])),
                    self.client_id,
                )
                self.excluded_capabilities.update(rule["excluded_capabilities"])

    ##################################################################
    #
    async def command(self, imap_command: IMAPClientCommand) -> None:
        """
        Process an IMAP command we received from the client.

        We use introspection to find out what IMAP commands this handler
        actually supports.

        Arguments:
        - `imap_command`: An instance parse.IMAPClientCommand
        """
        if self.mbox:
            logger.debug(
                "START: %s, '%s', '%s', '%s'",
                self.client.name,
                self.state.value,
                self.mbox.name,
                imap_command,
            )
        else:
            logger.debug(
                "START: %s, '%s', '%s'",
                self.client.name,
                self.state.value,
                imap_command,
            )
        if self.server and imap_command.command:
            self.server.num_rcvd_commands[imap_command.command] += 1

        # Since the imap command was properly parsed we know it is a valid
        # command. If it is one we support there will be a method
        # on this object of the format "do_%s" that will actually do the
        # command.
        #
        # If no such method exists then this is not a supported command.
        #
        self.tag = imap_command.tag
        if not hasattr(self, f"do_{imap_command.command}"):
            await self.client.push(
                f"{imap_command.tag} BAD Sorry, "
                f'"{imap_command.command}" is not a valid command\r\n'
            )
            return

        # Okay. The command was a known command. Process it. Each 'do_' method
        # will send any messages back to the client specific to that command
        # except the "OK" response and any exceptional errors which are handled
        # by this method.
        #
        start_time = asyncio.get_running_loop().time()
        try:
            # There may be cases where some underlying system is stuck locking
            # a folder. We are going to arbitrarily timeout out of those, but
            # we will not close the connection to our client.
            #
            # XXX For now we have a timeout of 6 minutes. We want to make this
            #     short, like 15 seconds, and pass the timeout context manager
            #     into subcontexts where it can extend it if it needs to.
            #
            async with asyncio.timeout(COMMAND_TIMEOUT) as tcm:
                imap_command.timeout_cm = tcm
                result = await getattr(self, f"do_{imap_command.command}")(
                    imap_command
                )
        except No as e:
            logger.warning(
                "%s: NO Response: '%s': %s", self.name, imap_command.qstr(), e
            )
            if self.server and imap_command.command:
                self.server.num_failed_commands[imap_command.command] += 1
            result = f"{imap_command.tag} NO {e}\r\n"
            await self.client.push(result)
            return
        except Bad as e:
            logger.warning(
                "%s: BAD Response: '%s': %r", self.name, imap_command.qstr(), e
            )
            if self.server and imap_command.command:
                self.server.num_failed_commands[imap_command.command] += 1
            result = f"{imap_command.tag} BAD {e}\r\n"
            await self.client.push(result)
            return
        except TimeoutError:
            mbox_name = self.mbox.name if self.mbox else "Not selected"
            logger.warning(
                "%s: Mailbox: '%s': command timed out: '%s'",
                self.name,
                mbox_name,
                str(imap_command),
            )
            if self.server and imap_command.command:
                self.server.num_failed_commands[imap_command.command] += 1
            result = f"{imap_command.tag} BAD Command timed out: '{imap_command.qstr()}'"
            try:
                await self.client.push(result)
            except Exception:
                pass
            return
        except ConnectionResetError as e:
            mbox_name = self.mbox.name if self.mbox else "no mailbox"
            logger.warning(
                "%s, mailbox: %s - Connection lost while doing %s: %s",
                self.name,
                mbox_name,
                str(imap_command),
                e,
            )
            raise
        except KeyboardInterrupt:
            sys.exit(0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "%s: Command '%s' failed with exception: %s",
                self.name,
                imap_command.qstr(),
                e,
            )

            if self.server and imap_command.command:
                self.server.num_failed_commands[imap_command.command] += 1
            result = f"{imap_command.tag} BAD Unhandled exception: {e}"
            try:
                await self.client.push(result.strip())
            except Exception:
                pass
            raise
        finally:
            imap_command.timeout_cm = None
            cmd_duration = asyncio.get_running_loop().time() - start_time
            if self.server and imap_command.command:
                self.server.command_durations[imap_command.command].append(
                    cmd_duration
                )

            # if cmd_duration > 1.0:
            logger.debug(
                "FINISH: %s, took %.3f seconds: '%s'",
                self.client.name,
                cmd_duration,
                imap_command.qstr(),
            )

        # If there was no result from running this command then everything went
        # okay and we send back a final 'OK' to the client for processing this
        # command.
        #
        cmd = "none" if imap_command.command is None else imap_command.command
        if result is None:
            result = (
                f"{imap_command.tag} OK {cmd.upper()} command completed\r\n"
            )
            await self.client.push(result)
        elif result is False:
            # Some commands do NOT send an OK response immediately.. aka the
            # IDLE command. If result is false
            # then we just return. We do not send a message back to our client.
            #
            return
        else:
            # The command has some specific response it wants to send back as
            # part of the tagged OK response.
            #
            result = (
                f"{imap_command.tag} OK {result} "
                f"{cmd.upper()} command completed\r\n"
            )
            await self.client.push(result)

    ####################################################################
    #
    def pending_expunges(self) -> bool:
        """
        Return True if any of the pending notifications are EXPUNGE's
        """
        return any("EXPUNGE" in x for x in self.pending_notifications)

    ##################################################################
    #
    async def send_pending_notifications(self) -> None:
        """
        Deal with pending notifications like expunges that have built up
        for this client.  This can only be called during a command, but not
        during FETCH, STORE, or SEARCH commands.

        Also we will not call this during things like 'select' or 'close'
        because they are no longer listening to the mailbox (but they will
        empty the list of pending expunges.
        """
        if self.pending_notifications:
            await self.client.push(*self.pending_notifications)
            self.pending_notifications = []

    ##################################################################
    #
    async def unceremonious_bye(self, msg: str) -> None:
        """
        Sometimes we hit a state where we can not easily recover while a client
        is connected. Frequently for clients that are in 'select' on a
        mailbox. In those cases we punt by forcibly disconnecting the client.

        With this we can usually restart whatever had problems (like a resync)
        and come out with things being proper.

        This method handles the basics of disconnecting a client.

        Arguments:
        - `msg`: The message to send to the client in the BYE.
        """
        mbox_name = None
        if self.mbox:
            mbox_name = self.mbox.name
            self.mbox.unselected(self.client.name)
            self.mbox = None

        logger.warning(
            "%s: Mailbox: '%s', Unceremonious BYE: %s",
            self.name,
            mbox_name,
            msg,
        )

        try:
            await self.client.push(f"* BYE {msg}\r\n")
            await self.client.close()

        finally:
            self.idling = False
            self.state = ClientState.LOGGED_OUT

    # The following commands are supported in any state.
    #
    ##################################################################
    #
    async def do_done(self, cmd: IMAPClientCommand | None = None) -> None:
        """
        We have gotten a DONE. This is only called when we are idling.

        Arguments:
        - `cmd`: This is ignored.
        """
        self.idling = False
        await self.send_pending_notifications()
        await self.client.push(f"{self.tag} OK IDLE terminated\r\n")
        return None

    #########################################################################
    #
    async def do_noop(self, cmd: IMAPClientCommand) -> None:
        """
        Waits for the mailbox to let the command proceed and then sends any
        pending notifies this client may have, if this client has a selected
        mailbox.

        Arguments:
        - `cmd`: The full IMAP command object.
        """
        if self.mbox:
            async with cmd.ready_and_okay(self.mbox):
                await self.send_pending_notifications()
        return None

    #########################################################################
    #
    async def do_capability(self, cmd: IMAPClientCommand) -> None:
        """
        Return the capabilities of this server.

        Arguments:
        - `cmd`: The full IMAP command object.
        """
        await self.send_pending_notifications()

        # Filter out any excluded capabilities for this client
        capabilities = [
            cap for cap in CAPABILITIES if cap not in self.excluded_capabilities
        ]

        await self.client.push(f"* CAPABILITY {' '.join(capabilities)}\r\n")
        return None

    #########################################################################
    #
    async def do_namespace(self, cmd: IMAPClientCommand) -> None:
        """
        We currently only support a single personal name space. There is no
        leading prefix on personal mailboxes and '/' is the hierarchy
        delimiter.

        Arguments:
        - `cmd`: The full IMAP command object.
        """
        await self.send_pending_notifications()
        await self.client.push('* NAMESPACE (("" "/")) NIL NIL\r\n')
        return None

    #########################################################################
    #
    async def do_id(self, cmd: IMAPClientCommand) -> None:
        """
        Construct an ID response... uh.. lookup the rfc that defines this.

        Arguments:
        - `cmd`: The full IMAP command object.
        """
        await self.send_pending_notifications()
        self.client_id = cmd.id_dict

        # Check if this client should have any capabilities excluded
        self.update_capability_exclusions()

        res = " ".join([f'"{k}" "{v}"' for k, v in SERVER_ID.items()])
        await self.client.push(f"* ID ({res})\r\n")
        return None

    #########################################################################
    #
    async def do_idle(self, cmd: IMAPClientCommand) -> bool:
        """
        The idle command causes the server to wait until the client sends
        us a 'DONE' continuation. During that time the client can not send
        any commands to the server. However, the client can still get
        asynchronous messages from the server.

        Arguments:
        - `cmd`: The full IMAP command object.
        """
        # Because this is a blocking command the main server read-loop
        # for this connection is not going to hit the read() again
        # until this thread exits. In here we send a "+\r\n" to the client
        # indicating that we are now waiting for its continuation. We
        # then block reading on the connection. When we get a line
        # of input, if it is "DONE" then we complete this command
        # If it is any other input we raise a bad syntax error.
        #
        await self.client.push("+ idling\r\n")
        await self.send_pending_notifications()
        self.idling = True
        return False

    #########################################################################
    #
    async def do_logout(self, cmd: IMAPClientCommand) -> None:
        """
        This just sets our state to 'logged out'. Our caller will take the
        appropriate actions to finishing a client's log out request.

        Arguments:
        - `cmd`: The full IMAP command object.
        """
        self.pending_notifications = []
        await self.client.push(
            "* BYE Logging out of asimap server. Good bye.\r\n"
        )
        self.state = ClientState.LOGGED_OUT
        return None


##################################################################
##################################################################
#
class PreAuthenticated(BaseClientHandler):
    """
    This handles the server-side state for an IMAP client when they
    are in the states before they have successfully authenticated to
    the IMAP server.

    NOTE: This is the class used by the main server to handle an IMAP
          client as it authenticates. It does not handle any IMAP
          commands after the client enters the authenticated
          state. All of those are handled by the subprocess instation
          of the Authenticated class
    """

    ##################################################################
    #
    def __init__(self, client: "IMAPClient"):
        """ """
        super().__init__(client)
        self.user: PWUser | None = None
        return

    # The following commands are supported in the non-authenticated state.
    #

    ##################################################################
    #
    async def do_authenticated(self, cmd: IMAPClientCommand) -> None:
        """
        We do not support any authentication mechanisms at this time.. just
        password authentication via the 'login' IMAP client command.

        Arguments:
        - `cmd`: The full IMAP command object.
        """
        await self.send_pending_notifications()
        if self.state == ClientState.AUTHENTICATED:
            raise Bad("client already is in the authenticated state")
        raise No("unsupported authentication mechanism")

    ##################################################################
    #
    async def do_login(self, cmd: IMAPClientCommand) -> None:
        """
        Process a LOGIN command with a username and password from the IMAP
        client.

        Arguments:
        - `cmd`: The full IMAP command object.
        """
        # If this client has been trying to log in too often with failed
        # results then we are going to throttle them and not accept their
        # attempt to login.
        #
        if not check_allow(cmd.user_name, self.client.rem_addr):
            # Sleep for a bit before we return this failure. If they are
            # failing too often then we provide a bit of a quagmire slowing
            # down our responses to them.
            #
            logger.info(
                "%s: not allowed. Delaying response 10s",
                self.name,
            )
            await asyncio.sleep(10)
            raise Bad("Too many authentication failures")

        # XXX This should poke the authentication mechanism we were passed
        #     to see if the user authenticated properly, and if they did
        #     determine what the path to the user's mailspool is.
        #
        #     But for our first test we are going to accept a test user
        #     and password.
        #
        await self.send_pending_notifications()
        if self.state == ClientState.AUTHENTICATED:
            logger.info(
                "%s: client already is in the authenticated state", self.name
            )
            raise Bad("client already is in the authenticated state")

        try:
            self.user = await authenticate(cmd.user_name, cmd.password)

            # Even if the user authenticates properly, we can not allow them to
            # login if they have no maildir.
            #
            if not (self.user.maildir.exists() and self.user.maildir.is_dir()):
                logger.error(
                    "%s: Either '%s' does not exist, or it is not a directory.",
                    self.name,
                    str(self.user.maildir),
                )
                raise No("You have no mailbox directory setup")

            self.state = ClientState.AUTHENTICATED
            logger.info(
                "%s logged in from %s", str(self.user), self.client.name
            )
        except AuthenticationException as e:
            # Record this failed authentication attempt
            #
            logger.warning(
                "%s: login failed (attempt from %s)",
                cmd.user_name,
                self.client.name,
            )
            login_failed(cmd.user_name, self.client.rem_addr)
            raise No(str(e)) from e
        return None


##################################################################
##################################################################
#
# XXX Should rename this "AuthenticatedClient" or something because just
#     "Authenticated" in other modules is a bit information lacking.
#
class Authenticated(BaseClientHandler):
    """
    The 'authenticated' client IMAP command handler. Basically this handles all
    of the IMAP messages from the IMAP client when they have authenticated and
    we are running in the user_server subprocess.

    This is basically the main command dispatcher for pretty much everything
    that the IMAP client is going to do.
    """

    ##################################################################
    #
    def __init__(
        self, client: "IMAPClientProxy", user_server: "IMAPUserServer"
    ):
        super().__init__(client)
        self.server = user_server
        self.mbox = None
        self.state = ClientState.AUTHENTICATED
        self.examine = False  # If a mailbox is selected in 'examine' mode

        # How many times has this client done a FETCH when there are pending
        # expunges? We track this so that when a client gets in a snit about
        # this instead of sending No's we will just disconnect them. They
        # should reconnect and when they do they should be able to fix the
        # state of the mailbox.
        #
        # NOTE: This is because we cannot send `EXPUNGES` during a non-UID
        # fetch:
        #
        #        An EXPUNGE response MUST NOT be sent when no command is in
        #        progress, nor while responding to a FETCH, STORE, or SEARCH
        #        command.  This rule is necessary to prevent a loss of
        #        synchronization of message sequence numbers between client and
        #        server.
        #
        self.fetch_while_pending_count = 0

        # How many times this client has done a 'SELECT' while already having
        # selected the folder that it wants to 'SELECT'. Apple's Mail.app every
        # now and then goes in to spazzes where it spends 30 minutes spamming
        # "SELECT" on the same connection over and over again. It is like it
        # does not see the response.
        #
        # If they do it too often in we will forcibly disconnect the client.
        #
        self.select_while_selected_count = 0

    #########################################################################
    #
    async def do_authenticate(self, cmd: IMAPClientCommand) -> None:
        await self.send_pending_notifications()
        raise No("client already is in the authenticated state")

    #########################################################################
    #
    async def do_login(self, cmd: IMAPClientCommand) -> None:
        await self.send_pending_notifications()
        raise No("client already is in the authenticated state")

    ##################################################################
    #
    async def do_select(
        self,
        cmd: IMAPClientCommand,
        examine: bool = False,
    ) -> str | None:
        """
        Select a folder, enter in to 'selected' mode.

        Arguments:
        - `cmd`: The IMAP command we are executing
        - `examine`: Opens the folder in read only mode if True
        """
        # Selecting a mailbox, even if the attempt fails, automatically
        # deselects any already selected mailbox.
        #
        self.pending_notifications = []
        self.idling = False
        if self.state == ClientState.SELECTED:
            self.state = ClientState.AUTHENTICATED
            if self.mbox:
                # Check if they are spamming SELECT. If they are, then dismiss
                # them.
                #
                if self.mbox.name == cmd.mailbox_name:
                    self.select_while_selected_count += 1
                    if self.select_while_selected_count > 10:
                        logger.warning(
                            "%s, mailbox: '%s': excessive selects while "
                            "selected, count: %d",
                            self.client.name,
                            self.mbox.name,
                            self.select_while_selected_count,
                        )
                        self.select_while_selected_count = 0
                        await self.unceremonious_bye(
                            "You are SELECT'ing the same mailbox too often."
                        )
                        return None

                else:
                    self.select_while_selected_count = 0

                # Otherwise, unselect the selected mailbox and move on.
                #
                self.mbox.unselected(self.client.name)
                self.mbox = None

        # Note the 'selected()' method may fail with an exception and
        # we should not set our state or the mailbox we have selected
        # until 'selected()' returns without a failure.
        #
        assert self.server
        mbox = await self.server.get_mailbox(cmd.mailbox_name)
        async with cmd.ready_and_okay(mbox):
            msgs = await mbox.selected(self)
            await self.client.push(*msgs)
            self.mbox = mbox
            self.state = ClientState.SELECTED
            self.examine = examine
            if self.examine:
                return "[READ-ONLY]"
            return "[READ-WRITE]"

    ##################################################################
    #
    async def do_unselect(self, cmd: IMAPClientCommand) -> None:
        """
        Unselect a mailbox. Similar to close, except it does not do an expunge.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        # NOTE: If this client currently has messages being processed in the
        # command queue for this mailbox then they are all tossed when they
        # pre-emptively select another mailbox (this could cause the client
        # some heartburn as commands they issues will never get their final
        # message.. but that is their problem.)
        #
        if self.state != ClientState.SELECTED:
            logger.warning("Attempting to UNSELECT while not SELECTED")
            # raise No("Client must be in the selected state")
            raise Bad("Client must be in the selected state")

        self.select_while_selected_count = 0
        if self.mbox:
            self.mbox.unselected(self.client.name)
            self.mbox = None
        self.pending_notifications = []
        self.idling = False
        self.state = ClientState.AUTHENTICATED

    #########################################################################
    #
    async def do_examine(self, cmd: IMAPClientCommand) -> str | None:
        """
        examine a specific mailbox (just like select, but read only)
        """
        return await self.do_select(cmd, examine=True)

    ##################################################################
    #
    async def do_create(self, cmd: IMAPClientCommand) -> None:
        """
        Create the specified mailbox.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        await self.send_pending_notifications()
        assert self.server
        await Mailbox.create(cmd.mailbox_name, self.server)
        cmd.completed = True

    ##################################################################
    #
    async def do_delete(self, cmd: IMAPClientCommand) -> None:
        """
        Delete the specified mailbox.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        assert self.server
        await self.send_pending_notifications()
        mbox = await self.server.get_mailbox(cmd.mailbox_name)
        async with cmd.ready_and_okay(mbox):
            await Mailbox.delete(cmd.mailbox_name, self.server)

    ##################################################################
    #
    async def do_rename(self, cmd: IMAPClientCommand) -> None:
        """
        Renames a mailbox from one name to another.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        await self.send_pending_notifications()
        assert self.server
        mbox = await self.server.get_mailbox(cmd.mailbox_src_name)
        async with cmd.ready_and_okay(mbox):
            await Mailbox.rename(
                cmd.mailbox_src_name, cmd.mailbox_dst_name, self.server
            )
        await self.send_pending_notifications()

    ##################################################################
    #
    async def do_subscribe(self, cmd: IMAPClientCommand) -> None:
        """
        The SUBSCRIBE command adds the specified mailbox name to the
        server's set of "active" or "subscribed" mailboxes as returned by
        the LSUB command.  This command returns a tagged OK response only
        if the subscription is successful.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        await self.send_pending_notifications()

        assert self.server
        mbox = await self.server.get_mailbox(cmd.mailbox_name)
        mbox.subscribed = True
        await mbox.commit_to_db()

    ##################################################################
    #
    async def do_unsubscribe(self, cmd: IMAPClientCommand) -> None:
        """
        The UNSUBSCRIBE command removes the specified mailbox name
        from the server's set of "active" or "subscribed" mailboxes as
        returned by the LSUB command.  This command returns a tagged
        OK response only if the unsubscription is successful.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        await self.send_pending_notifications()

        assert self.server
        mbox = await self.server.get_mailbox(cmd.mailbox_name)
        mbox.subscribed = False
        await mbox.commit_to_db()

    ##################################################################
    #
    @staticmethod
    def _fmt_list_response(
        mbox_name: str,
        attributes: set[str],
        child_info: set[str] | None,
    ) -> str:
        """
        Format a single ``* LIST`` response line per RFC 3501 / RFC 5258.

        Returns the complete untagged response including the trailing
        CRLF.  When *child_info* is non-empty, an RFC 5258 CHILDINFO
        extended data item is appended after the mailbox name::

            * LIST (\\HasChildren) "/" "projects" ("CHILDINFO" ("SUBSCRIBED"))
        """
        attrs_str = " ".join(sorted(attributes))
        line = f'* LIST ({attrs_str}) "/" "{mbox_name}"'
        if child_info:
            criteria = " ".join(f'"{c}"' for c in sorted(child_info))
            line += f' ("CHILDINFO" ({criteria}))'
        return line + "\r\n"

    ####################################################################
    #
    async def _compute_status_for_list(
        self,
        mbox_name: str,
        status_atts: list[StatusAtt],
    ) -> str | None:
        """
        Compute a ``* STATUS`` response line for *mbox_name* from the
        in-memory ``Mailbox`` object.  Used by LIST-STATUS (RFC 5819).

        All mailboxes are already loaded in memory by the user server,
        so ``get_mailbox()`` returns the cached instance cheaply.

        Returns the formatted response line (with trailing CRLF), or
        ``None`` if the mailbox cannot be found.
        """
        assert self.server
        try:
            mbox = await self.server.get_mailbox(mbox_name)
        except NoSuchMailbox:
            return None

        result: list[str] = []
        for att in status_atts:
            match att:
                case StatusAtt.MESSAGES:
                    result.append(f"MESSAGES {mbox.num_msgs}")
                case StatusAtt.RECENT:
                    result.append(f"RECENT {mbox.num_recent}")
                case StatusAtt.UIDNEXT:
                    result.append(f"UIDNEXT {mbox.next_uid}")
                case StatusAtt.UIDVALIDITY:
                    result.append(f"UIDVALIDITY {mbox.uid_vv}")
                case StatusAtt.UNSEEN:
                    result.append(f"UNSEEN {len(mbox.sequences['unseen'])}")

        return f'* STATUS "{mbox_name}" ({" ".join(result)})\r\n'

    ####################################################################
    #
    async def do_list(self, cmd: IMAPClientCommand, lsub: bool = False) -> None:
        """
        Handle the LIST command (RFC 3501) with RFC 5258 LIST-EXTENDED
        and return-option support.

        In extended mode (selection options or multiple patterns present)
        the parsed command's ``list_select_opts`` and ``list_patterns``
        are forwarded to ``Mailbox.list()`` and the response may include
        extended data items (CHILDINFO).

        Arguments:
        - `cmd`: The IMAP command we are executing
        - `lsub`: If True this will only match folders that have their
          subscribed bit set (legacy LSUB command).
        """
        await self.send_pending_notifications()

        extended = bool(cmd.list_select_opts or cmd.list_patterns)
        if extended or cmd.list_return_opts:
            logger.debug(
                "LIST%s: select_opts=%s, patterns=%s, return_opts=%s, "
                "status_atts=%s",
                "-EXTENDED" if extended else "",
                cmd.list_select_opts,
                cmd.list_patterns,
                cmd.list_return_opts,
                cmd.list_status_atts,
            )

        # Handle the special case where the client is probing for the
        # hierarchy separation character.  In extended mode (RFC 5258)
        # this probe is not defined — return nothing.
        #
        # NOTE: when ``list_patterns`` is populated the patterns live
        # there and ``list_mailbox`` is empty, so we must also check
        # that no patterns were provided.
        #
        if (
            cmd.mailbox_name == ""
            and cmd.list_mailbox == ""
            and not cmd.list_patterns
        ):
            if not extended:
                await self.client.push(r'* LIST (\Noselect) "/" ""' + "\r\n")
            return

        assert self.server

        # Collect all results first so we can fix \HasChildren /
        # \HasNoChildren attributes based on which folders are
        # actually present (the cached attributes can be stale).
        #
        results: list[tuple[str, set[str], set[str] | None]] = []
        async for mbox_name, attributes, child_info in Mailbox.list(
            cmd.mailbox_name,
            cmd.list_mailbox,
            self.server,
            lsub,
            select_opts=cmd.list_select_opts or None,
            patterns=cmd.list_patterns or None,
        ):
            # Skip the root "" entry from LIST results.  It is not a
            # real mailbox and confuses strict IMAP clients (iOS 18+).
            #
            if mbox_name == "":
                continue
            mbox_name = "INBOX" if mbox_name.lower() == "inbox" else mbox_name
            results.append((mbox_name, attributes, child_info))

        # SUBSCRIBED return option: when the client asked for
        # \Subscribed attributes via RETURN but did NOT use SUBSCRIBED
        # as a selection filter, Mailbox.list() won't have annotated
        # the results.  Look up subscription status and annotate here.
        #
        if (
            ListReturnOpt.SUBSCRIBED in cmd.list_return_opts
            and ListSelectOpt.SUBSCRIBED not in cmd.list_select_opts
        ):
            for mbox_name, attributes, _ in results:
                if r"\Subscribed" not in attributes:
                    db_name = (
                        mbox_name.lower() if mbox_name == "INBOX" else mbox_name
                    )
                    row = await self.server.db.fetchone(
                        "SELECT subscribed FROM mailboxes WHERE name=?",
                        (db_name,),
                    )
                    if row and row[0]:
                        attributes.add(r"\Subscribed")

        # Build a set of all returned folder names so we can verify
        # \HasChildren / \HasNoChildren correctness.
        #
        all_names = {name for name, _, _ in results}
        for mbox_name, attributes, child_info in results:
            has_children = any(n.startswith(mbox_name + "/") for n in all_names)
            if has_children:
                attributes.discard(r"\HasNoChildren")
                attributes.add(r"\HasChildren")
            else:
                attributes.discard(r"\HasChildren")
                attributes.add(r"\HasNoChildren")

            if lsub:
                attrs_str = " ".join(sorted(attributes))
                msg = f'* LSUB ({attrs_str}) "/" "{mbox_name}"\r\n'
            else:
                msg = self._fmt_list_response(mbox_name, attributes, child_info)
            await self.client.push(msg)

            # RFC 5819 LIST-STATUS: after each selectable LIST entry
            # that is not a RECURSIVEMATCH-only result, emit a
            # * STATUS response.  Skip \Noselect mailboxes and
            # RECURSIVEMATCH entries (child_info is not None).
            #
            if (
                ListReturnOpt.STATUS in cmd.list_return_opts
                and cmd.list_status_atts
                and r"\Noselect" not in attributes
                and child_info is None
            ):
                status_line = await self._compute_status_for_list(
                    mbox_name, cmd.list_status_atts
                )
                if status_line:
                    await self.client.push(status_line)

    ####################################################################
    #
    async def do_lsub(self, cmd: IMAPClientCommand) -> None:
        """
        The lsub command lists mailboxes we are subscribed to with the
        'SUBSCRIBE' command.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        return await self.do_list(cmd, lsub=True)

    ##################################################################
    #
    async def do_status(self, cmd: IMAPClientCommand) -> None:
        """
        Get the designated mailbox and return the requested status
        attributes to our client.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        await self.send_pending_notifications()

        assert self.server
        result: list[str] = []
        mbox = await self.server.get_mailbox(cmd.mailbox_name)
        async with cmd.ready_and_okay(mbox):
            for att in cmd.status_att_list:
                match att:
                    case StatusAtt.MESSAGES:
                        result.append(f"MESSAGES {mbox.num_msgs}")
                    case StatusAtt.RECENT:
                        result.append(f"RECENT {mbox.num_recent}")
                    case StatusAtt.UIDNEXT:
                        result.append(f"UIDNEXT {mbox.next_uid}")
                    case StatusAtt.UIDVALIDITY:
                        result.append(f"UIDVALIDITY {mbox.uid_vv}")
                    case StatusAtt.UNSEEN:
                        result.append(f"UNSEEN {len(mbox.sequences['unseen'])}")

        await self.client.push(
            f'* STATUS "{cmd.mailbox_name}" ({" ".join(result)})\r\n'
        )

    ##################################################################
    #
    async def do_append(self, cmd: IMAPClientCommand) -> str:
        """
        Append a message to a mailbox.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        assert self.server
        await self.send_pending_notifications()

        try:
            mbox = await self.server.get_mailbox(cmd.mailbox_name)
            async with cmd.ready_and_okay(mbox):
                uid = await mbox.append(
                    cmd.message, cmd.flag_list, cmd.date_time
                )
        except NoSuchMailbox as exc:
            # For APPEND and COPY if the mailbox does not exist we
            # MUST supply the TRYCREATE flag so we catch the generic
            # exception and return the appropriate NO result.
            #
            raise No(
                f"[TRYCREATE] No such mailbox: '{cmd.mailbox_name}'"
            ) from exc
        await self.send_pending_notifications()
        return f"[APPENDUID {mbox.uid_vv} {uid}]"

    ##################################################################
    #
    async def do_check(self, cmd: IMAPClientCommand) -> None:
        """
        state: must be selected

        Do a 'checkpoint' of the currently selected mailbox. Basically
        this means for us we just do a resync.

        This may cause messages to be generated but this is
        okay. Clients should be prepared for that (but they should not
        expect this to happen.)

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != ClientState.SELECTED:
            raise No("Client must be in the selected state")

        # If self.mbox is None then this mailbox was deleted while this user
        # had it selected. In that case we disconnect the user and let them
        # reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            await self.unceremonious_bye(
                "Your selected mailbox no longer exists"
            )
            return

        await self.send_pending_notifications()

        # This forces a checkpoint, which is essentially done on the next
        # resync. Force the next resync by setting `optional_resync` to
        # False.  Then wait for the mbox to let this command
        # execute. Because `CHECK` must execute when no other commands
        # running, a resync will be running before control passes back to
        # this function.
        #
        self.mbox.optional_resync = False
        async with cmd.ready_and_okay(self.mbox):
            pass
        await self.send_pending_notifications()

    ##################################################################
    #
    async def do_close(self, cmd: IMAPClientCommand) -> None:
        """
        state: must be selected

        The CLOSE command permanently removes all messages that have
        the Deleted flag set from the currently selected mailbox, and
        returns to the authenticated state from the selected state.
        No untagged EXPUNGE responses are sent.

        No messages are removed, and no error is given, if the mailbox is
        selected by an EXAMINE command or is otherwise selected read-only.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != ClientState.SELECTED:
            raise No("Client must be in the selected state")

        # We allow for the mailbox to be deleted.. it has no effect on this
        # operation.
        #
        self.pending_notifications = []
        self.state = ClientState.AUTHENTICATED

        if not self.mbox:
            return

        mbox = self.mbox
        self.mbox.unselected(self.name)
        self.mbox = None

        # If the mailbox was selected via 'examine' then closing the
        # mailbox does NOT do a purge of all messages marked with '\Delete'
        #
        if self.examine:
            return

        # Otherwise closing the mailbox (unlike doing a 'select' 'examine'
        # or 'logout') will perform an expunge (just no messages will be
        # sent to this client.) We pass no client parameter so the expunge
        # does its work 'silently.'
        #
        # Do an EXPUNGE only if there are any messages marked 'Delete'
        #
        if mbox.sequences.get("Deleted", []):
            async with cmd.ready_and_okay(mbox):
                await mbox.expunge()

    ##################################################################
    #
    async def do_expunge(self, cmd: IMAPClientCommand) -> None:
        """
        Delete all messages marked with 'Delete' from the mailbox and send out
        untagged expunge messages...

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != ClientState.SELECTED:
            raise No("Client must be in the selected state")

        # If self.mbox is None then this mailbox was deleted while this user
        # had it selected. In that case we disconnect the user and let them
        # reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            await self.unceremonious_bye(
                "Your selected mailbox no longer exists"
            )
            return

        await self.send_pending_notifications()

        # If we selected the mailbox via 'examine' then we can not make any
        # changes anyways...
        #
        if self.examine:
            return

        # In order for the EXPUNGE operation to immediately send EXPUNGE
        # messages to this client we will do a bit of a hack and indicate
        # that this client is "idling" while the operation is running.
        #
        try:
            idling = self.idling
            self.idling = True
            async with cmd.ready_and_okay(self.mbox):
                # Do an EXPUNGE if there are any messages marked 'Delete'
                #
                if self.mbox.sequences.get("Deleted", []):
                    uid_msg_set = (
                        list(cmd.msg_set_as_set)
                        if cmd.uid_command and cmd.msg_set_as_set
                        else None
                    )
                    await self.mbox.expunge(uid_msg_set=uid_msg_set)
        finally:
            self.idling = idling

    ##################################################################
    #
    async def do_search(self, cmd: IMAPClientCommand) -> None:
        """
        Search... NOTE: Can not send untagged EXPUNGE messages during this
        command.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != ClientState.SELECTED:
            raise No("Client must be in the selected state")

        # If self.mbox is None then this mailbox was likely deleted while this
        # user had it selected. In that case we disconnect the user and let
        # them reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            await self.unceremonious_bye(
                "Your selected mailbox no longer exists"
            )
            return

        # If this client has pending notifications messages then we return
        # a tagged No response.. the client should see this and do a NOOP
        # or such and receive the pending expunges. Unless this is a UID
        # command. It is okay to send pending expunges during the
        # operations of a UID SEARCH.
        #
        if self.pending_expunges():
            if cmd.uid_command:
                await self.send_pending_notifications()
            else:
                raise No("There are pending untagged responses")

        async with cmd.ready_and_okay(self.mbox):
            try:
                results = await self.mbox.search(
                    cmd.search_key, cmd.uid_command, cmd.timeout_cm
                )
                await self.client.push(
                    f"* SEARCH {' '.join(str(x) for x in results)}\r\n"
                )
            except MailboxInconsistency as e:
                self.optional_resync = False
                self.full_search = True
                logger.warning("Mailbox '%s': %s", self.mbox.name, str(e))

    ##################################################################
    #
    async def do_fetch(self, cmd: IMAPClientCommand) -> None:
        """
        Fetch data from the messages indicated in the command.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != ClientState.SELECTED:
            raise No("Client must be in the selected state")

        # If self.mbox is None then this mailbox was deleted while this user
        # had it selected. In that case we disconnect the user and let them
        # reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            await self.unceremonious_bye(
                "Your selected mailbox no longer exists"
            )
            return

        # If this client has pending EXPUNGE messages then we return a
        # tagged No response.. the client should see this and do a NOOP or
        # such and receive the pending expunges. Unless this is a UID
        # command. It is okay to send pending expunges during the
        # operations of a UID FETCH.
        #
        if self.pending_expunges():
            if cmd.uid_command:
                await self.send_pending_notifications()
            else:
                # If a client continues to pound us asking for FETCH's when
                # there are pending EXPUNGE's give them the finger by
                # forcing them to disconnect. It is obvious watching
                # Mail.app that it will not give up when given a No so we
                # punt this connection of theirs. They should reconnect and
                # learn the error of their ways.
                #
                self.fetch_while_pending_count += 1
                if self.fetch_while_pending_count > 3:
                    await self.unceremonious_bye("You have pending EXPUNGEs.")
                    return
                else:
                    raise No("There are pending EXPUNGEs.")
        else:
            await self.send_pending_notifications()

        self.fetch_while_pending_count = 0
        try:
            async with cmd.ready_and_okay(self.mbox):
                msg_set = (
                    sorted(cmd.msg_set_as_set) if cmd.msg_set_as_set else []
                )
                async for idx, results in self.mbox.fetch(
                    msg_set, cmd.fetch_atts, cmd.uid_command, cmd.timeout_cm
                ):
                    msg = b"* %(idx)d FETCH (%(results)b)\r\n" % {
                        b"idx": idx,
                        b"results": b" ".join(results),
                    }
                    await self.client.push(msg)
        except MailboxInconsistency as exc:
            self.mbox.optional_resync = False
            logger.exception(
                "%s: Mailbox '%s', failure during fetch: %s",
                self.name,
                self.mbox.name,
                exc,
            )
            raise Bad(f"Problem while fetching: {exc}") from exc

        # The FETCH may have caused some message flags to change, and they may
        # not have been in the FETCH responses we already sent (and other
        # FETCH's may have done the same) so make sure we send out all pending
        # notifications.
        #
        await self.send_pending_notifications()

    ##################################################################
    #
    async def do_store(self, cmd: IMAPClientCommand) -> None:
        """
        The STORE command alters data associated with a message in the
        mailbox.  Normally, STORE will return the updated value of the
        data with an untagged FETCH response.  A suffix of ".SILENT" in
        the data item name prevents the untagged FETCH, and the server
        SHOULD assume that the client has determined the updated value
        itself or does not care about the updated value.

        By data we mean the flags on a message.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        if self.state != ClientState.SELECTED:
            raise No("Client must be in the selected state")

        # If self.mbox is None then this mailbox was deleted while this user
        # had it selected. In that case we disconnect the user and let them
        # reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            await self.unceremonious_bye(
                "Your selected mailbox no longer exists"
            )
            return

        # If this client has pending EXPUNGE messages then we return a
        # tagged No response.. the client should see this and do a NOOP or
        # such and receive the pending expunges.  Unless this is a UID
        # command. It is okay to send pending expunges during the
        # operations of a UID FETCH.
        #
        if self.pending_expunges():
            if cmd.uid_command:
                await self.send_pending_notifications()
            else:
                raise No("There are pending EXPUNGEs.")
        else:
            await self.send_pending_notifications()

        # We do not issue any messages to the client here. This is done
        # automatically when 'resync' is called because resync will examine
        # the in-memory copy of the sequences with what is on disk and if
        # there are differences issue FETCH messages for each message with
        # different flags.
        #
        # Unless 'SILENT' was set in which case we still notify all other
        # clients listening to this mailbox, but not this client.
        #
        # XXX We can use the 'client is idling' trick here for sending the
        #     updated flags via fetches instead of the 'dont_notify=self'
        #     stuff here.
        #
        try:
            async with cmd.ready_and_okay(self.mbox):
                msg_set = (
                    sorted(cmd.msg_set_as_set) if cmd.msg_set_as_set else []
                )
                fetch_notifications = await self.mbox.store(
                    msg_set,
                    cmd.store_action,
                    cmd.flag_list,
                    cmd.uid_command,
                    dont_notify=self,
                )
        except MailboxInconsistency as exc:
            # Force a resync of this mailbox. Likely something was fiddling
            # messages directly (an nmh command run from the command line)
            # and what the mbox thinks the internal state is does not
            # actually match the state of the folder.
            #
            self.optional_resync = False
            self.full_search = True
            raise Bad(f"Problem while storing: {exc}") from exc

        # IF this is not a "SILENT" store, we will send the FETCH messages
        # that were generated due to the actions of this STORE
        # command. (All the other clients will have gotten untagged FETCH
        # messages when the resync was completed by the management task.)
        #
        if not cmd.silent:
            await self.client.push(*fetch_notifications)

    ##################################################################
    #
    async def do_copy(self, cmd: IMAPClientCommand) -> str | None:
        """
        Copy the given set of messages to the destination mailbox.

        NOTE: Causes a resync of the destination mailbox.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        assert self.server
        if self.state != ClientState.SELECTED:
            raise No("Client must be in the selected state")

        # If self.mbox is None then this mailbox was deleted while this user
        # had it selected. In that case we disconnect the user and let them
        # reconnect and relearn mailbox state.
        #
        if self.mbox is None:
            await self.unceremonious_bye(
                "Your selected mailbox no longer exists"
            )
            return None

        await self.send_pending_notifications()

        # Wait until the mailbox gives us the go-ahead to run the command.
        #
        async with cmd.ready_and_okay(self.mbox):
            try:
                dest_mbox = await self.server.get_mailbox(cmd.mailbox_name)
                src_uids, dst_uids = await self.mbox.copy(
                    cmd.msg_set,
                    dest_mbox,
                    cmd.uid_command,
                    imap_cmd=cmd,
                )
            except NoSuchMailbox as exc:
                # For APPEND and COPY if the mailbox does not exist we
                # MUST supply the TRYCREATE flag so we catch the generic
                # exception and return the appropriate NO result.
                #
                raise No(
                    f"[TRYCREATE] No such mailbox: '{cmd.mailbox_name}'"
                ) from exc

        return self._format_copyuid(
            dest_mbox,
            [u for u in src_uids if u is not None],
            [u for u in dst_uids if u is not None],
        )

    ##################################################################
    #
    async def do_move(self, cmd: IMAPClientCommand) -> str | None:
        """
        Move the given set of messages to the destination mailbox (RFC 6851).

        This is an atomic COPY + expunge of the source messages. The source
        messages are removed without requiring the \\Deleted flag.

        To avoid deadlocks we do NOT hold the source mailbox while writing
        to the destination. Instead we follow the same pattern as `copy()`:
        read from source, release source, write to destination. Then we
        re-acquire the source for the expunge phase using the UIDs returned
        by copy().

        RFC 6851 states that MOVE takes "no more and no less risk" than the
        traditional COPY + STORE \\Deleted + EXPUNGE sequence, so this small
        window between copy and expunge is acceptable.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        assert self.server
        if self.state != ClientState.SELECTED:
            raise No("Client must be in the selected state")

        if self.mbox is None:
            await self.unceremonious_bye(
                "Your selected mailbox no longer exists"
            )
            return None

        if self.examine:
            raise No("Mailbox is read-only")

        await self.send_pending_notifications()

        # Phase 1: Copy messages to the destination mailbox.
        #
        # We pass `imap_cmd=cmd` so that copy() releases the source
        # mailbox after reading (before writing to destination). This
        # avoids holding both source and destination locks simultaneously,
        # which could deadlock if two clients MOVE between the same pair
        # of mailboxes in opposite directions.
        #
        async with cmd.ready_and_okay(self.mbox):
            try:
                dest_mbox = await self.server.get_mailbox(cmd.mailbox_name)
                src_uids, dst_uids = await self.mbox.copy(
                    cmd.msg_set,
                    dest_mbox,
                    cmd.uid_command,
                    imap_cmd=cmd,
                )
            except NoSuchMailbox as exc:
                raise No(
                    f"[TRYCREATE] No such mailbox: '{cmd.mailbox_name}'"
                ) from exc

        # Phase 2: Send untagged OK with COPYUID before EXPUNGE
        # notifications (required by RFC 6851).
        #
        src_uid_list = [u for u in src_uids if u is not None]
        dst_uid_list = [u for u in dst_uids if u is not None]
        copyuid = self._format_copyuid(dest_mbox, src_uid_list, dst_uid_list)
        await self.client.push(f"* OK {copyuid}\r\n")

        # Phase 3: Re-acquire the source mailbox and expunge the moved
        # messages by their UIDs, regardless of the Deleted sequence.
        #
        # We use a phony EXPUNGE command to go through the management
        # task queue (same pattern copy() uses for the destination).
        # We use the idling hack so EXPUNGE notifications are delivered
        # immediately to this client.
        #
        expunge_cmd = IMAPClientCommand("A001 EXPUNGE")
        expunge_cmd.command = IMAPCommand.EXPUNGE
        try:
            idling = self.idling
            self.idling = True
            async with expunge_cmd.ready_and_okay(self.mbox):
                await self.mbox.expunge(
                    uid_msg_set=src_uid_list,
                    check_deleted=False,
                )
        finally:
            self.idling = idling

        return None

    ##################################################################
    #
    def _format_copyuid(
        self,
        dest_mbox: Mailbox,
        src_uids: list[int],
        dst_uids: list[int],
    ) -> str:
        """
        Format a COPYUID response code from source and destination UID lists.

        Compresses consecutive UIDs into ranges (e.g., [1,2,3,5] -> "1:3,5").

        NOTE: I tip my hat to: http://stackoverflow.com/questions/3429510/
        pythonic-way-to-convert-a-list-of-integers-into-a-string-of-
        comma-separated-range/3430231#3430231

        Arguments:
        - `dest_mbox`: The destination mailbox
        - `src_uids`: List of source UIDs
        - `dst_uids`: List of destination UIDs

        Returns:
            A string like "[COPYUID <uidvalidity> <src_uids> <dst_uids>]"
        """
        try:
            new_src_uids = [
                list(x)
                for _, x in groupby(src_uids, lambda x, c=count(): next(c) - x)  # type: ignore[misc]
            ]
            str_src_uids = ",".join(
                ":".join(map(str, (g[0], g[-1])[: len(g)]))
                for g in new_src_uids
            )
            new_dst_uids = [
                list(x)
                for _, x in groupby(dst_uids, lambda x, c=count(): next(c) - x)  # type: ignore[misc]
            ]
            str_dst_uids = ",".join(
                ":".join(map(str, (g[0], g[-1])[: len(g)]))
                for g in new_dst_uids
            )
        except Exception as e:
            logger.error(
                "Unable to generate src and dst uid lists. Source mailbox: "
                "%s, dest mailbox: %s Exception: %s, src_uids: %s, dst_uids: "
                "%s",
                self.mbox.name if self.mbox else "<None>",
                dest_mbox.name,
                str(e),
                str(src_uids),
                str(dst_uids),
            )
            raise

        return f"[COPYUID {dest_mbox.uid_vv} {str_src_uids} {str_dst_uids}]"
