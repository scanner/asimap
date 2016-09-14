#!/usr/bin/env python
#
# Copyright (C) 2007 Eric "Scanner" Luce
#
# File: $Id: imapsearch_test.py 1362 2007-07-21 20:57:39Z scanner $
#
"""
This test module runs the IMAPSearch engine through some of its
paces.
"""

import unittest
import commands
import os.path
import email
import mhlib
import pytz

from datetime import datetime

# mhimap imports
#
import mhimap.utils
from mhimap.IMAPSearch import IMAPSearch
from mhimap.Mailbox import MessageEntry

from mhimap.test.utils import folder_setup

############################################################################   
#
class IMAPSearchTest(unittest.TestCase):
    """
    Create a bunch of IMAPSearch objects of varying complexity
    and feed sets of messages through them to see what matches.
    """

    # Our tests including searching for messages with various flags set or
    # not set. To that end we define the message numbers of messages that
    # have various flags set. This way we can use this both to generate the
    # test data and to make sure the results of certain searches worked
    # properly. This also assumes that the messages with these numbers exist
    # in our folder.
    #
    SEEN_MSGS = [x for x in range(1,90)]
    ANSWERED_MSGS = [x for x in range(1,80)]
    FLAGGED_MSGS = [1,5,8,10,20]
    DELETED_MSGS = [1,2,3,4,5]
    DRAFT_MSGS = [7,10,13,21]
    RECENT_MSGS = [x for x in range(90,100)]

    ########################################################################
    #
    def setUp(self):
        folder_setup(self)
        
    ########################################################################
    #
    def search(self, imap_search):
        """
        A helper routine that will apply the given imap search object
        to all of the messages we have, and return a list of the
        message numbers for which the imap_server returned true.
        """
        result = []
        for msg_num, msg_entry in self.messages.iteritems():
            msg = msg_entry.msg
            if imap_search.match(msg, msg_entry, msg, msg_num, self.max_num,
                                 self.max_uid):
                result.append(msg_num)
        return result

    ########################################################################
    #
    def testAll(self):
        # NOTE: There are 99 messages in our 'oldinbox' that we use for this
        # this test. So tests that should match all messages will return a
        # list of range[1,100]
        #
        result = self.search(IMAPSearch(IMAPSearch.OP_ALL))
        self.assertEqual(result, range(1,100))

    ########################################################################
    #
    def testBefore(self):

        # Find all messages before the internal date of message #29
        # NOTE: The oldinbox mail is sorted by date.
        #
        match_date = self.messages[29].internal_date
        result = self.search(IMAPSearch(IMAPSearch.OP_BEFORE,
                                        date = match_date))
        self.assertEqual(result, range(1,29))

    ########################################################################
    #
    def testBody01(self):
        result = self.search(IMAPSearch(IMAPSearch.OP_BODY,
                                        string = "blowfish"))
        self.assertEqual(result, [38,50,51])

    ########################################################################
    #
    def testBody02(self):
        result = self.search(IMAPSearch(IMAPSearch.OP_BODY,
                                        string = "saturday"))
        self.assertEqual(result, [53,55,60,81,87,94])

    ########################################################################
    #
    def testHeader01(self):
        result = self.search(IMAPSearch(IMAPSearch.OP_HEADER,
                                        header = "from",
                                        string = "mellon"))
        self.assertEqual(result, [23, 25, 26,27])

    ########################################################################
    #
    def testHeader02(self):
        """
        This test makes sure that the header content match is down in lower
        case.. we will match messages with the string "Law" or "law" in the cc
        header.
        """
        result = self.search(IMAPSearch(IMAPSearch.OP_HEADER,
                                        header = "cC",
                                        string = "law"))
        self.assertEqual(result, [26, 27])

    ########################################################################
    #
    def testKeyword01(self):
        result = self.search(IMAPSearch(IMAPSearch.OP_KEYWORD,
                                        keyword = '\Deleted'))
        self.assertEqual(result, self.DELETED_MSGS)

    ########################################################################
    #
    def testKeyword02(self):
        result = self.search(IMAPSearch(IMAPSearch.OP_KEYWORD,
                                        keyword = '\Seen'))
        self.assertEqual(result, self.SEEN_MSGS)

    ########################################################################
    #
    def testLarger(self):
        result = self.search(IMAPSearch(IMAPSearch.OP_LARGER, n = 10000))
        self.assertEqual(result, [12,14,15,35,38,55,59,63,78,83,93,98,99])

    ########################################################################
    #
    def testMessageSet(self):
        msg_set = [1,2,3,4,(20,28),50,'*']
        result = self.search(IMAPSearch(IMAPSearch.OP_MESSAGE_SET,
                                        msg_set = msg_set))
        self.assertEqual(result, [1,2,3,4,20,21,22,23,24,25,26,27,28,50,99])

    ########################################################################
    #
    def testOn(self):
        match_date = self.messages[29].internal_date
        result = self.search(IMAPSearch(IMAPSearch.OP_ON,
                                        date = match_date))
        self.assertEqual(result, [29,30])

    ########################################################################
    #
    def testSentBefore(self):
        match_date = mhimap.utils.parsedate(self.messages[29].msg['date'])
        result = self.search(IMAPSearch(IMAPSearch.OP_SENTBEFORE,
                                        date = match_date))
        self.assertEqual(result, range(1,29))

    ########################################################################
    #
    def testSentOn(self):
        match_date = mhimap.utils.parsedate(self.messages[29].msg['date'])
        result = self.search(IMAPSearch(IMAPSearch.OP_SENTON,
                                        date = match_date))
        self.assertEqual(result, [29, 30])

    ########################################################################
    #
    def testSentSince(self):
        match_date = mhimap.utils.parsedate(self.messages[29].msg['date'])
        result = self.search(IMAPSearch(IMAPSearch.OP_SENTSINCE,
                                        date = match_date))
        self.assertEqual(result, range(30,100))

    ########################################################################
    #
    def testSince(self):
        # NOTE: The oldinbox mail is sorted by date.
        #
        match_date = self.messages[29].internal_date
        result = self.search(IMAPSearch(IMAPSearch.OP_SINCE,
                                        date = match_date))
        self.assertEqual(result, range(29,100))

    ########################################################################
    #
    def testSmaller(self):
        result = self.search(IMAPSearch(IMAPSearch.OP_SMALLER, n = 1299))
        self.assertEqual(result, [10,24,28,64,68,77,84])

    ########################################################################
    #
    def testText01(self):
        result = self.search(IMAPSearch(IMAPSearch.OP_TEXT,
                                        string = "tale"))
        self.assertEqual(result, [23,24,25,26,27,28,32,35])

    ########################################################################
    #
    def testUid(self):
        # Our uid's happen to match our message sequence numbers in this
        # test.
        #
        msg_set = [1,2,3,4,(20,28),50,'*']
        result = self.search(IMAPSearch(IMAPSearch.OP_MESSAGE_SET,
                                        msg_set = msg_set))
        self.assertEqual(result, [1,2,3,4,20,21,22,23,24,25,26,27,28,50,99])

    ########################################################################
    #
    def testAnd(self):
        result = self.search(IMAPSearch(IMAPSearch.OP_AND,
                     search_key = [IMAPSearch(IMAPSearch.OP_KEYWORD,
                                              keyword = '\Deleted'),
                                   IMAPSearch(IMAPSearch.OP_KEYWORD,
                                              keyword = '\Seen')]))
        self.assertEqual(result, self.DELETED_MSGS)

    ########################################################################
    #
    def testOr(self):
        result = self.search(IMAPSearch(IMAPSearch.OP_OR,
                     search_key = [IMAPSearch(IMAPSearch.OP_KEYWORD,
                                              keyword = '\Deleted'),
                                   IMAPSearch(IMAPSearch.OP_KEYWORD,
                                              keyword = '\Seen')]))
        self.assertEqual(result, self.SEEN_MSGS)

    ########################################################################
    #
    def testNot(self):
        result = self.search(IMAPSearch(IMAPSearch.OP_NOT,
                                 search_key = IMAPSearch(IMAPSearch.OP_KEYWORD,
                                                         keyword = '\Seen')))
        self.assertEqual(result, range(90,100))

    ########################################################################
    #
    def testComplex01(self):
        search = IMAPSearch(IMAPSearch.OP_AND,
                            search_key = [IMAPSearch(IMAPSearch.OP_KEYWORD,
                                                     keyword = "\Answered"),
                                          IMAPSearch(IMAPSearch.OP_SINCE,
                                                   date = datetime(2003,6,12,0,0,0,0,pytz.UTC)),
                                          IMAPSearch(IMAPSearch.OP_NOT,
                                                     search_key = IMAPSearch(IMAPSearch.OP_HEADER, header = 'cc', string = 'april'))])
        result = self.search(search)
        self.assertEqual(result, [14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 29,
                                  30, 31, 33, 34, 35, 36, 37, 38, 39, 40, 41,
                                  42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52,
                                  53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63,
                                  64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74,
                                  75, 76, 77, 78, 79])

############################################################################   
#
def suite():

    # NOTE: Since this data is all used readonly we create these files
    # via their tar archives and let them sit around for the entire
    # test session.
    #
    # Make sure to remove any temp data files that may have been
    # lieing around.
    #
    commands.getoutput("rm -rf /tmp/mh-imap-test")

    # We re-create our tail mail directory from scratch each time
    # this is run.
    #
    commands.getoutput("tar -C /tmp/ -x -f test-data/test-data.tar")

    suite = unittest.makeSuite(IMAPSearchTest)
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
    
