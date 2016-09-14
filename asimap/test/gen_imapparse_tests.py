#!/usr/bin/env python
#
# Dumb little script to generate all the test cases for imapparse_test.py
# from a single dictionary.
#
# I wonder if I could do something like this using introspection and create
# these functions on the fly basically since they are all the same.
#
# This dictionary is a bunch of IMAP commands represented as complete
# strings. Some of them may be rather long.
#
# We are not testing for correctness of parsing the IMAP message in to a
# specific set of operations. We are simply making sure that the parser does
# not croak on any of these messages. (This is only testing success cases, not
# even if we actually parsed the message incorrectly.. just that we were able
# to parse the message.)
#
# The key is the "name" of the IMAP client message we are testing. The value
# is the actual message.
#
# BTW - these messages were cribbed directly from rfc2060's examples of each
# command.
#

import string

imap_messages = {
    'noop_uc': 'a002 NOOP\\r\\n',
    'noop_lc': 'a002 noop\\r\\n',
    'capability': 'abcd CAPABILITY\\r\\n',
    'logout': '1023 logout\\r\\n',
    'authenticate': 'a001 AUTHENTICATE KERBEROS_V4\\r\\n',
    'login1': 'a001 login smith sesame\\r\\n',
    'login2': 'a001 login {11}\r\nFRED FOOBAR {7}\r\nfat man\r\n',
    'select': 'A142 SELECT INBOX\\r\\n',
    'examine': 'A932 EXAMINE blurdybloop\\r\\n',
    'create': 'A003 CREATE owatagusiam/\\r\\n',
    'delete': 'A683 DELETE blurdybloop\\r\\n',
    'rename': 'A683 RENAME blurdybloop sarasoop\\r\\n',
    'subscribe': 'A002 SUBSCRIBE #news.comp.mail.mime\\r\\n',
    'unsubscribe': 'A002 UNSUBSCRIBE #news.comp.mail.mime\\r\\n',
    'list1': 'A101 LIST "" ""\\r\\n',
    'list2': 'A102 LIST #news.comp.mail.misc ""\\r\\n',
    'list3': 'A103 LIST /usr/staff/jones ""\\r\\n',
    'list4': 'A202 list ~/Mail/ %\\r\\n',
    'lsub': 'A002 LSUB "#news." "comp.mail.*"\\r\\n',
    'status': 'A042 STATUS blurdybloop (UIDNEXT MESSAGES)\\r\\n',
    'append': ('A003 APPEND saved-messages (\\Seen) {310}\\r\\nDate: '
               'Mon, 7 Feb 1994 21:52:25 -0800 (PST)\\r\\nFrom: Fred Foobar '
               '<foobar@Blurdybloop.COM>\\r\\nSubject: afternoon meeting'
               '\\r\\nTo: mooch@owatagu.siam.edu\\r\\nMessage-Id: '
               '<B27397-0100000@Blurdybloop.COM>\\r\\nMIME-Version: 1.0\\r'
               '\\nContent-Type: TEXT/PLAIN; CHARSET=US-ASCII\\r\\n\\r\\n'
               'Hello Joe, do you think we can meet at 3:30 tomorrow?\\r\\n'),
    'check': 'FXXZ CHECK\\r\\n',
    'close': 'A341 CLOSE\\r\\n',
    'expunge': 'A202 EXPUNGE\\r\\n',
    'search': 'A282 SEARCH FLAGGED SINCE 1-Feb-1994 NOT FROM "Smith"\\r\\n',
    'fetch': 'A654 FETCH 2:4 (FLAGS BODY[HEADER.FIELDS (DATE FROM)])\\r\\n',
    'store': 'A003 STORE 2:4 +FLAGS (\Deleted)\\r\\n',
    'copy': 'A003 COPY 2:4 MEETING\\r\\n',
    'uid': 'A999 UID FETCH 4827313:4828442 FLAGS\\r\\n',
    'x_command': 'A442 XPIG-LATIN\\r\\n'
    }

###########
#
# If we are invoked as a standalone program, just run the test suite defined
# in this module.
#
if __name__ == "__main__":
    for test_name in imap_messages.keys():
        fn_name = string.capitalize(test_name)
        print """    def test%s(self):
        '''%s'''
        msg = '%s'
        self.failIfEqual(self.parser.process(msg), None, '%s')
""" % (fn_name, test_name, imap_messages[test_name], test_name)
#
#
###########
