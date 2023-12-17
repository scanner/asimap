"""
Higher up the stack.. testing the `client` module. This is the wrapper over
the Mailbox, basically.
"""
# system imports
#

# 3rd party imports
#
import pytest

# Project imports
#
from ..client import (
    CAPABILITIES,
    SERVER_ID,
    Authenticated,
    BaseClientHandler,
    ClientState,
    PreAuthenticated,
)
from ..parse import IMAPClientCommand
from .conftest import client_push_responses


####################################################################
#
@pytest.mark.asyncio
async def test_client_handler_idle_done(imap_client_proxy):
    """
    `DONE` is not handled via the IMAPClientCommand. There is code in the
    user_server stream reader loop to handle it.. so we just test it manually
    here.
    """
    imap_client = await imap_client_proxy()
    client_handler = BaseClientHandler(imap_client)

    cmd = IMAPClientCommand("A001 IDLE\r\n")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["+ idling"]
    assert client_handler.idling is True
    await client_handler.do_done(None)
    results = client_push_responses(imap_client)
    assert results == ["A001 OK IDLE terminated"]
    assert client_handler.idling is False


####################################################################
#
@pytest.mark.asyncio
async def test_client_handler_logout(imap_client_proxy):
    """
    `DONE` is not handled via the IMAPClientCommand. There is code in the
    user_server stream reader loop to handle it.. so we just test it manually
    here.
    """
    imap_client = await imap_client_proxy()
    client_handler = BaseClientHandler(imap_client)

    cmd = IMAPClientCommand("A001 LOGOUT\r\n")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == [
        "* BYE Logging out of asimap server. Good bye.",
        "A001 OK LOGOUT command completed",
    ]
    assert client_handler.state == "logged_out"


####################################################################
#
@pytest.mark.asyncio
async def test_client_handler_unceremonious_bye(imap_client_proxy):
    imap_client = await imap_client_proxy()
    client_handler = BaseClientHandler(imap_client)

    await client_handler.unceremonious_bye("Good bye")
    results = client_push_responses(imap_client)
    assert results == [
        "* BYE Good bye",
    ]
    assert client_handler.state == "logged_out"


####################################################################
#
@pytest.mark.asyncio
async def test_base_client_handler_command(imap_client_proxy):
    """
    Using a BaseClientHandler test the `command()` method. Using a
    BaseClientHandler lets us test things that are valid IMAPCommands, but not
    supported by the BaseClientHandler so we get to test various failures as
    well.
    """
    imap_client = await imap_client_proxy()
    client_handler = BaseClientHandler(imap_client)

    # We test various IMAPCommand's against the client handler.
    #
    commands = [
        r"A001 CAPABILITY",
        r"A001 NAMESPACE",
        r'A001 ID ("version" "21B101" "os" "iOS" "name" "iPhone Mail" "os-version" "17.1.2 (21B101)")',
        r"A001 SELECT INBOX",
    ]
    expecteds = [
        [
            f"* CAPABILITY {' '.join(CAPABILITIES)}",
            "A001 OK CAPABILITY command completed",
        ],
        [
            r'* NAMESPACE (("" "/")) NIL NIL',
            r"A001 OK NAMESPACE command completed",
        ],
        [
            f"""* ID ({" ".join([f'"{k}" "{v}"' for k, v in SERVER_ID.items()])})""",
            "A001 OK ID command completed",
        ],
        ['A001 BAD Sorry, "select" is not a valid command'],
    ]

    for command, expected in zip(commands, expecteds):
        cmd = IMAPClientCommand(command + "\r\n")
        cmd.parse()
        await client_handler.command(cmd)
        results = client_push_responses(imap_client)
        assert results == expected


