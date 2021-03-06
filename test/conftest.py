#!/usr/bin/env python
#
# File: $Id$
#
"""
pytest fixtures for testing `asimap`
"""

# system imports
#

# project imports
#
import pytest
import ssl


####################################################################
#
def create_test_users(tmpdir):
    """
    Create a auth yaml file with some test users in it.
    """
    raise NotImplementedError


####################################################################
#
@pytest.fixture(scope='session')
def ssl_certificate(tmpdir):
    """
    Generate a SSL certificate for use by `asimap` in tests.

    Returns the file path to the certificate
    """
    raise NotImplementedError


####################################################################
#
@pytest.fixture(scope='session')
def ssl_context(ssl_certificate):
    """
    Generate and return a SSL context that has its own private CA and
    such so we can test SSL as part of our test suite.
    """
    # Create certificate

    # Store in temp file

    ctx = ssl.SSLContext(ssl.PROTOCOL_TSL)
    ctx.verify_mode = ssl.CERT_NONE

    # XXX Do not remove until we actually properly setup the context
    #
    raise NotImplementedError
