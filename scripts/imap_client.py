#!/usr/bin/env python
#
"""
A simple IMAP client to test our server with
"""
# system imports
#
import imaplib


#############################################################################
#
def main():
    """
    Connect to imap server on localhost, using ssl, authenticate
    """
    imap = imaplib.IMAP4_SSL(host="127.0.0.1", port=2121)
    print(f"IMAP Connection: {imap}")
    resp = imap.capability()
    print(f"Server capabilities: {resp}")
    resp = imap.login("tyler38@example.net", "x6tnBEr4&i")
    print(f"login response: {resp}")
    resp = imap.capability()
    print(f"Server capabilities (again): {resp}")
    # resp = imap.list()
    # print(f"List response: {resp}")
    print("Logging out")
    resp = imap.logout()
    print(f"Server LOGOUT: {resp}")
    # imap.close()


############################################################################
############################################################################
#
# Here is where it all starts
#
if __name__ == "__main__":
    main()
#
############################################################################
############################################################################