####################################################################
#
@pytest.mark.asyncio
async def test_preauth_client_handler_login(
    faker, user_factory, password_file_factory, imap_client_proxy
):
    password = faker.password()
    user = user_factory(password=password)
    password_file_factory([user])

    imap_client = await imap_client_proxy()
    client_handler = PreAuthenticated(imap_client)

    cmd = IMAPClientCommand(f"A001 LOGIN {user.username} {password}\r\n")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 OK LOGIN command completed"]


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_handler_commands(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    """
    Test the simpler commands against the Authenticated client handler.
    """
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    # We test various IMAPCommand's against the client handler.
    #
    commands = [
        r"A001 NOOP",
        r"A002 AUTHENTICATE KERBEROS_V4",
        r"A003 LOGIN foo bar",
        r"A004 SELECT INBOX",
        r"A005 SELECT INBOX",  # Exercise already-selected logic
        r"A006 UNSELECT INBOX",
        r"A007 UNSELECT INBOX",
        r"A008 EXAMINE INBOX",
        r'A009 RENAME INBOX "inbox_copy"',
        r"A010 DELETE INBOX",
        r"A011 DELETE bar",
        r"A012 CREATE foo",
    ]
    expecteds = [
        ["A001 OK NOOP command completed"],
        ["A002 BAD client already is in the authenticated state"],
        ["A003 BAD client already is in the authenticated state"],
        [
            "* 20 EXISTS",
            "* 20 RECENT",
            "* OK [UNSEEN 1]",
            "* OK [UIDVALIDITY 1]",
            "* OK [UIDNEXT 21]",
            r"* FLAGS (\Answered \Deleted \Draft \Flagged \Recent \Seen unseen)",
            r"* OK [PERMANENTFLAGS (\Answered \Deleted \Draft \Flagged \Seen \*)]",
            "A004 OK [READ-WRITE] SELECT command completed",
        ],
        [
            "* 20 EXISTS",
            "* 20 RECENT",
            "* OK [UNSEEN 1]",
            "* OK [UIDVALIDITY 1]",
            "* OK [UIDNEXT 21]",
            r"* FLAGS (\Answered \Deleted \Draft \Flagged \Recent \Seen unseen)",
            r"* OK [PERMANENTFLAGS (\Answered \Deleted \Draft \Flagged \Seen \*)]",
            "A005 OK [READ-WRITE] SELECT command completed",
        ],
        ["A006 OK UNSELECT command completed"],
        ["A007 NO Client must be in the selected state"],
        [
            "* 20 EXISTS",
            "* 20 RECENT",
            "* OK [UNSEEN 1]",
            "* OK [UIDVALIDITY 1]",
            "* OK [UIDNEXT 21]",
            r"* FLAGS (\Answered \Deleted \Draft \Flagged \Recent \Seen unseen)",
            r"* OK [PERMANENTFLAGS (\Answered \Deleted \Draft \Flagged \Seen \*)]",
            "A008 OK [READ-ONLY] EXAMINE command completed",
        ],
        [
            "* 20 EXPUNGE",
            "* 19 EXPUNGE",
            "* 18 EXPUNGE",
            "* 17 EXPUNGE",
            "* 16 EXPUNGE",
            "* 15 EXPUNGE",
            "* 14 EXPUNGE",
            "* 13 EXPUNGE",
            "* 12 EXPUNGE",
            "* 11 EXPUNGE",
            "* 10 EXPUNGE",
            "* 9 EXPUNGE",
            "* 8 EXPUNGE",
            "* 7 EXPUNGE",
            "* 6 EXPUNGE",
            "* 5 EXPUNGE",
            "* 4 EXPUNGE",
            "* 3 EXPUNGE",
            "* 2 EXPUNGE",
            "* 1 EXPUNGE",
            "* 0 EXISTS",
            "* 0 RECENT",
            "A009 OK RENAME command completed",
        ],
        ["A010 NO You are not allowed to delete the inbox"],
        ["A011 NO No such mailbox: 'bar'"],
        ["A012 OK CREATE command completed"],
    ]

    for command, expected in zip(commands, expecteds):
        cmd = IMAPClientCommand(command + "\r\n")
        cmd.parse()
        await client_handler.command(cmd)
        results = client_push_responses(imap_client)
        assert results == expected

    # We should still be in EXAMINE mode.
    #
    assert client_handler.examine
    assert client_handler.state == ClientState.SELECTED
    assert client_handler.mbox
    assert client_handler.mbox.name == "inbox"
    mbox_foo = await server.get_mailbox("foo")
    assert mbox_foo

    mbox_inbox_copy = await server.get_mailbox("inbox_copy")
    assert mbox_inbox_copy

    # Rename inbox_copy to something else.
    #
    cmd = IMAPClientCommand('A013 RENAME "inbox_copy" bar')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A013 OK RENAME command completed"]

    # And delete `bar`
    cmd = IMAPClientCommand("A014 DELETE bar")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A014 OK DELETE command completed"]


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_subscribe_and_list(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)
    assert client_handler