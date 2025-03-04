"""
Tests for the mbox module
"""

# system imports
#
import asyncio
import os
import random
from dataclasses import dataclass
from datetime import datetime
from mailbox import MHMessage
from typing import Dict, List, Tuple

# 3rd party imports
#
import aiofiles
import pytest
from dirty_equals import IsNow
from pytest_mock import MockerFixture

# Project imports
#
from ..constants import flag_to_seq
from ..exceptions import Bad, No
from ..fetch import FetchAtt, FetchOp
from ..mbox import InvalidMailbox, Mailbox, MailboxExists, NoSuchMailbox
from ..parse import IMAPClientCommand, StoreAction, parse_cmd_from_msg
from ..search import IMAPSearch
from .conftest import assert_email_equal, client_push_responses


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_init(imap_user_server):
    """
    We can create a Mailbox object instance.
    """
    server = imap_user_server
    NAME = "inbox"
    mbox = await server.get_mailbox(NAME)
    assert mbox
    assert mbox.id
    assert mbox.last_resync == IsNow(unix_number=True)

    results = await server.db.fetchone(
        "select id, uid_vv,attributes,mtime,next_uid,num_msgs,"
        "num_recent,uids,last_resync,subscribed from mailboxes "
        "where name=?",
        (NAME,),
    )
    (
        id,
        uid_vv,
        attributes,
        mtime,
        next_uid,
        num_msgs,
        num_recent,
        uids,
        last_resync,
        subscribed,
    ) = results
    assert id == mbox.id
    assert uid_vv == 1  # 1 because first mailbox in server
    assert mbox.uid_vv == uid_vv
    assert sorted(attributes.split(",")) == [
        r"\HasNoChildren",
        r"\Unmarked",
    ]
    assert mbox.mtime == mtime
    assert mtime == IsNow(unix_number=True)
    assert next_uid == 1
    assert mbox.next_uid == next_uid
    assert num_msgs == 0
    assert mbox.num_msgs == num_msgs
    assert num_recent == 0
    assert mbox.num_recent == num_recent
    assert uids == ""
    assert len(mbox.uids) == 0
    assert mbox.last_resync == last_resync
    assert bool(subscribed) is False
    assert mbox.subscribed == bool(subscribed)


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_init_with_messages(mailbox_with_bunch_of_email):
    mbox = mailbox_with_bunch_of_email
    assert mbox.uid_vv == 1
    assert r"\Marked" in mbox.attributes
    assert r"\HasNoChildren" in mbox.attributes

    msg_keys = set([int(x) for x in mbox.mailbox.keys()])
    assert len(msg_keys) > 0
    mtimes = []
    for msg_key in sorted(msg_keys):
        path = os.path.join(mbox.mailbox._path, str(msg_key))
        mtimes.append(await aiofiles.os.path.getmtime(path))

    async with mbox.mh_sequences_lock:
        seqs = mbox.get_sequences_from_folder()

    # NOTE: By default `bunch_of_email_in_folder` inserts all messages it
    # creates in to the `unseen` sequence.
    #
    assert mbox.num_msgs == len(msg_keys)
    assert mbox.sequences == seqs
    assert len(mbox.sequences["unseen"]) == len(msg_keys)
    assert mbox.sequences["unseen"] == msg_keys
    assert len(mbox.sequences["Seen"]) == 0
    assert mbox.sequences["Recent"] == msg_keys
    assert len(mbox.msg_keys) == len(mbox.uids)

    # The messages mtimes should not have changed.
    #
    for msg_key, orig_mtime in zip(sorted(msg_keys), mtimes):
        path = os.path.join(mbox.mailbox._path, str(msg_key))
        mtime = await aiofiles.os.path.getmtime(path)
        assert mtime == orig_mtime


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_gets_new_message(
    bunch_of_email_in_folder, imap_user_server
):
    """
    After initial init, add message to folder. Do resync.
    """
    NAME = "inbox"
    bunch_of_email_in_folder(folder=NAME)
    server = imap_user_server
    mbox = await server.get_mailbox(NAME)
    last_resync = mbox.last_resync

    # We need to sleep at least one second for mbox.last_resync to change
    # (we only consider seconds)
    #
    await asyncio.sleep(1)

    # Now add one message to the folder.
    #
    bunch_of_email_in_folder(folder=NAME, num_emails=1)
    msg_keys = set([int(x) for x in mbox.mailbox.keys()])

    await mbox.check_new_msgs_and_flags()
    assert r"\Marked" in mbox.attributes
    assert mbox.last_resync > last_resync
    assert mbox.num_msgs == len(msg_keys)
    assert len(mbox.sequences["unseen"]) == len(msg_keys)
    assert mbox.sequences["unseen"] == set(msg_keys)
    assert mbox.sequences["Recent"] == set(msg_keys)
    assert len(mbox.sequences["Seen"]) == 0
    assert len(mbox.msg_keys) == len(mbox.uids)


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_resync_auto_pack(
    bunch_of_email_in_folder, imap_user_server
):
    """
    resync autopacks if the folder is too gappy.
    """
    NAME = "inbox"

    # Gap every other message. This is enough gaps for the auto-repack to kick
    # in.
    #
    bunch_of_email_in_folder(sequence=range(1, 41))

    server = imap_user_server
    Mailbox.FOLDER_SIZE_PACK_LIMIT = 20
    mbox = await Mailbox.new(NAME, server)

    msg_keys = list(range(1, 21))  # After pack it should be 1..20
    assert mbox.num_msgs == len(msg_keys)
    assert len(mbox.sequences["unseen"]) == len(msg_keys)
    assert mbox.sequences["unseen"] == set(msg_keys)
    assert mbox.sequences["Recent"] == set(msg_keys)
    assert len(mbox.msg_keys) == len(mbox.uids)


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_selected_unselected(
    bunch_of_email_in_folder, imap_user_server_and_client
):
    NAME = "inbox"
    bunch_of_email_in_folder()
    server, imap_client_proxy = imap_user_server_and_client
    mbox = await Mailbox.new(NAME, server)
    msg_keys = [int(x) for x in mbox.mailbox.keys()]
    num_msgs = len(msg_keys)

    results = await mbox.selected(imap_client_proxy.cmd_processor)

    expected = [
        f"* {num_msgs} EXISTS",
        f"* {num_msgs} RECENT",
        f"* OK [UNSEEN {msg_keys[0]}]",
        f"* OK [UIDVALIDITY {mbox.uid_vv}]",
        f"* OK [UIDNEXT {mbox.next_uid}]",
        r"* FLAGS (\Answered \Deleted \Draft \Flagged \Recent \Seen unseen)",
        r"* OK [PERMANENTFLAGS (\Answered \Deleted \Draft \Flagged \Seen \*)]",
    ]

    results = [x.strip() for x in results]
    assert expected == results

    mbox.unselected(imap_client_proxy.cmd_processor.name)

    results = await mbox.selected(imap_client_proxy.cmd_processor)
    results = [x.strip() for x in results]
    assert expected == results


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_append(imap_user_server, email_factory):
    server = imap_user_server
    NAME = "inbox"
    mbox = await Mailbox.new(NAME, server)

    msg = email_factory()

    uid = await mbox.append(msg, flags=[r"\Flagged"], date_time=datetime.now())

    msg_keys = [int(x) for x in mbox.mailbox.keys()]

    assert len(msg_keys) == 1
    msg_key = msg_keys[0]
    folder_msg = mbox.get_msg(msg_key)
    uid_vv, msg_uid = mbox.get_uid_from_msg(msg_key)
    msg_seqs = mbox.msg_sequences(msg_key)
    assert sorted(msg_seqs) == sorted(["flagged", "unseen", "Recent"])
    assert mbox.sequences == {"flagged": {1}, "unseen": {1}, "Recent": {1}}
    assert msg_uid == uid
    assert uid_vv == mbox.uid_vv

    # Make sure the messages match.
    #
    assert_email_equal(msg, folder_msg)


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_expunge_with_client(
    bunch_of_email_in_folder, imap_user_server_and_client
):
    num_msgs_to_delete = 4
    NAME = "inbox"
    bunch_of_email_in_folder(folder=NAME)
    server, imap_client = imap_user_server_and_client
    mbox = await server.get_mailbox(NAME)
    mbox.clients[imap_client.cmd_processor.name] = imap_client.cmd_processor

    # Mark messages for expunge.
    #
    msg_keys = [int(x) for x in mbox.mailbox.keys()]
    num_msgs = len(msg_keys)
    for i in range(1, num_msgs_to_delete + 1):
        mbox.sequences["Deleted"].add(msg_keys[i])

    async with mbox.mh_sequences_lock:
        mbox.set_sequences_in_folder(mbox.sequences)

    imap_client.cmd_processor.idling = True
    await mbox.expunge()
    imap_client.cmd_processor.idling = False

    results = client_push_responses(imap_client)
    assert results == [
        "* 5 EXPUNGE",
        "* 4 EXPUNGE",
        "* 3 EXPUNGE",
        "* 2 EXPUNGE",
    ]
    assert mbox.uids == [
        1,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
    ]
    msg_keys = [int(x) for x in mbox.mailbox.keys()]
    assert len(msg_keys) == num_msgs - num_msgs_to_delete
    assert len(mbox.uids) == len(msg_keys)
    async with mbox.mh_sequences_lock:
        seqs = mbox.get_sequences_from_folder()
    assert "Deleted" not in seqs
    assert not mbox.sequences["Deleted"]


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_uid_expunge_with_client(
    bunch_of_email_in_folder, imap_user_server_and_client
):
    NUM_MSGS_TO_DELETE = 4
    NUM_MSGS_MARKED_DELETED = 10
    NAME = "inbox"
    bunch_of_email_in_folder(folder=NAME)
    server, imap_client = imap_user_server_and_client
    mbox = await server.get_mailbox(NAME)
    mbox.clients[imap_client.cmd_processor.name] = imap_client.cmd_processor

    # Mark messages \Deleted
    #
    msg_keys = [int(x) for x in mbox.mailbox.keys()]
    num_msgs = len(msg_keys)
    expect_deleted = []
    for i in range(1, NUM_MSGS_MARKED_DELETED + 1):
        mbox.sequences["Deleted"].add(msg_keys[i])
        expect_deleted.append(msg_keys[i])

    async with mbox.mh_sequences_lock:
        mbox.set_sequences_in_folder(mbox.sequences)

    # NOTE: uid's and msg_keys have the same values when messages are first
    #       added to a mailbox.
    #
    uids_to_delete = list(range(2, NUM_MSGS_TO_DELETE + 2))
    set_expect_deleted = set(expect_deleted) - set(uids_to_delete)
    print(f"**** uids to delete: {uids_to_delete}")
    imap_client.cmd_processor.idling = True
    await mbox.expunge(uid_msg_set=uids_to_delete)
    imap_client.cmd_processor.idling = False

    results = client_push_responses(imap_client)
    assert results == [
        "* 5 EXPUNGE",
        "* 4 EXPUNGE",
        "* 3 EXPUNGE",
        "* 2 EXPUNGE",
    ]
    assert mbox.uids == [
        1,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
    ]
    msg_keys = [int(x) for x in mbox.mailbox.keys()]
    assert len(msg_keys) == num_msgs - NUM_MSGS_TO_DELETE
    assert len(mbox.uids) == len(msg_keys)
    async with mbox.mh_sequences_lock:
        seqs = mbox.get_sequences_from_folder()

    assert seqs["Deleted"] == set_expect_deleted


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_search(mailbox_with_bunch_of_email):
    """
    Search is tested mostly `test_search`.. so we only need a very simple
    search.
    """
    mbox = mailbox_with_bunch_of_email
    msg_keys = [int(x) for x in mbox.mailbox.keys()]
    search_op = IMAPSearch("all")

    # new mailbox, msg_keys have the same values is imap message sequences
    #
    results = await mbox.search(search_op, uid_cmd=False)
    assert results == msg_keys

    # ditto for uid's
    #
    results = await mbox.search(search_op, uid_cmd=True)
    assert results == msg_keys


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_fetch(mailbox_with_bunch_of_email):
    """
    Fetch is tested mostly `test_fetch`.. so we only need a very simple
    fetch.
    """
    # We know this mailbox has messages numbered from 1 to 20.
    #
    mbox = mailbox_with_bunch_of_email
    msg_keys = [int(x) for x in mbox.mailbox.keys()]
    msg_set = [2, 3, 4]

    # New mailbox.. all messages are unseen. FETCH BODY without PEEK marks them
    # as seen.
    #
    seen = flag_to_seq(r"\Seen")
    unseen = flag_to_seq("unseen")
    assert unseen in mbox.sequences
    assert mbox.sequences[unseen] == set(msg_keys)
    assert not mbox.sequences[seen]

    # UID's, message number, and message key are all the same value for a fresh
    # mailbox.
    #
    expected_keys = (2, 3, 4)
    msgs: Dict[int, MHMessage] = {}
    for msg_key in expected_keys:
        msgs[msg_key] = mbox.get_msg(msg_key)
    fetch_ops = [
        FetchAtt(FetchOp.FLAGS),
        FetchAtt(
            FetchOp.BODY,
            section=[["HEADER.FIELDS", ["Date", "From"]]],
            peek=True,
        ),
    ]

    # `fetch()` yields a tuple. The first element is the message number. The
    # second element is a list that contains the individual fetch att
    # results. In the case of a UID command it also has a `UID` result.
    #
    # NOTE: We are not going to test the contents of the results for now. We
    #       test that in other modules. Just want to make sure that the data
    #       was formatted properly.
    async for fetch_result in mbox.fetch(msg_set, fetch_ops):
        msg_key, result = fetch_result
        assert msg_key in expected_keys
        flags, headers = result
        assert flags.startswith(b"FLAGS (")
        assert headers.startswith(b"BODY[HEADER.FIELDS (Date From)] {")

    for msg_key in msg_set:
        # One of the FETCH's is a BODY.PEEK, thus `\Seen` flag should
        # not be on the messages yet, and they should still be `unseen`.
        #
        assert msg_key not in mbox.sequences[seen]
        assert msg_key in mbox.sequences[unseen]

    # Twiggle the FETCH BODY.PEEK to be a FETCH BODY.
    #
    fetch_ops[1].peek = False
    async for idx, result in mbox.fetch(msg_set, fetch_ops, uid_cmd=True):
        assert msg_key in expected_keys
        flags, headers, uid = result
        uid_str, uid_val = uid.split()
        assert uid_str == b"UID"
        # NOTE: idx is a imap message sequence number, which is 1-based. So need
        #       -1 to get the proper UID.
        #
        assert int(uid_val) == mbox.uids[idx - 1]
        assert flags.startswith(b"FLAGS (")
        assert headers.startswith(b"BODY[HEADER.FIELDS (Date From)] {")

    for msg_key in msg_set:
        # One of the FETCH's is a BODY.PEEK, thus `\Seen` flag should
        # not be on the messages yet, and they should still be `unseen`.
        #
        assert msg_key in mbox.sequences[seen]
        assert msg_key not in mbox.sequences[unseen]

        msg_sequences = mbox.msg_sequences(msg_key)
        assert seen in msg_sequences
        assert unseen not in msg_sequences


