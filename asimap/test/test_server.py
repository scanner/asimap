#!/usr/bin/env python
#
"""
Test the top level asimapd server through a series of integration tests.
"""
# system imports
#

# 3rd party imports
#
import pytest

# Project imports
#
from ..client import CAPABILITIES


####################################################################
#
@pytest.mark.integration
def test_server_capability(imap_server):
    """
    We want a high level test of the server, but do not want to get into it
    launching the subprocess for an authenticated user. Getting the
    'CAPABILITY' response from the server is good enough for that.
    """
    fixtures = imap_server
    imap = fixtures["client"]
    status, capabilities = imap.capability()
    assert status == "OK"
    assert str(capabilities[0], "ascii") == " ".join(CAPABILITIES)
    imap.logout()


####################################################################
#
@pytest.mark.integration
def test_server_login(imap_server, imap_user_server_program):
    """
    Try logging in to the server. This will also launch the subprocess and
    communicate with it.
    """
    fixtures = imap_server
    imap = fixtures["client"]
    status, capabilities = imap.capability()
    assert status == "OK"
    status, resp = imap.login(fixtures["user"].username, fixtures["password"])
    assert status == "OK"
    status, resp = imap.logout()
    assert status == "BYE"


####################################################################
#
@pytest.mark.integration
def test_server_list_status_select(
    bunch_of_email_in_folder, imap_server, imap_user_server_program
):
    """
    LIST, STATUS INBOX, SELECT INBOX
    """
    fixtures = imap_server
    imap = fixtures["client"]
    status, capabilities = imap.capability()
    assert status == "OK"
    status, resp = imap.login(fixtures["user"].username, fixtures["password"])
    assert status == "OK"
    status, resp = imap.list()
    status, resp = imap.status(
        "INBOX", "(messages recent uidnext uidvalidity unseen)"
    )
    status, resp = imap.select(mailbox="INBOX")
    status, resp = imap.fetch(
        "1:5", "(UID BODY[HEADER.FIELDS (TO FROM SUBJECT DATE)])"
    )
    status, resp = imap.uid(
        "FETCH",
        "1:5",
        "(INTERNALDATE UID RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS (date subject from to cc message-id in-reply-to references content-type x-priority x-uniform-type-identifier x-universally-unique-identifier list-id list-unsubscribe bimi-indicator bimi-location x-bimi-indicator-hash authentication-results dkim-signature x-spam-status x-spam-flag received-spf X-Forefront-Antispam-Report)])",
    )
    status, resp = imap.logout()
    assert status == "BYE"
