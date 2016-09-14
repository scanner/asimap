#!/usr/bin/env python
#
# Copyright (C) 2005 Eric "Scanner" Luce
#
# File: $Id: imapparse_test.py 1456 2007-12-16 07:48:08Z scanner $
#
"""This module contains the unit tests for IMAPParse python module
"""

import unittest
from ..parse import IMAPClientCommand

TEST_DATA = [
    
]

class IMAPParseTest(unittest.TestCase):
    """This test case test a bunch of IMAP client messages of various amounts
    of complexity. The list of messages we attempt to parse is from the
    dictionary 'imap_messages' that is defined in this module.
    """

    def testRename(self):
        '''rename'''
        msg = 'A683 RENAME blurdybloop sarasoop\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testUid1(self):
        '''uid1'''
        msg = 'A999 UID FETCH 4827313:4828442 FLAGS\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testUid2(self):
        '''uid2'''
        msg = 'A999 UID SEARCH 1:100 UID 443:557\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testList4(self):
        '''list4'''
        msg = 'A202 list ~/Mail/ %\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testList1(self):
        '''list1'''
        msg = 'A101 LIST "" ""\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testList3(self):
        '''list3'''
        msg = 'A103 LIST /usr/staff/jones ""\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testList2(self):
        '''list2'''
        msg = 'A102 LIST #news.comp.mail.misc ""\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testSubscribe(self):
        '''subscribe'''
        msg = 'A002 SUBSCRIBE #news.comp.mail.mime\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testExamine(self):
        '''examine'''
        msg = 'A932 EXAMINE blurdybloop\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testClose(self):
        '''close'''
        msg = 'A341 CLOSE\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