####################################################################
#
@pytest.mark.skip(reason="will impelement test later")
@pytest.mark.asyncio
async def test_mailbox_fetch_notifies_other_clients(
    mailbox_with_bunch_of_email,
):
    """
    If a fetch modifies flags (Recent & unseen) then we need to make sure
    other clients were notified of these changes by being sent untagged FETCH
    messages.
    """
    # XXX Do this by mocking _dispatch_or_pend_notifications and checking to
    #     see if it was called with the right messages.
    #
    assert False


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_db_commit_sequence_changes(
    faker, bunch_of_email_in_folder, mailbox_with_bunch_of_email
):
    """
    Make sure our db commit code works when we change the sequences on a
    mailbox.
    """
    mbox = mailbox_with_bunch_of_email
    msg_keys = mbox.msg_keys

    # Create new sequences
    #
    mbox.sequences["newnew"] = set(msg_keys[:5])
    mbox.sequences["newnew2"] = set(msg_keys[:5])
    await mbox.commit_to_db()

    # Update an existing sequence
    #
    mbox.sequences["newnew"] = set(msg_keys[:8])
    await mbox.commit_to_db()

    # Effectively delete sequences via empty set and removing the element
    # entirely.
    #
    mbox.sequences["newnew"] = set()
    del mbox.sequences["newnew2"]
    await mbox.commit_to_db()


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_fetch_after_new_messages(
    faker, bunch_of_email_in_folder, mailbox_with_bunch_of_email
):
    """
    Makes sure that doing a fetch after a folder has gotten new messages
    and done a resync works.
    """
    mbox = mailbox_with_bunch_of_email

    # Now add one message to the folder.
    #
    bunch_of_email_in_folder(folder=mbox.name, num_emails=1)
    msg_keys = mbox.mailbox.keys()

    await mbox.check_new_msgs_and_flags(optional=False)
    assert len(msg_keys) == mbox.num_msgs
    search_op = IMAPSearch("all")

    # Get the UID's of all the messages in the folder.
    #
    search_results = await mbox.search(search_op, uid_cmd=True)

    # Fetch the flags of the messages by uid we got from the search results
    #
    fetch_ops = [
        FetchAtt(FetchOp.FLAGS),
        FetchAtt(
            FetchOp.BODY,
            section=[["HEADER.FIELDS", ["Date", "From"]]],
            peek=True,
        ),
    ]

    async for fetch_result in mbox.fetch(
        search_results, fetch_ops, uid_cmd=True
    ):
        msg_key, results = fetch_result
        for result in results:
            if result.startswith(b"UID "):
                uid = int(result.split(b" ")[1])
                # message keys are 1-based, search results list is 0-based.
                #
                assert uid == search_results[msg_key - 1]


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_store(mailbox_with_bunch_of_email):
    """
    Search is tested mostly `test_search`.. so we only need a very simple
    search.
    """
    # We know this mailbox has messages numbered from 1 to 20.  We also know
    # since this is an initial state the msg_key, message sequence number, and
    # uid's are the same for each message (ie: 1 == 1 == 1)
    #
    mbox = mailbox_with_bunch_of_email
    msg_set = sorted(list(random.sample(mbox.msg_keys, 5)))

    # can not touch `\Recent`
    #
    with pytest.raises(No):
        await mbox.store(msg_set, StoreAction.REMOVE_FLAGS, [r"\Recent"])

    with pytest.raises(Bad):
        await mbox.store(msg_set, -1, [r"\Answered"])

    # The messages are all currently 'unseen' when the mbox is created.
    # By setting `\Seen` they will all lose `unseen` (and gain `\Seen`)
    #
    await mbox.store(msg_set, StoreAction.ADD_FLAGS, [r"\Seen"])
    for msg_key in msg_set:
        assert msg_key in mbox.sequences[flag_to_seq(r"\Seen")]
        assert msg_key not in mbox.sequences[flag_to_seq("unseen")]

        msg_seq = mbox.msg_sequences(msg_key)
        assert flag_to_seq(r"\Seen") in msg_seq
        assert flag_to_seq("unseen") not in msg_seq

    await mbox.store(msg_set, StoreAction.REMOVE_FLAGS, [r"\Seen"])
    for msg_key in msg_set:
        assert msg_key not in mbox.sequences[flag_to_seq(r"\Seen")]
        assert msg_key in mbox.sequences[flag_to_seq("unseen")]

        msg_seq = mbox.msg_sequences(msg_key)
        assert flag_to_seq(r"\Seen") not in msg_seq
        assert flag_to_seq("unseen") in msg_seq

    await mbox.store(msg_set, StoreAction.REPLACE_FLAGS, [r"\Answered"])
    for msg_key in msg_set:
        assert msg_key in mbox.sequences[flag_to_seq(r"\Answered")]
        assert msg_key not in mbox.sequences[flag_to_seq(r"\Seen")]
        assert msg_key in mbox.sequences[flag_to_seq("unseen")]

    await mbox.store(
        msg_set, StoreAction.REPLACE_FLAGS, [r"\Seen", r"\Answered"]
    )
    for msg_key in msg_set:
        assert msg_key in mbox.sequences[flag_to_seq(r"\Answered")]
        assert msg_key in mbox.sequences[flag_to_seq(r"\Seen")]
        assert msg_key not in mbox.sequences[flag_to_seq("unseen")]

        msg_seq = mbox.msg_sequences(msg_key)
        assert flag_to_seq(r"\Answered") in msg_seq
        assert flag_to_seq(r"\Seen") in msg_seq
        assert flag_to_seq("unseen") not in msg_seq


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_copy(mailbox_with_bunch_of_email):
    # We know this mailbox has messages numbered from 1 to 20.  We also know
    # since this is an initial state the msg_key, message sequence number, and
    # uid's are the same for each message (ie: 1 == 1 == 1)
    #
    mbox = mailbox_with_bunch_of_email

    # `mbox` creates `inbox`. We need a folder to copy messages to.
    #
    ARCHIVE = "Archive"
    archive_mh = mbox.server.mailbox.add_folder(ARCHIVE)

    # Let the server discover this folder and incorporate it.
    #
    await mbox.server.find_all_folders()
    dst_mbox = await mbox.server.get_mailbox(ARCHIVE)

    msg_keys = mbox.msg_keys
    msg_set = sorted(list(random.sample(msg_keys, 15)))

    src_uids, dst_uids = await mbox.copy(msg_set, dst_mbox)
    assert len(src_uids) == len(dst_uids)
    dst_msg_keys = dst_mbox.msg_keys
    assert len(dst_msg_keys) == len(msg_set)
    assert dst_msg_keys == archive_mh.keys()

    # in the source mailbox the message keys, message indices, and uid's are
    # all the same values for the same messages (because this is the initial
    # population of the mailbox it turns out this way).
    #
    assert src_uids == msg_set

    # Compare the messages.
    #
    for src_msg_key, src_uid, dst_msg_key, dst_uid in zip(
        msg_set, src_uids, dst_msg_keys, dst_uids
    ):
        src_msg = mbox.get_msg(src_msg_key)
        dst_msg = dst_mbox.get_msg(dst_msg_key)

        assert_email_equal(src_msg, dst_msg)

        _, uid = mbox.get_uid_from_msg(src_msg_key)
        assert uid == src_uid
        _, uid = dst_mbox.get_uid_from_msg(dst_msg_key)
        assert uid == dst_uid


