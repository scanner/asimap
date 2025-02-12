"""
Higher up the stack.. testing the `client` module. This is the wrapper over
the Mailbox, basically.
"""

# system imports
#
import asyncio
import random
from email.policy import SMTP

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
from ..mbox import Mailbox
from ..parse import IMAPClientCommand, StoreAction
from .conftest import assert_email_equal, client_push_responses


####################################################################
#
@pytest.mark.asyncio
async def test_client_noop(imap_client_proxy):
    imap_client = await imap_client_proxy()
    client_handler = BaseClientHandler(imap_client)

    cmd = IMAPClientCommand("A001 noop\r\n")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 OK NOOP command completed"]


####################################################################
#
@pytest.mark.asyncio
async def test_client_namespace(imap_client_proxy):
    imap_client = await imap_client_proxy()
    client_handler = BaseClientHandler(imap_client)

    cmd = IMAPClientCommand("A001 NAMESPACE\r\n")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == [
        '* NAMESPACE (("" "/")) NIL NIL',
        "A001 OK NAMESPACE command completed",
    ]


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
    await client_handler.do_done()
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

    cmd = IMAPClientCommand(f'A001 LOGIN {user.username} "{password}"\r\n')
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
        r"A003.5 STATUS inbox (MESSAGES recent UIDNEXT uidvalidity unseen)",
        r"A004 SELECT INBOX",
        r"A005 SELECT INBOX",  # Exercise already-selected logic
        r"A006 UNSELECT INBOX",
        r"A007 UNSELECT INBOX",
        r"A008 EXAMINE INBOX",
        r'A009 RENAME INBOX "inbox_copy"',
        r"A001 NOOP",
        r"A010 DELETE INBOX",
        r"A011 DELETE bar",
        r"A012 CREATE foo",
    ]
    expecteds = [
        ["A001 OK NOOP command completed"],
        ["A002 NO client already is in the authenticated state"],
        ["A003 NO client already is in the authenticated state"],
        [
            '* STATUS "inbox" (MESSAGES 20 RECENT 20 UIDNEXT 21 UIDVALIDITY 1 UNSEEN 20)',
            "A003.5 OK STATUS command completed",
        ],
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
        ["A007 BAD Client must be in the selected state"],
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
            "A009 OK RENAME command completed",
        ],
        ["A001 OK NOOP command completed"],
        ["A010 NO You are not allowed to delete the inbox"],
        ["A011 NO No such mailbox: 'bar'"],
        ["A012 OK CREATE command completed"],
    ]

    for command, expected in zip(commands, expecteds):
        cmd = IMAPClientCommand(command + "\r\n")
        cmd.parse()
        async with asyncio.timeout(5):
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
async def test_authenticated_client_list(
    faker, mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email

    # Let us make several other folders.
    #
    folders = ["inbox"]
    for _ in range(5):
        folder_name = faker.word()
        await Mailbox.create(folder_name, server)
        folders.append(folder_name)
        for _ in range(3):
            sub_folder = f"{folder_name}/{faker.word()}"
            if sub_folder in folders:
                continue
            await Mailbox.create(sub_folder, server)
            folders.append(sub_folder)

    folders = sorted(folders)

    client_handler = Authenticated(imap_client, server)
    cmd = IMAPClientCommand('A001 LIST "" ""\r\n')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == [
        r'* LIST (\Noselect) "/" ""',
        "A001 OK LIST command completed",
    ]

    cmd = IMAPClientCommand('A001 LIST "" *\r\n')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)

    assert len(results) == len(folders) + 1
    assert results[-1] == "A001 OK LIST command completed"
    for result, folder in zip(results[:-1], folders):
        assert result.startswith("* LIST (")
        result_fname = result.split()[-1]
        assert f'"{folder.lower()}"' == result_fname.lower()

    # Create one folder with a space in its name to make sure quoting works
    # properly.
    #
    TEST_SPACE = "test space"
    await Mailbox.create(TEST_SPACE, server)
    folders.append(TEST_SPACE)
    cmd = IMAPClientCommand('A002 LIST "" "test*"\r\n')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert len(results) == 2
    assert results[0].endswith(f'"{TEST_SPACE}"')
    assert results[1] == "A002 OK LIST command completed"


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_subscribe_lsub_unsubscribe(
    faker, mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email

    # Let us make several other folders.
    #
    folders = ["inbox"]
    for _ in range(5):
        folder_name = faker.word()
        await Mailbox.create(folder_name, server)
        folders.append(folder_name)
        for _ in range(3):
            sub_folder = f"{folder_name}/{faker.word()}"

            # Do not make folders that already exist.
            #
            if sub_folder in folders:
                continue

            await Mailbox.create(sub_folder, server)
            folders.append(sub_folder)

    folders = sorted(folders)

    client_handler = Authenticated(imap_client, server)

    # Subscribe to five random folders (usually we are going to subscribe to
    # every folder..)
    #
    subscribed = sorted(random.sample(folders, 5))
    for idx, subscribe in enumerate(subscribed):
        cmd = IMAPClientCommand(f'A00{idx} SUBSCRIBE "{subscribe}"')
        cmd.parse()
        await client_handler.command(cmd)
        results = client_push_responses(imap_client)
        assert results == [f"A00{idx} OK SUBSCRIBE command completed"]
        mbox = await server.get_mailbox(subscribe)
        assert mbox.subscribed

    cmd = IMAPClientCommand('A001 LSUB "" "*"\r\n')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert len(results) - 1 == len(subscribed)
    assert results[-1] == "A001 OK LSUB command completed"
    for result, subscribe in zip(results[:-1], subscribed):
        assert result.startswith("* LSUB (")
        result_fname = result.split()[-1]
        assert f'"{subscribe.lower()}"' == result_fname.lower()

    # and unsubscribe..
    #
    for idx, subscribe in enumerate(subscribed):
        cmd = IMAPClientCommand(f'A00{idx} UNSUBSCRIBE "{subscribe}"')
        cmd.parse()
        await client_handler.command(cmd)
        results = client_push_responses(imap_client)
        assert results == [f"A00{idx} OK UNSUBSCRIBE command completed"]
        mbox = await server.get_mailbox(subscribe)
        assert mbox.subscribed is False

    # and lsub will have no results
    #
    cmd = IMAPClientCommand('A001 LSUB "" "*"\r\n')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 OK LSUB command completed"]


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_append(
    email_factory, mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    msg = email_factory()
    msg_as_string = msg.as_string()

    cmd = IMAPClientCommand(
        f'A001 APPEND NOSUCHMAILBOX (\\Flagged) "05-jan-1999 20:55:23 +0000" {{{len(msg_as_string)}+}}\r\n{msg_as_string}'
    )
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 NO [TRYCREATE] No such mailbox: 'NOSUCHMAILBOX'"]

    cmd = IMAPClientCommand(
        f'A001 APPEND inbox (\\Flagged) "05-jan-1999 20:55:23 +0000" {{{len(msg_as_string)}+}}\r\n{msg_as_string}'
    )
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 OK [APPENDUID 1 21] APPEND command completed"]

    # Get the message from the mailbox..
    #
    mbox = await server.get_mailbox("inbox")
    appended_msg = mbox.get_msg(21)
    assert_email_equal(msg, appended_msg)

    # Let us append again, but this time with the mailbox selected. We
    # should get untagged updates from the mailbox for the new message.
    #
    cmd = IMAPClientCommand("A002 SELECT inbox")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    msg = email_factory()
    msg_as_string = msg.as_string(policy=SMTP)

    cmd = IMAPClientCommand(
        f'A003 APPEND inbox (\\Flagged) "05-jan-1999 20:55:23 +0000" {{{len(msg_as_string)}+}}\r\n{msg_as_string}'
    )
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == [
        "* 22 EXISTS",
        "* 22 RECENT",
        r"* 22 FETCH (FLAGS (unseen \Recent \Flagged))",
        "A003 OK [APPENDUID 1 22] APPEND command completed",
    ]
    appended_msg = mbox.get_msg(22)
    assert_email_equal(msg, appended_msg)


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_check(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    # `check` without `select` will fail with No
    #
    cmd = IMAPClientCommand("A001 CHECK")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 NO Client must be in the selected state"]

    cmd = IMAPClientCommand("A001 SELECT INBOX")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    cmd = IMAPClientCommand("A002 CHECK")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A002 OK CHECK command completed"]


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_close(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    cmd = IMAPClientCommand("A001 CLOSE")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 NO Client must be in the selected state"]

    # Messages that are marked `\Deleted` are removed when the mbox is closed.
    #
    mbox = await server.get_mailbox("inbox")
    msg_keys = mbox.mailbox.keys()
    to_delete = sorted(random.sample(msg_keys, 5))
    await mbox.store(to_delete, StoreAction.ADD_FLAGS, [r"\Deleted"])

    # Closing when we had done 'EXAMINE' does not result in messages being
    # purged.
    #
    cmd = IMAPClientCommand("A002 EXAMINE INBOX")
    cmd.parse()
    await client_handler.command(cmd)
    client_push_responses(imap_client)

    cmd = IMAPClientCommand("A003 CLOSE")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A003 OK CLOSE command completed"]
    assert client_handler.mbox is None
    assert client_handler.name not in mbox.clients

    # And get the message keys again.. should have no changes from the
    # previous set.
    #
    new_msg_keys = mbox.mailbox.keys()
    assert new_msg_keys == msg_keys

    # Now SELECT the inbox, and then close it.. the messages we had marked
    # `\Deleted` should be removed from the mbox.
    #
    cmd = IMAPClientCommand("A004 SELECT INBOX")
    cmd.parse()
    await client_handler.command(cmd)
    client_push_responses(imap_client)

    cmd = IMAPClientCommand("A005 CLOSE")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A005 OK CLOSE command completed"]
    assert client_handler.mbox is None
    assert client_handler.name not in mbox.clients

    new_msg_keys = mbox.mailbox.keys()
    for msg_key in to_delete:
        assert msg_key not in new_msg_keys


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_expunge(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    cmd = IMAPClientCommand("A001 EXPUNGE")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 NO Client must be in the selected state"]

    # Messages that are marked `\Deleted` are removed when the mbox is closed.
    #
    mbox = await server.get_mailbox("inbox")
    msg_keys = mbox.mailbox.keys()
    to_delete = sorted(random.sample(msg_keys, 5))
    await mbox.store(to_delete, StoreAction.ADD_FLAGS, [r"\Deleted"])

    # Expunging when we had done 'EXAMINE' does not result in messages being
    # purged.
    #
    cmd = IMAPClientCommand("A002 EXAMINE INBOX")
    cmd.parse()
    await client_handler.command(cmd)
    client_push_responses(imap_client)

    cmd = IMAPClientCommand("A003 EXPUNGE")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A003 OK EXPUNGE command completed"]
    assert client_handler.mbox == mbox
    assert client_handler.name in mbox.clients

    # And get the message keys again.. should have no changes from the
    # previous set.
    #
    new_msg_keys = mbox.mailbox.keys()
    assert new_msg_keys == msg_keys

    # Now SELECT the inbox, and then close it.. the messages we had marked
    # `\Deleted` should be removed from the mbox.
    #
    cmd = IMAPClientCommand("A004 SELECT INBOX")
    cmd.parse()
    await client_handler.command(cmd)
    client_push_responses(imap_client)

    cmd = IMAPClientCommand("A005 EXPUNGE")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    # The results should have the same message sequence numbers as
    # to_delete, in reverse.
    #
    for msg, msg_seq_num in zip(results[:-1], sorted(to_delete, reverse=True)):
        assert msg == f"* {msg_seq_num} EXPUNGE"
    # len(results) - 1 for the "OK"
    #
    assert len(results) - 1 == len(to_delete)
    assert results[-1:] == [
        "A005 OK EXPUNGE command completed",
    ]
    for deleted, result in zip(sorted(to_delete, reverse=True), results):
        assert f"* {deleted} EXPUNGE" == result

    assert client_handler.mbox == mbox
    assert client_handler.name in mbox.clients

    new_msg_keys = mbox.mailbox.keys()
    for msg_key in to_delete:
        assert msg_key not in new_msg_keys


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_search(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    """
    Search is tested mostly `test_search`.. so we only need a very simple
    search.
    """
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    cmd = IMAPClientCommand("A001 SEARCH UNSEEN")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 NO Client must be in the selected state"]

    # Every message in the inbox should be unseen.. so our response should have
    # all these message indicies in it.
    #
    mbox = await server.get_mailbox("inbox")
    msg_keys = mbox.mailbox.keys()

    cmd = IMAPClientCommand("A004 SELECT INBOX")
    cmd.parse()
    await client_handler.command(cmd)
    client_push_responses(imap_client)

    cmd = IMAPClientCommand("A001 SEARCH UNSEEN")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    expected = f"* SEARCH {' '.join(str(x) for x in msg_keys)}"
    assert results == [expected, "A001 OK SEARCH command completed"]

    # and do a UID search
    #
    cmd = IMAPClientCommand("A001 UID SEARCH UNSEEN")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    expected = f"* SEARCH {' '.join(str(x) for x in msg_keys)}"
    assert results == [expected, "A001 OK SEARCH command completed"]


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_fetch(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    """
    simple fetches
    """
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    cmd = IMAPClientCommand("A001 FETCH 1:5 ALL")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 NO Client must be in the selected state"]

    # Every message in the inbox should be unseen.. so our response should have
    # all these message indicies in it.
    #
    mbox = await server.get_mailbox("inbox")
    cmd = IMAPClientCommand("A004 SELECT INBOX")
    cmd.parse()
    await client_handler.command(cmd)
    client_push_responses(imap_client)

    cmd = IMAPClientCommand(
        "A001 FETCH 1:5 (BODY[HEADER.FIELDS (FROM SUBJECT)])"
    )
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    # The "A001 OK FETCH" is a str, everything else is bytes.
    #
    assert results[-1] == "A001 OK FETCH command completed"
    for idx in range(1, 6):
        msg = mbox.get_msg(idx)
        result = results[idx - 1]
        assert isinstance(result, bytes)  # Making mypy happy
        inter = [x.strip() for x in result.split(b"\r\n")]
        subj = inter[1]
        frm = inter[2]
        assert msg["Subject"].encode("latin-1") == subj.split(b":")[1].strip()
        assert msg["From"].encode("latin-1") == frm.split(b":")[1].strip()


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_fetch_lotta_fields(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    """
    Test a more involved fetch that the apple mail client frequently does.
    """
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    mbox = await server.get_mailbox("inbox")
    msg_keys = mbox.mailbox.keys()

    cmd = IMAPClientCommand("A004 SELECT INBOX")
    cmd.parse()
    await client_handler.command(cmd)
    client_push_responses(imap_client)

    cmd = IMAPClientCommand(
        f"A001 FETCH 1:{len(msg_keys)} (INTERNALDATE UID RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS (date subject from to cc message-id in-reply-to references content-type x-priority x-uniform-type-identifier x-universally-unique-identifier list-id list-unsubscribe bimi-indicator bimi-location x-bimi-indicator-hash authentication-results dkim-signature)])"
    )
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client, strip=False)
    assert results[-1] == "A001 OK FETCH command completed\r\n"


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_store(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    """
    Search is tested mostly `test_search`.. so we only need a very simple
    search.
    """
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    cmd = IMAPClientCommand(r"A001 STORE 1:5 +FLAGS \Seen")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 NO Client must be in the selected state"]

    # Every message in the inbox should be unseen.. so our response should have
    # all these message indicies in it.
    #
    cmd = IMAPClientCommand("A004 SELECT INBOX")
    cmd.parse()
    await client_handler.command(cmd)
    client_push_responses(imap_client)

    cmd = IMAPClientCommand(r"A001 STORE 1:5 +FLAGS \Seen")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert len(results) == 6
    assert results[-1] == "A001 OK STORE command completed"

    for idx in range(1, 6):
        assert results[idx - 1] == rf"* {idx} FETCH (FLAGS (\Recent \Seen))"

    cmd = IMAPClientCommand(r"A001 STORE 6:10 +FLAGS.SILENT \Seen")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 OK STORE command completed"]


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_copy(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    """
    Search is tested mostly `test_search`.. so we only need a very simple
    search.
    """
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    cmd = IMAPClientCommand(r"A001 COPY 2:6 MEETING")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 NO Client must be in the selected state"]

    cmd = IMAPClientCommand("A002 SELECT inbox")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    cmd = IMAPClientCommand(r"A003 COPY 2:6 MEETING")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A003 NO [TRYCREATE] No such mailbox: 'MEETING'"]

    cmd = IMAPClientCommand("A004 CREATE MEETING")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    results = client_push_responses(imap_client)
    cmd = IMAPClientCommand(r"A003 COPY 2:6 MEETING")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A003 OK [COPYUID 2 2:6 1:5] COPY command completed"]

    dst_mbox = await server.get_mailbox("MEETING")
    msg_keys = dst_mbox.mailbox.keys()
    assert len(msg_keys) == 5

    # Not going to both inspecting the messages.. the `test_mbox` tests should
    # be good enough for that.


####################################################################
#
@pytest.mark.asyncio
async def test_authenticated_client_create_delete_folder(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    """
    Search is tested mostly `test_search`.. so we only need a very simple
    search.
    """
    FOLDER = "__imapclient"
    SUBFOLDER = "__imapclient/foobar"
    server, imap_client = imap_user_server_and_client
    _ = mailbox_with_bunch_of_email
    client_handler = Authenticated(imap_client, server)

    cmd = IMAPClientCommand(f"A001 CREATE {FOLDER}")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A001 OK CREATE command completed"]

    cmd = IMAPClientCommand(f'BFCO24 LIST "" "{FOLDER}"')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == [
        r'* LIST (\HasNoChildren \Unmarked) "/" "__imapclient"',
        "BFCO24 OK LIST command completed",
    ]

    cmd = IMAPClientCommand(f'BFCO27 LIST "" "{SUBFOLDER}"')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == [
        "BFCO27 OK LIST command completed",
    ]

    cmd = IMAPClientCommand(f"A002 CREATE {SUBFOLDER}")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A002 OK CREATE command completed"]

    cmd = IMAPClientCommand(f'BFCO28 LIST "" "{SUBFOLDER}"')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == [
        r'* LIST (\HasNoChildren \Unmarked) "/" "__imapclient/foobar"',
        "BFCO28 OK LIST command completed",
    ]

    cmd = IMAPClientCommand(f'BFCO29 LIST "{FOLDER}" "*"')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == [
        r'* LIST (\HasChildren \Unmarked) "/" "__imapclient"',
        r'* LIST (\HasNoChildren \Unmarked) "/" "__imapclient/foobar"',
        "BFCO29 OK LIST command completed",
    ]

    cmd = IMAPClientCommand(f"A003 DELETE {SUBFOLDER}")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A003 OK DELETE command completed"]

    cmd = IMAPClientCommand(f'BFCO30 LIST "" "{SUBFOLDER}"')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == [
        "BFCO30 OK LIST command completed",
    ]

    cmd = IMAPClientCommand(f'BFCO31 LIST "{FOLDER}" "*"')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == [
        r'* LIST (\HasNoChildren \Unmarked) "/" "__imapclient"',
        "BFCO31 OK LIST command completed",
    ]

    cmd = IMAPClientCommand(f"A004 DELETE {FOLDER}")
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == ["A004 OK DELETE command completed"]

    cmd = IMAPClientCommand(f'BFCO31 LIST "{FOLDER}" "*"')
    cmd.parse()
    await client_handler.command(cmd)
    results = client_push_responses(imap_client)
    assert results == [
        "BFCO31 OK LIST command completed",
    ]
