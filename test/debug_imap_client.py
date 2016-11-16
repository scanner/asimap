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
import imaplib

# XXX Should have the TestAuth module create a random username and
# password and write those in to a file relative to the asimap test
# directory and have this program read those in.
#
TEST_USERNAME = 'foobie'
TEST_PASSWORD = 'test'


#############################################################################
#
def main():
    # XXX Should have this program read some well known file for the
    # hostname / port to connect to.
    #
    imap = imaplib.IMAP4('127.0.0.1', 1143)
    imap.login(TEST_USERNAME, TEST_PASSWORD)
    imap.select()
    typ, data = imap.search(None, 'ALL')
    if data[0]:
        for num in data[0].split():
            typ, data = imap.fetch(num, '(RFC822)')
            print 'Message %s\n%s\n' % (num, data[0][1])
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