#    def testX_command(self):
#        '''x_command'''
#        msg = 'A442 XPIG-LATIN\r\n'
#        self.failUnlessRaises(UnknownCommand,
#                              IMAPClientCommand,msg)

    def testCheck(self):
        '''check'''
        msg = 'FXXZ CHECK\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testAppend(self):
        '''append'''
        msg = ('A003 APPEND saved-messages (\Seen) {310}\r\nDate: Mon, 7 '
               'Feb 1994 21:52:25 -0800 (PST)\r\nFrom: Fred Foobar '
               '<foobar@Blurdybloop.COM>\r\nSubject: afternoon meeting\r\nTo:'
               ' mooch@owatagu.siam.edu\r\nMessage-Id: <B27397-0100000@'
               'Blurdybloop.COM>\r\nMIME-Version: 1.0\r\nContent-Type: '
               'TEXT/PLAIN; CHARSET=US-ASCII\r\n\r\nHello Joe, do you think '
               'we can meet at 3:30 tomorrow?\r\n\r\n')
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testAppend2(self):
        '''append2'''
        msg = ('A003 APPEND saved-messages (\Seen) "05-jan-1999 20:55:23 '
               '+0000" {310}\r\nDate: Mon, 7 Feb 1994 21:52:25 -0800 '
               '(PST)\r\nFrom: Fred Foobar <foobar@Blurdybloop.COM>\r\n'
               'Subject: afternoon meeting\r\nTo: mooch@owatagu.siam.edu'
               '\r\nMessage-Id: <B27397-0100000@Blurdybloop.COM>\r\n'
               'MIME-Version: 1.0\r\nContent-Type: TEXT/PLAIN; CHARSET='
               'US-ASCII\r\n\r\nHello Joe, do you think we can meet at '
               '3:30 tomorrow?\r\n\r\n')
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testAuthenticate(self):
        '''authenticate'''
        msg = 'a001 AUTHENTICATE KERBEROS_V4\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testCreate(self):
        '''create'''
        msg = 'A003 CREATE owatagusiam/\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testCreate1(self):
        '''create'''
        msg = 'A004 CREATE owatagusiam/blurdybloop\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testSelect(self):
        '''select'''
        msg = 'A142 SELECT INBOX\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testStore1(self):
        '''store1'''
        msg = 'A003 STORE 2:4 +FLAGS (\Deleted)\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testStore2(self):
        '''store2'''
        msg = 'A003 STORE 2:4 FLAGS \Seen\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testStore3(self):
        '''store3'''
        msg = 'A003 STORE 2:4 -FLAGS.SILENT (\Seen \Flagged)\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testStatus(self):
        '''status'''
        msg = 'A042 STATUS blurdybloop (UIDNEXT MESSAGES)\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testStatus1(self):
        '''status1'''
        msg = 'A042 STATUS blurdybloop (RECENT)\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testLogout(self):
        '''logout'''
        msg = '1023 logout\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testLsub(self):
        '''lsub'''
        msg = 'A002 LSUB "#news." "comp.mail.*"\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testCopy(self):
        '''copy'''
        msg = 'A003 COPY 2:4 MEETING\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testSearch(self):
        '''search'''
        msg = 'A282 SEARCH FLAGGED SINCE 1-Feb-1994 NOT FROM "Smith"\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testSearch2(self):
        '''search2'''
        msg = 'A282 SEARCH OR FLAGGED SINCE 1-Feb-1994 NOT FROM "Smith"\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testSearch3(self):
        '''search3'''
        msg = 'A282 SEARCH (OR FLAGGED 1:3,4,5,6) SINCE 1-Feb-1994 NOT FROM "Smith"\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testNoop_lc(self):
        '''noop_lc'''
        msg = 'a002 noop\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testExpunge(self):
        '''expunge'''
        msg = 'A202 EXPUNGE\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testNoop_uc(self):
        '''noop_uc'''
        msg = 'a002 NOOP\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testCapability(self):
        '''capability'''
        msg = 'abcd CAPABILITY\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testUnsubscribe(self):
        '''unsubscribe'''
        msg = 'A002 UNSUBSCRIBE #news.comp.mail.mime\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testLogin1(self):
        '''login1'''
        msg = 'a001 login smith sesame\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testLogin2(self):
        '''login2 - literal'''
        msg = 'a001 login {11}\r\nFRED FOOBAR {7}\r\nfat man\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch1(self):
        '''fetch1'''
        msg = 'A654 FETCH 2:4 (FLAGS BODY[HEADER.FIELDS (DATE FROM)])\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertEqual(str(p), 'A654 FETCH (2, 4) (FLAGS BODY[HEADER.FIELDS (DATE FROM)])')

    def testFetch2(self):
        '''fetch2'''
        msg = 'A654 FETCH 2:4 BODY\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch3(self):
        '''fetch3'''
        msg = 'A654 FETCH 2:4 BODY[]<0.2048>\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch4(self):
        '''fetch4'''
        msg = 'A654 FETCH 2:4 BODY[1.2.3.4.HEADER]\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch5(self):
        '''fetch5'''
        msg = 'A654 FETCH 2:4 BODY[HEADER]\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch6(self):
        '''fetch6'''
        msg = 'A654 FETCH 2:4 BODY[TEXT]\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch7(self):
        '''fetch7'''
        msg = 'A654 FETCH 2:4 BODY[1]\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch8(self):
        '''fetch8'''
        msg = 'A654 FETCH 2:4 BODY[3.HEADER]\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch9(self):
        '''fetch9'''
        msg = 'A654 FETCH 2:4 BODY[3.TEXT]\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch10(self):
        '''fetch10'''
        msg = 'A654 FETCH 2:4 BODY[3.1]\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch11(self):
        '''fetch11'''
        msg = 'A654 FETCH 2:4 BODY[4.1.MIME]\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch12(self):
        '''fetch12'''
        msg = 'A654 FETCH 2:4 BODY[4.2.HEADER]\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testFetch13(self):
        '''fetch13'''
        msg = 'A654 FETCH 2:4 BODY[4.2.2.1]\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testDelete1(self):
        '''delete1'''
        msg = 'A683 DELETE blurdybloop\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testDelete2(self):
        '''delete2'''
        msg = 'A685 DELETE foo/bar\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())

    def testDelete3(self):
        '''delete3'''
        msg = 'A684 DELETE foo\r\n'
        p = IMAPClientCommand(msg)
        p.parse()
        print str(p)
        self.assertIsNone(IMAPClientCommand(msg).parse())


def suite():
    suite = unittest.makeSuite(IMAPParseTest)
    return suite

###########
#
# If we are invoked as a standalone program, just run the test suite defined
# in this module.
#
if __name__ == "__main__":
    suite = suite()
    unittest.main()

#
#
###########
