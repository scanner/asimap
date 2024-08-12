"""
Here we have the classes that represent the server side state for a
single connected IMAP client.
"""

# system imports
#
import asyncio
import logging
import sys
import time
from enum import StrEnum
from itertools import count, groupby
from typing import TYPE_CHECKING, List, Optional, Union

# asimapd imports
#
from asimap import __version__

from .auth import PWUser, authenticate
from .exceptions import AuthenticationException, Bad, MailboxInconsistency, No
from .mbox import Mailbox, NoSuchMailbox
from .parse import IMAPClientCommand, StatusAtt
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
    "UNSELECT",
    "UIDPLUS",
    "LITERAL+",
    "CHILDREN",
)
SERVER_ID = {
    "name": "asimapd",
    "version": __version__,
    "vendor": "Apricot Systematic",
    "support-url": "https://github.com/scanner/asimap/issues",
    "command": "asimapd.py",
    "os": sys.platform,
}


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
        self.mbox: Optional[Mailbox] = None

        # Idling is like a sub-state. When we are idling we expect a 'DONE'
        # completion from the IMAP client before it sends us any other
        # message. However during this time the server may still send async
        # messages to the client.
        #
        self.idling: bool = False

        # This is used to keep track of the tag.. useful for when finishing an
        # DONE command when idling.
        #
        self.tag: Optional[str] = None

        # If there are pending expunges that we need to send to the client
        # during its next command (that we can send pending expunges during)
        # they are stored here.
        #
        self.pending_notifications: List[str] = []

    ##################################################################
    #
    async def command(self, imap_command: IMAPClientCommand):
        """
        Process an IMAP command we received from the client.

        We use introspection to find out what IMAP commands this handler
        actually supports.

        Arguments:
        - `imap_command`: An instance parse.IMAPClientCommand
        """
        if self.mbox:
            logger.debug(
                "START: Client: %s, State: '%s', mbox: '%s', IMAP Command: %s",
                self.client.name,
                self.state.value,
                self.mbox.name,
                imap_command,
            )
        else:
            logger.debug(
                "START: Client: %s, State: '%s', IMAP Command: %s",
                self.client.name,
                self.state.value,
                imap_command,
            )

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
        start_time = time.time()
        try:
            # There may be cases where some underlying system is stuck locking
            # a folder. We are going to arbitrarily timeout out of those, but
            # we will not close the connection to our client.
            #
            # XXX For now we have a timeout of 6 minutes. We want to make this
            #     short, like 15 seconds, and pass the timeout context manager
            #     into subcontexts where it can extend it if it needs to.
            #
            async with asyncio.timeout(360) as tcm:
                imap_command.timeout_cm = tcm
                result = await getattr(self, f"do_{imap_command.command}")(
                    imap_command
                )
        except No as e:
            result = f"{imap_command.tag} NO {e}\r\n"
            await self.client.push(result)
            logger.debug(result.strip())
            return
        except Bad as e:
            result = f"{imap_command.tag} BAD {e}\r\n"
            await self.client.push(result)
            logger.debug(result.strip())
            return
        except asyncio.TimeoutError:
            result = f"{imap_command.tag} BAD Command timed out: '{imap_command.qstr()}'"
            try:
                await self.client.push(result)
            except Exception:
                pass
            logger.error(result)
            return
        except ConnectionResetError as e:
            mbox_name = self.mbox.name if self.mbox else "no mailbox"
            logger.debug(
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
            result = f"{imap_command.tag} BAD Unhandled exception: {e}"
            try:
                await self.client.push(result.strip())
            except Exception:
                pass
            logger.debug(result)
            raise
        finally:
            imap_command.timeout_cm = None
            cmd_duration = time.time() - start_time
            logger.debug(
                "FINISH: Client: %s, IMAP Command '%s' took %.3f seconds",
                self.client.name,
                imap_command.qstr(),
                cmd_duration,
            )

        # If there was no result from running this command then everything went
        # okay and we send back a final 'OK' to the client for processing this
        # command.
        #
        cmd = "none" if imap_command.command is None else imap_command.command
        if result is None:
            result = (
                f"{imap_command.tag} OK " f"{cmd.upper()} command completed\r\n"
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
    async def send_pending_notifications(self):
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
    async def unceremonious_bye(self, msg):
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
        await self.client.push(f"* BYE {msg}\r\n")
        await self.client.close()

        self.idling = False
        self.state = ClientState.LOGGED_OUT

    # The following commands are supported in any state.
    #
    ##################################################################
    #
    async def do_done(self, cmd: Optional[IMAPClientCommand] = None):
        """
        We have gotten a DONE. This is only called when we are idling.

        Arguments:
        - `cmd`: This is ignored.
        """
        self.idling = False
        await self.send_pending_notifications()
        await self.client.push(f"{self.tag} OK IDLE terminated\r\n")
        return

    #########################################################################
    #
    async def do_capability(self, cmd: IMAPClientCommand):
        """
        Return the capabilities of this server.

        Arguments:
        - `cmd`: The full IMAP command object.
        """
        await self.send_pending_notifications()
        await self.client.push(f"* CAPABILITY {' '.join(CAPABILITIES)}\r\n")
        return None

    #########################################################################
    #
    async def do_namespace(self, cmd: IMAPClientCommand):
        """
        We currently only support a single personal name space. No leading
        prefix is used on personal mailboxes and '/' is the hierarchy
        delimiter.

        Arguments:
        - `cmd`: The full IMAP command object.
        """
        await self.send_pending_notifications()
        await self.client.push('* NAMESPACE (("" "/")) NIL NIL\r\n')
        return None

    #########################################################################
    #
    async def do_id(self, cmd: IMAPClientCommand):
        """
        Construct an ID response... uh.. lookup the rfc that defines this.

        Arguments:
        - `cmd`: The full IMAP command object.
        """
        await self.send_pending_notifications()
        self.client_id = cmd.id_dict
        res = " ".join([f'"{k}" "{v}"' for k, v in SERVER_ID.items()])
        await self.client.push(f"* ID ({res})\r\n")
        return None

    #########################################################################
    #
    async def do_idle(self, cmd: IMAPClientCommand):
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
    async def do_logout(self, cmd: IMAPClientCommand):
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
        self.user: Optional[PWUser] = None
        return

    # The following commands are supported in the non-authenticated state.
    #

    ##################################################################
    #
    async def do_authenticated(self, cmd: IMAPClientCommand):
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
    async def do_login(self, cmd: IMAPClientCommand):
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
            raise No(str(e))
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

    # XXX Remove this method. Resync's can only happen in the mbox's management
    #     task or the final bit of the COPY command.
    #
    # ##################################################################
    # #
    # async def notifies(self):
    #     """
    #     Handles the common case of sending pending expunges and a resync where
    #     we only notify this client of exists/recent.
    #     """
    #     if self.state == ClientState.SELECTED and self.mbox is not None:
    #         if self.mbox.lock.this_task_has_read_lock():
    #             await self.mbox.resync()
    #         else:
    #             async with self.mbox.lock.read_lock():
    #                 await self.mbox.resync()
    #     await self.send_pending_notifications()

    #########################################################################
    #
    async def do_noop(self, cmd: IMAPClientCommand):
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
    async def do_authenticate(self, cmd: IMAPClientCommand):
        await self.send_pending_notifications()
        raise No("client already is in the authenticated state")

    #########################################################################
    #
    async def do_login(self, cmd: IMAPClientCommand):
        await self.send_pending_notifications()
        raise No("client already is in the authenticated state")

    ##################################################################
    #
    async def do_select(
        self,
        cmd: IMAPClientCommand,
        examine=False,
    ):
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
                self.mbox.unselected(self.client.name)
                self.mbox = None

        # Note the 'selected()' method may fail with an exception and
        # we should not set our state or the mailbox we have selected
        # until 'selected()' returns without a failure.
        #
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
    async def do_unselect(self, cmd: IMAPClientCommand):
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
            raise No("Client must be in the selected state")

        if self.mbox:
            self.mbox.unselected(self.client.name)
            self.mbox = None
        self.pending_notifications = []
        self.idling = False
        self.state = ClientState.AUTHENTICATED

    #########################################################################
    #
    async def do_examine(self, cmd: IMAPClientCommand):
        """
        examine a specific mailbox (just like select, but read only)
        """
        return await self.do_select(cmd, examine=True)

    ##################################################################
    #
    async def do_create(self, cmd: IMAPClientCommand):
        """
        Create the specified mailbox.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        await self.send_pending_notifications()
        await Mailbox.create(cmd.mailbox_name, self.server)
        cmd.completed = True

    ##################################################################
    #
    async def do_delete(self, cmd: IMAPClientCommand):
        """
        Delete the specified mailbox.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
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

        mbox = await self.server.get_mailbox(cmd.mailbox_name)
        mbox.subscribed = False
        await mbox.commit_to_db()

    ##################################################################
    #
    async def do_list(self, cmd, lsub=False) -> None:
        """
        The LIST command returns a subset of names from the complete
        set of all names available to the client.  Zero or more
        untagged LIST replies are returned, containing the name
        attributes, hierarchy delimiter, and name; see the description
        of the LIST reply for more detail.

        Arguments:
        - `cmd`: The IMAP command we are executing
        - `lsub`: If True this will only match folders that have their
          subscribed bit set.
        """
        await self.send_pending_notifications()

        # Handle the special case where the client is basically just probing
        # for the hierarchy sepration character.
        #
        if cmd.mailbox_name == "" and cmd.list_mailbox == "":
            await self.client.push(r'* LIST (\Noselect) "/" ""' + "\r\n")
            return

        res = "LIST"
        if lsub:
            res = "LSUB"

        async for mbox_name, attributes in Mailbox.list(
            cmd.mailbox_name, cmd.list_mailbox, self.server, lsub
        ):
            mbox_name = "INBOX" if mbox_name.lower() == "inbox" else mbox_name
            msg = f'* {res} ({" ".join(attributes)}) "/" "{mbox_name}"\r\n'
            await self.client.push(msg)

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
    async def do_status(self, cmd: IMAPClientCommand):
        """
        Get the designated mailbox and return the requested status
        attributes to our client.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        await self.send_pending_notifications()

        mbox = await self.server.get_mailbox(cmd.mailbox_name, expiry=45)
        result: List[str] = []
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
    async def do_append(self, cmd: IMAPClientCommand):
        """
        Append a message to a mailbox.

        Arguments:
        - `cmd`: The IMAP command we are executing
        """
        await self.send_pending_notifications()

        try:
            mbox = await self.server.get_mailbox(cmd.mailbox_name)
            async with cmd.ready_and_okay(mbox):
                uid = await mbox.append(
                    cmd.message, cmd.flag_list, cmd.date_time
                )
        except NoSuchMailbox:
            # For APPEND and COPY if the mailbox does not exist we
            # MUST supply the TRYCREATE flag so we catch the generic
            # exception and return the appropriate NO result.
            #
            raise No(f"[TRYCREATE] No such mailbox: '{cmd.mailbox_name}'")
        await self.send_pending_notifications()
        return f"[APPENDUID {mbox.uid_vv} {uid}]"

    ##################################################################
    #
    async def do_check(self, cmd: IMAPClientCommand):
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
            self.unceremonious_bye("Your selected mailbox no longer exists")
            return

        await self.send_pending_notifications()

        # This forces a checkpoint, which is essentially done on the next
        # resync. Force the next resync by setting `optional_resync` to False.
        # Then wait for the mbox to let this command execute. Because `CHECK`
        # must execute when no other commands running, a resync will be running
        # before control passes back to this function.
        #
        self.mbox.optional_resync = False
        async with cmd.ready_and_okay(self.mbox):
            pass
        await self.send_pending_notifications()

    ##################################################################
    #
    async def do_close(self, cmd: IMAPClientCommand):
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
        mbox = None
        if self.mbox:
            self.mbox.unselected(self.name)
            mbox = self.mbox
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
        if mbox:
            # Do an EXPUNGE if there are any messages marked 'Delete'
            #
            if mbox.sequences.get("Deleted", []):
                async with cmd.ready_and_okay(mbox):
                    await mbox.expunge()

    ##################################################################
    #
    async def do_expunge(self, cmd: IMAPClientCommand):
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
            self.unceremonious_bye("Your selected mailbox no longer exists")
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
                    await self.mbox.expunge()
        finally:
            self.idling = idling

    ##################################################################
    #
    async def do_search(self, cmd: IMAPClientCommand):
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
            self.unceremonious_bye("Your selected mailbox no longer exists")
            return

        # If this client has pending notifications messages then we return a
        # tagged No response.. the client should see this and do a NOOP or such
        # and receive the pending expunges. Unless this is a UID command. It is
        # okay to send pending expunges during the operations of a UID SEARCH.
        #
        if self.pending_expunges():
            if cmd.uid_command:
                self.send_pending_notifications()
            else:
                raise No("There are pending untagged responses")

        async with cmd.ready_and_okay(self.mbox):
            try:
                results = await self.mbox.search(
                    cmd.search_key, cmd.uid_command
                )
                await self.client.push(
                    f"* SEARCH {' '.join(str(x) for x in results)}\r\n"
                )
            except MailboxInconsistency as e:
                self.optional_resync = False
                self.full_search = True
                self.server.msg_cache.clear_mbox(self.mbox.name)
                logger.warning("Mailbox %s: %s", self.mbox.name, str(e))

    ##################################################################
    #
    async def do_fetch(self, cmd: IMAPClientCommand):
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
            self.unceremonious_bye("Your selected mailbox no longer exists")
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
                    self.unceremonious_bye("You have pending EXPUNGEs.")
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
                    msg_set, cmd.fetch_atts, cmd.uid_command
                ):
                    await self.client.push(
                        f"* {idx} FETCH ({' '.join(results)})\r\n"
                    )
        except MailboxInconsistency as exc:
            self.server.msg_cache.clear_mbox(self.mbox.name)
            self.mbox.full_search = True
            self.mbox.optional_resync = False
            raise Bad(f"Problem while fetching: {exc}")

        # The FETCH may have caused some message flags to change, and they may
        # not have been in the FETCH responses we already sent (and other
        # FETCH's may have done the same) so make sure we send out all pending
        # notifications.
        #
        await self.send_pending_notifications()

    ##################################################################
    #
    async def do_store(self, cmd: IMAPClientCommand):
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
            self.unceremonious_bye("Your selected mailbox no longer exists")
            return

        # If this client has pending EXPUNGE messages then we return a
        # tagged No response.. the client should see this and do a NOOP or
        # such and receive the pending expunges.  Unless this is a UID
        # command. It is okay to send pending expunges during the
        # operations of a UID FETCH.
        #
        if self.pending_expunges():
            if cmd.uid_command:
                self.send_pending_notifications()
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
            self.server.msg_cache.clear_mbox(self.mbox.name)
            raise Bad(f"Problem while storing: {exc}")

        # IF this is not a "SILENT" store, we will send the FETCH messages that
        # were generated due to the actions of this STORE command. (All the
        # other clients will have gotten untagged FETCH messages when the
        # resync was completed by the management task.)
        #
        if not cmd.silent:
            await self.client.push(*fetch_notifications)

    ##################################################################
    #
    async def do_copy(self, cmd: IMAPClientCommand):
        """
        Copy the given set of messages to the destination mailbox.

        NOTE: Causes a resync of the destination mailbox.

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
            self.unceremonious_bye("Your selected mailbox no longer exists")
            return

        await self.send_pending_notifications()

        # Wait until the mailbox gives us the go-ahead to run the command.
        #
        async with cmd.ready_and_okay(self.mbox):
            try:
                dest_mbox = await self.server.get_mailbox(cmd.mailbox_name)
                src_uids, dst_uids = await self.mbox.copy(
                    cmd.msg_set, dest_mbox, cmd.uid_command, imap_cmd=cmd
                )
            except NoSuchMailbox:
                # For APPEND and COPY if the mailbox does not exist we
                # MUST supply the TRYCREATE flag so we catch the generic
                # exception and return the appropriate NO result.
                #
                raise No(f"[TRYCREATE] No such mailbox: '{cmd.mailbox_name}'")

        # NOTE: I tip my hat to: http://stackoverflow.com/questions/3429510/
        # pythonic-way-to-convert-a-list-of-integers-into-a-string-of-
        # comma-separated-range/3430231#3430231
        #
        try:
            new_src_uids = [
                list(x)
                for _, x in groupby(src_uids, lambda x, c=count(): next(c) - x)
            ]
            str_src_uids = ",".join(
                ":".join(map(str, (g[0], g[-1])[: len(g)]))
                for g in new_src_uids
            )
            new_dst_uids = [
                list(x)
                for _, x in groupby(dst_uids, lambda x, c=count(): next(c) - x)
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
                self.mbox.name,
                dest_mbox.name,
                str(e),
                str(src_uids),
                str(dst_uids),
            )
            raise

        return f"[COPYUID {dest_mbox.uid_vv} {str_src_uids} {str_dst_uids}]"