####################################################################
#
@pytest.mark.asyncio
async def test_mbox_copy_verify_sequences(
    mailbox_with_bunch_of_email, incr_email, mailbox_instance
):
    """
    When copying messages to a mailbox make sure that the sequences get
    copied correctly as well.
    """
    mbox = mailbox_with_bunch_of_email

    # Mark some of the messages in the `inbox` as seen.
    #
    cmd = parse_cmd_from_msg(r"A005 STORE 1:5 +FLAGS.SILENT (\Seen)")
    async with cmd.ready_and_okay(mbox):
        msg_set = sorted(cmd.msg_set_as_set) if cmd.msg_set_as_set else []
        await mbox.store(
            msg_set, cmd.store_action, cmd.flag_list, cmd.uid_command
        )

    # Validate that we only have `unseen` and `Seen` sequences and only
    # messages 1-5 are in the `Seen` sequence and 6:* are in the `unseen`
    # sequence.
    #
    expected_sequences = {"unseen", "Seen", "Recent"}
    assert set(mbox.sequences.keys()) == expected_sequences
    mailbox_sequences = mbox.sequences
    assert set(mailbox_sequences.keys()) == expected_sequences
    expected_seen_sequence = set(range(1, 6))
    expected_unseen_sequence = set(range(6, mbox.num_msgs + 1))
    assert mbox.sequences["Seen"] == expected_seen_sequence
    assert mbox.sequences["unseen"] == expected_unseen_sequence
    assert mailbox_sequences["Seen"] == expected_seen_sequence
    assert mailbox_sequences["unseen"] == expected_unseen_sequence

    # Let the server discover this folder and incorporate it.
    #
    # `mbox` creates `inbox`. We need a folder to copy messages to.
    #
    ARCHIVE = "Archive"
    archive_mh = mbox.server.mailbox.add_folder(ARCHIVE)
    await mbox.server.find_all_folders()

    dst_mbox = await mbox.server.get_mailbox(ARCHIVE)

    # Copy messages 1-6.. 1-5 should be `Seen` and 6 should be `unseen`
    msg_set = list(range(1, 7))
    src_uids, dst_uids = await mbox.copy(msg_set, dst_mbox)
    assert len(src_uids) == len(dst_uids)
    dst_msg_keys = dst_mbox.msg_keys
    assert len(dst_msg_keys) == len(msg_set)
    assert dst_msg_keys == archive_mh.keys()

    # Validate the dest mailbox sequences
    #
    assert set(dst_mbox.sequences.keys()) == expected_sequences
    mailbox_sequences = dst_mbox.sequences
    assert set(mailbox_sequences.keys()) == expected_sequences
    assert dst_mbox.sequences["Seen"] == set(range(1, 6))
    assert dst_mbox.sequences["unseen"] == {6}
    assert mailbox_sequences["Seen"] == set(range(1, 6))
    assert mailbox_sequences["unseen"] == {6}


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_create_delete(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client_proxy = imap_user_server_and_client
    mbox = mailbox_with_bunch_of_email
    ARCHIVE = "Archive"
    SUB_FOLDER = "Archive/foo"

    # Make sure we can not create or delete `inbox` or one that is all digits.
    #
    with pytest.raises(InvalidMailbox):
        await Mailbox.create("inbox", server)

    with pytest.raises(InvalidMailbox):
        await Mailbox.delete("inbox", server)

    with pytest.raises(InvalidMailbox):
        await Mailbox.create("1234", server)

    await Mailbox.create(ARCHIVE, server)
    archive = await server.get_mailbox(ARCHIVE)

    # You can not create a mailbox if it already exists.
    #
    with pytest.raises(MailboxExists):
        await Mailbox.create(ARCHIVE, server)

    # Create a mailbox in a mailbox..
    #
    await Mailbox.create(SUB_FOLDER, server)

    # You can delete a mailbox that has children (it gets the `\Noselect`
    # attribute)
    #
    await Mailbox.delete(ARCHIVE, server)
    assert r"\Noselect" in archive.attributes

    # If you try to delete a mailbox with `\Noselect` and it has children
    # mailboxes, this also fails.
    #
    with pytest.raises(InvalidMailbox):
        await Mailbox.delete(ARCHIVE, server)

    # You can not select a `\Noselect` mailbox
    #
    with pytest.raises(No):
        await archive.selected(imap_client_proxy.cmd_processor)

    # Trying to create it will remove the `\Noselect` attribute..
    #
    await Mailbox.create(ARCHIVE, server)
    assert r"\Noselect" not in archive.attributes

    # and we will copy some messages into the Archive mailbox just to make
    # sure we can actually do stuff with it.
    #
    msg_keys = mbox.msg_keys
    msg_set = sorted(list(random.sample(msg_keys, 5)))
    src_uids, dst_uids = await mbox.copy(msg_set, archive)
    archive_msg_keys = archive.msg_keys
    assert len(dst_uids) == len(archive_msg_keys)
    assert archive.uids == dst_uids

    # And finally we will delete the subfolder and then the archive folder and
    # make sure that neither folder exists after the deletes.
    #
    await Mailbox.delete(SUB_FOLDER, server)
    with pytest.raises(NoSuchMailbox):
        await server.get_mailbox(SUB_FOLDER)
    await Mailbox.delete(ARCHIVE, server)
    with pytest.raises(NoSuchMailbox):
        await server.get_mailbox(ARCHIVE)

    # Also if we get the list of folders, neither ARCHIVE nor SUB_FOLDER should
    # existn.
    #
    mbox_names = []
    async for mbox_name, _ in Mailbox.list("", "*", server):
        mbox_names.append(mbox_name)
    assert ARCHIVE not in mbox_names
    assert SUB_FOLDER not in mbox_names


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_rename(
    mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client_proxy = imap_user_server_and_client
    inbox = mailbox_with_bunch_of_email
    NEW_MBOX_NAME = "new_mbox"

    # The mailbox we are moving must exist.
    #
    await Mailbox.create("nope", server)
    with pytest.raises(MailboxExists):
        await Mailbox.rename("inbox", "nope", server)

    # If you rename the inbox, you get a new mailbox with the contents of the
    # inbox moved to it.
    #
    msg_keys = inbox.msg_keys
    saved_msg_keys = msg_keys[:]
    await Mailbox.rename("inbox", NEW_MBOX_NAME, server)

    new_mbox = await server.get_mailbox(NEW_MBOX_NAME)
    new_msg_keys = new_mbox.msg_keys

    assert new_msg_keys == msg_keys
    assert new_mbox.uids == new_msg_keys

    msg_keys = inbox.msg_keys
    assert not msg_keys
    assert not inbox.uids
    assert not inbox.sequences

    # Create a new subordinate folder for `new_mbox` so we can make sure
    # the subfolders are treated right when the mailbox is renamed.
    #
    await Mailbox.create(NEW_MBOX_NAME + "/subfolder", server)

    # And now rename our `new_mbox`
    #
    NEW_NEW_NAME = "newnew_mbox"
    await Mailbox.rename(NEW_MBOX_NAME, NEW_NEW_NAME, server)

    folders = server.mailbox.list_folders()
    assert sorted(folders) == sorted(["inbox", "newnew_mbox", "nope"])

    with pytest.raises(NoSuchMailbox):
        _ = await server.get_mailbox(NEW_MBOX_NAME)

    # When we rename a mailbox it changes the name on the mailbox. the
    # object remains the same. It should have messages equivalent to the
    # origina inbox list.
    #
    new_new_mbox = await server.get_mailbox("newnew_mbox")
    assert new_mbox == new_new_mbox
    nnmsg_keys = new_new_mbox.msg_keys
    assert nnmsg_keys == saved_msg_keys
    assert nnmsg_keys == new_new_mbox.uids


####################################################################
#
@pytest.mark.asyncio
async def test_mailbox_list(
    faker, mailbox_with_bunch_of_email, imap_user_server_and_client
):
    server, imap_client_proxy = imap_user_server_and_client
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

    list_results = []
    async for mbox_name, attributes in Mailbox.list("", "*", server):
        mbox_name = mbox_name.lower() if mbox_name == "INBOX" else mbox_name
        assert mbox_name in folders
        list_results.append(mbox_name)

    assert sorted(folders) == sorted(list_results)


####################################################################
#
@pytest.mark.skip(reason="will impelement test later")
@pytest.mark.asyncio
async def test_append_when_other_msgs_also_added():
    """
    If we do an append, and other new messages have been added to the
    mailbox (by the mail delivery system) make sure everything works
    appropripately.
    """
    assert False


####################################################################
#
# Dataclass used for scenarios of testing which IMAP Commands would conflict
# with each other.
#
@dataclass(frozen=True)
class IMAPCommandConflictScenario:
    """
    A structure for representing a set of test case parameters.
    imap_command: The IMAP Command being tested
    executing_commands: A list of IMAP Commands currently executing.
    would_conflict: Whether the imap_command would conflict with any of the
                     executing commands.

    This lets us test the various combinations of commands to test if we can
    correcty predict which ones would conflict or not when trying to run at the
    same time.
    """

    imap_command: IMAPClientCommand
    executing_commands: List[IMAPClientCommand]
    sequences: Dict[str, set]
    would_conflict: bool


# Make sure all of our base commands are supported (and since no other commands
# are listed as executing, none of these would conflict.)
#
COMMANDS_WITH_NO_CONFLICTS = [
    pytest.param(
        IMAPCommandConflictScenario(
            imap_command=IMAPClientCommand(x).parse(),
            executing_commands=[],
            sequences={},
            would_conflict=False,
        ),
        id="no_conflicts_" + x.split(" ")[1],  # 'A01 SELECT INBOX' -> SELECT
    )
    for x in [
        "A001 APPEND foo (unseen) {11}\r\nFrom no one",
        "A001 CHECK foo",
        "A001 CLOSE",
        "A001 COPY 2:4 bar",
        "A001 DELETE foo",
        "A001 EXAMINE foo",
        "A001 EXPUNGE",
        "A001 FETCH 2:4 ALL",
        "A001 NOOP",
        "A001 RENAME foo bar",
        "A001 SEARCH unseen",
        "A001 SELECT foo",
        "A001 STATUS foo (RECENT)",
        "A001 STORE 2:4 FLAGS unseen",
    ]
]

# If these cmmands are executing, they would conflict with every other
# command. We are not testing every other command here, but will use NOOP which
# is the most innocuous of the commands.
#
CONFLICTING_COMMANDS = [
    pytest.param(
        IMAPCommandConflictScenario(
            imap_command=IMAPClientCommand("A01 NOOP").parse(),
            executing_commands=[
                IMAPClientCommand(x).parse(),
            ],
            sequences={},
            would_conflict=True,
        ),
        id="conflicting_" + x.split(" ")[1],  # 'A01 SELECT INBOX' -> SELECT
    )
    for x in [
        "A001 APPEND foo (unseen) {11}\r\nFrom no one",
        "A001 CHECK foo",
        "A001 CLOSE",
        "A001 DELETE foo",
        "A001 EXPUNGE",
        "A001 RENAME foo bar",
    ]
]

# These commands in most cases conflict if any other command is running, so we
# test against NOOP. The exceptions are CLOSE and EXPUNGE which only conflict
# with an executing task if the `\Deleted` sequence is not empty.
#
CONFLICTING_CMD_VS_NOOP = [
    pytest.param(
        IMAPCommandConflictScenario(
            imap_command=IMAPClientCommand(cmd).parse(),
            executing_commands=[IMAPClientCommand("A002 NOOP").parse()],
            sequences=sequences,
            would_conflict=conflicting,
        ),
        id=f"noop_vs_{cmd.split(' ')[1]}_{conflicting}",
    )
    for cmd, conflicting, sequences in [
        ["A001 APPEND foo (unseen) {11}\r\nFrom no one", True, {}],
        ["A001 CHECK foo", True, {}],
        ["A001 CLOSE", False, {}],
        ["A001 CLOSE", True, {"Deleted": {1}}],
        ["A001 DELETE foo", True, {}],
        ["A001 EXPUNGE", False, {}],
        ["A001 EXPUNGE", True, {"Deleted": {1}}],
        ["A001 RENAME foo bar", True, {}],
    ]
]


# COPY conflicts with STORE & FETCH if they operate on the same messages,
# unless the FETCH is a BODY.PEEK
#
COPY_VS_STORE_FETCH = [
    pytest.param(
        IMAPCommandConflictScenario(
            imap_command=IMAPClientCommand("A001 COPY 1:4 bar").parse(),
            executing_commands=[IMAPClientCommand(executing_cmd).parse()],
            sequences={},
            would_conflict=conflicting,
        ),
        id=f"copy_vs_{executing_cmd.split(' ')[1]}_{conflicting}",
    )
    for executing_cmd, conflicting in [
        ["A002 STORE 3 FLAGS unseen", True],
        ["A002 STORE 5 FLAGS unseen", False],
        ["A002 FETCH 3 BODY[HEADER]", True],
        ["A002 FETCH 5 BODY[HEADER]", False],
        ["A002 FETCH 3 BODY.PEEK[HEADER]", False],
    ]
]

# If a FETCH command could alter any sequences then it would conflict with any
# running command that depends on that sequence state not changing while
# running. Conversly, if the FETCH would not affect any sequence then it would
# notn conflict with any of EXAMINE, NOOP, SEARCH, SELECT, STATUS
#
FETCH_VS_MBOX_STATE_CMDS = [
    pytest.param(
        IMAPCommandConflictScenario(
            imap_command=IMAPClientCommand("A002 FETCH 3 BODY[HEADER]").parse(),
            executing_commands=[IMAPClientCommand(x).parse()],
            sequences={},
            would_conflict=conflicting,
        ),
        id=f"fetch_{x.split(' ')[1]}_peek",
    )
    for x, conflicting in [
        ("A001 EXAMINE foo", False),
        ("A001 NOOP", False),
        ("A001 SEARCH unseen", True),
        ("A001 SELECT foo", False),
        ("A001 STATUS foo (RECENT)", False),
    ]
]

FETCH_PEEK_VS_MBOX_STATE_CMDS = [
    pytest.param(
        IMAPCommandConflictScenario(
            imap_command=IMAPClientCommand(
                "A002 FETCH 3 BODY.PEEK[HEADER]"
            ).parse(),
            executing_commands=[IMAPClientCommand(x).parse()],
            sequences={},
            would_conflict=False,
        ),
        id=f"fetch_peek_{x.split(' ')[1]}_peek",
    )
    for x in [
        "A001 EXAMINE foo",
        "A001 NOOP",
        "A001 SEARCH unseen",
        "A001 SELECT foo",
        "A001 STATUS foo (RECENT)",
    ]
]

FETCH_VS_COPY_FETCH_STORE = [
    pytest.param(
        IMAPCommandConflictScenario(
            imap_command=IMAPClientCommand(cmd).parse(),
            executing_commands=[IMAPClientCommand(executing_cmd).parse()],
            sequences={},
            would_conflict=conflicting,
        ),
        id=f"fetch_{cmd.split(' ')[3]}_{executing_cmd.split(' ')[1]}_{conflicting}",
    )
    for cmd, conflicting, executing_cmd in [
        ["A002 FETCH 3 BODY[HEADER]", True, "A001 COPY 2:4 bar"],
        ["A002 FETCH 3 BODY[HEADER]", True, "A001 FETCH 2:4 ALL"],
        ["A002 FETCH 3 BODY[HEADER]", True, "A001 STORE 2:4 FLAGS unseen"],
        ["A002 FETCH 3 BODY[HEADER]", False, "A001 COPY 5 bar"],
        ["A002 FETCH 3 BODY[HEADER]", False, "A001 FETCH 5 ALL"],
        ["A002 FETCH 3 BODY[HEADER]", False, "A001 STORE 5 FLAGS unseen"],
        ["A002 FETCH 3 BODY.PEEK[HEADER]", False, "A001 COPY 2:4 bar"],
        ["A002 FETCH 3 BODY.PEEK[HEADER]", False, "A001 FETCH 2:4 ALL"],
        ["A002 FETCH 3 BODY.PEEK[HEADER]", True, "A001 STORE 2:4 FLAGS unseen"],
    ]
]

# I know this is 'search, select, status vs fetch, store. However have decided
# to let SELECT and STATUS not conflict with FETCH, STORE.
#
SEARCH_SELECT_STATUS: List[str] = [
    "A002 SEARCH unseen",
    # "A002 SELECT foo",
    # "A002 STATUS foo (RECENT)",
]
FETCH_STORE: List[Tuple[bool, str]] = [
    (False, "A001 NOOP"),
    (True, "A001 FETCH 2:4 BODY[HEADER]"),
    (False, "A001 FETCH 2:4 BODY.PEEK[HEADER]"),
    (True, "A001 STORE 2:4 FLAGS unseen"),
]

SEARCH_SELECT_STATUS_VS_FETCH_STORE = [
    pytest.param(
        IMAPCommandConflictScenario(
            imap_command=IMAPClientCommand(cmd).parse(),
            executing_commands=[IMAPClientCommand(executing_cmd).parse()],
            sequences={},
            would_conflict=conflicting,
        ),
        id=f"{cmd.split(' ')[1]}_{executing_cmd.split(' ')[1]}_{conflicting}",
    )
    for cmd, conflicting, executing_cmd in [
        [c, e[0], e[1]] for c in SEARCH_SELECT_STATUS for e in FETCH_STORE
    ]
]

STORE_VS_EXAMINE_NOOP_SEARCH_SELECT_STATUS = [
    pytest.param(
        IMAPCommandConflictScenario(
            imap_command=IMAPClientCommand("A002 STORE 3 FLAGS unseen").parse(),
            executing_commands=[IMAPClientCommand(x).parse()],
            sequences={},
            would_conflict=conflicting,
        ),
        id=f"store_vs_{x.split(' ')[1]}",
    )
    for x, conflicting in [
        ("A001 EXAMINE foo", False),
        ("A001 NOOP", False),
        ("A001 SEARCH unseen", True),
        ("A001 SELECT foo", False),
        ("A001 STATUS foo (RECENT)", False),
    ]
]

STORE_VS_STORE_FETCH_COPY = [
    pytest.param(
        IMAPCommandConflictScenario(
            imap_command=IMAPClientCommand(
                "A002 STORE 2:4 FLAGS unseen"
            ).parse(),
            executing_commands=[IMAPClientCommand(executing_cmd).parse()],
            sequences={},
            would_conflict=conflicting,
        ),
        id=f"copy_vs_{executing_cmd.split(' ')[1]}_{conflicting}",
    )
    for executing_cmd, conflicting in [
        ["A001 STORE 3 FLAGS unseen", True],
        ["A001 STORE 5 FLAGS unseen", False],
        ["A001 FETCH 3 BODY[HEADER]", True],
        ["A001 FETCH 5 BODY[HEADER]", False],
        ["A001 FETCH 3 BODY.PEEK[HEADER]", True],
        ["A001 COPY 2:5 bar", True],
        ["A001 COPY 5 bar", False],
    ]
]


####################################################################
#
@pytest.mark.parametrize(
    "scenario",
    COMMANDS_WITH_NO_CONFLICTS
    + CONFLICTING_COMMANDS
    + CONFLICTING_CMD_VS_NOOP
    + COPY_VS_STORE_FETCH
    + FETCH_VS_MBOX_STATE_CMDS
    + FETCH_PEEK_VS_MBOX_STATE_CMDS
    + FETCH_VS_COPY_FETCH_STORE
    + SEARCH_SELECT_STATUS_VS_FETCH_STORE
    + STORE_VS_EXAMINE_NOOP_SEARCH_SELECT_STATUS
    + STORE_VS_STORE_FETCH_COPY,
)
def test_would_conflict(
    scenario: IMAPCommandConflictScenario,
    mocker: MockerFixture,
    mailbox_with_bunch_of_email: Mailbox,
):
    """
    Test the variations of executing commands along with a new command to
    execute to see if the 'would conflict' or not
    """
    mbox = mailbox_with_bunch_of_email
    imap_cmd = scenario.imap_command
    # XXX Be nice if the scenario did this for us.. maybe make it a proper
    #     class with a method that takes the mbox and does the
    #     msg_setOt_msg_seq_set conversion for us.
    #
    imap_cmd.msg_set_as_set = mbox.msg_set_to_msg_seq_set(
        imap_cmd.msg_set, imap_cmd.uid_command
    )
    mbox.executing_tasks = []
    mbox.sequences.update(scenario.sequences)

    for cmd in scenario.executing_commands:
        cmd.msg_set_as_set = mbox.msg_set_to_msg_seq_set(
            cmd.msg_set, cmd.uid_command
        )
        mbox.executing_tasks.append(cmd)
    assert mbox.would_conflict(imap_cmd) == scenario.would_conflict


####################################################################
#
@pytest.mark.parametrize(
    # XXX Consider making this a frozen data attr class so it is eaier to read.
    "sequence_set,expected,uid_cmd,num_msgs",
    [
        (
            (2, (4, 7), 9, (12, "*")),
            {2, 4, 5, 6, 7, 9, 12, 13, 14, 15, 16, 17, 18, 19, 20},
            False,
            20,
        ),
        (
            (("*", 4), (5, 7)),
            {4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20},
            False,
            20,
        ),
        (
            (2, (4, 7), 9, (12, "*")),
            {2, 4, 5, 6, 7, 9, 12, 13, 14, 15, 16, 17, 18, 19, 20},
            True,
            20,
        ),
        (
            (("*", 4), (5, 7)),
            {4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20},
            True,
            20,
        ),
        (
            ((1, "*")),
            set(),
            True,
            0,
        ),
    ],
)
@pytest.mark.asyncio
async def test_msg_set_to_msg_seq_set(
    sequence_set, expected, uid_cmd, num_msgs, imap_user_server
) -> None:
    """
    Make sure that we can properly convert a parsed "sequence set" in to a
    set of the messages it indicates.

    The mbox fixture returns a mailbox with 20 messages in it. Since it is a
    newly created mailbox the message sequence numbers will be from 1 to 20,
    and the UID's will also be from 1 to 20.
    """
    NAME = "inbox"
    server = imap_user_server
    mbox = await server.get_mailbox(NAME)

    # For this test we only need to set 'num_msgs', 'msg_keys', and 'uids'
    # on the mailbox.
    #
    mbox.num_msgs = num_msgs
    mbox.uids = list(range(1, num_msgs + 1))
    mbox.msg_keys = list(range(1, num_msgs + 1))
    msg_set_as_set = mbox.msg_set_to_msg_seq_set(sequence_set, uid_cmd)
    assert msg_set_as_set == expected
