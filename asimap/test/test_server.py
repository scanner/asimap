#!/usr/bin/env python
#
"""
Test the top level asimapd server through a series of integration tests.
"""
import asyncio
import imaplib

# system imports
#
import ssl
import threading
import time

# 3rd party imports
#
import pytest

# Project imports
#
from ..server import AsyncIMAPServer


####################################################################
#
@pytest.mark.integration
def test_server_capabilities(
    faker, ssl_certs, user_factory, password_file_factory
):
    """
    We want a high level test of the server, but do not want to get into it
    launching the subprocess for an authenticated user. Getting the
    'CAPABILITY' response from the server is good enough for that.
    """
    # XXX Maybe we should make a fixture that returns a server already
    #     running.. Yeah, I will do that once I get this test working.
    #
    ca, server_cert = ssl_certs
    user = user_factory()
    pw_file = password_file_factory([user])
    host = "127.0.0.1"
    port = faker.pyint(min_value=1024, max_value=65535)

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_cert.configure_cert(ssl_context)
    server = AsyncIMAPServer(host, port, ssl_context, pw_file)

    ############################
    #
    # start a mini server.. how cute
    #
    def start_server():
        asyncio.run(server.run())

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # Sleep for a teeny bit to let our server actually start up
    #
    time.sleep(0.1)

    try:
        client_ssl_context = ssl.create_default_context()
        ca.configure_trust(client_ssl_context)
        imap = imaplib.IMAP4_SSL(
            host=host, port=port, ssl_context=client_ssl_context, timeout=1
        )
        capabilities = imap.capability()
        print(f"Capabilities: {capabilities}")
        imap.logout()
    finally:
        # And shutdown the server.
        #
        server.asyncio_server.close()
