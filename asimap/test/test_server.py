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
