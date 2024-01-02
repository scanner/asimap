#!/usr/bin/env python
#
"""
A simple IMAP client to test our server with
"""
# system imports
#
import imaplib
from typing import Union


#############################################################################
#
def main():
    """
    Connect to imap server on localhost, using ssl, authenticate
    """
    imap = imaplib.IMAP4_SSL(host="127.0.0.1", port=993)
    print(f"IMAP Connection: {imap}")
    resp = imap.capability()
    print(f"Server capabilities: {resp}")
    resp = imap.login("tyler38@example.net", "x6tnBEr4&i")
    print(f"login response: {resp}")
    resp = imap.capability()
    print(f"Server capabilities (again): {resp}")
    ok, resp = imap.list()
    print(f"List response: {ok}")
    if ok.lower() == "ok":
        mbox: Union[str, bytes]
        for mbox in resp:
            mbox = str(mbox, "latin-1").split(" ")[-1]
            ok, r = imap.subscribe(mbox)
            if ok.lower() != "ok":
                print(f"Subscribe for {mbox} failed: {r}")
    resp = imap.lsub()
    print(f"lsub response: {resp}")
    mbox = "inbox"
    resp = imap.select(mbox)
    print(f"Select {mbox} response: {resp}")
    resp = imap.fetch(
        "1:*",
        "(INTERNALDATE UID RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS (date subject from to cc message-id in-reply-to references content-type x-priority x-uniform-type-identifier x-universally-unique-identifier list-id list-unsubscribe bimi-indicator bimi-location x-bimi-indicator-hash authentication-results dkim-signature)])",
    )
    print(f"FETCH response: {resp}")
    resp = imap.close()
    print(f"Close response: {resp}")
    print("Logging out")
    resp = imap.logout()
    print(f"Server LOGOUT: {resp}")


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
