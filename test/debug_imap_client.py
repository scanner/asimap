#!/usr/bin/env python
#
# File: $Id$
#
"""
A quick little script to test an imap server by connecting to it
on localhost and logging in.

This is a playground for testing the imap commands we want to actually
use against a test server.
"""

# system imports
#
import os
import os.path
import imaplib
from datetime import datetime
import time

#############################################################################
#
def main():
    # Look for the credentials in a well known file in several
    # locations relative to the location of this file.
    #
    # XXX Should make a command line option to set the mail dir
    #     directory we exepct to use.
    #
    username = None
    password = None
    for path in ('test_mode', '../test_mode'):
        creds_file = os.path.join(os.path.dirname(__file__),
                                  path,
                                  "test_mode_creds.txt")
        print "Looking for creds file {}".format(creds_file)
        if os.path.exists(creds_file):
            print "Using credentials file {}".format(creds_file)
            username, password = open(creds_file).read().strip().split(':')
            break

    if username is None or password is None:
        raise RuntimeError("Unable to find test mode credentials")

    # Look for the address file in the same directory as the creds file
    #
    addr_file = os.path.join(os.path.dirname(creds_file), 'test_mode_addr.txt')
    addr, port = open(addr_file).read().strip().split(':')
    port = int(port)

    imap = imaplib.IMAP4(addr, port)
    imap.login(username, password)
    imap.select()
    imap.select('Archive')
    typ, data = imap.search(None, 'ALL')
    if data[0]:
        for num in data[0].split():
            if num in ('1', '20'):
                typ, data = imap.fetch(num, '(RFC822)')
                print 'Message %s\n%s\n' % (num, data[0][1])
    # imap.append('Archive', ('unseen'), None, 'Hello')
    big_msg = open('test/test_mode/big_email').read()
    imap.append('INBOX', ('unseen'), None, big_msg)
    imap.close()
    imap.logout()

############################################################################
############################################################################
#
# Here is where it all starts
#
if __name__ == '__main__':
    main()
#
############################################################################
############################################################################
