#!/usr/bin/env python
#
# Copyright (C) 2007 Eric "Scanner" Luce
#
# File: $Id: functional_test.py 1459 2007-12-17 18:03:24Z scanner $
#
"""
This test module is a test of the whole IMAP command processing path,
using a test user and test authentication method.

We will take canned IMAP messages and feed them directly to a command
processor that will run those commands that will manipulate an IMAP
directory and produce results.

This does not test a real authentication system.  This does not test
an IMAP network server. It tests the IMAPProcess module (and in doing
so tests the User module, and all of the actual mh dir modules
(UserMHDir, Mailbox, etc.)
"""

import unittest
import commands

# mhimap imports
#
import mhimap.IMAPParse

from mhimap.Client import TestServerClient as Client
from mhimap.IMAPParse import IMAPClientCommand
from mhimap.Auth import User
from mhimap.Auth import TestAuth
from mhimap.IMAPProcess import AuthenticatedIMAPCommandProcessor
from mhimap.IMAPProcess import PasswordPreAuthenticatedIMAPCommandProcessor
from mhimap.UserMHDir import UserMHDir

############################################################################   
#
class FunctionalIMAPTest(unittest.TestCase):
    """
    A series of IMAP commands fed directly in to a imap command processor.
    """

    ########################################################################
    #
    def write(self, data):
        """
        The IMAP command processor invokes 'write' on the connection
        it is passed to return messages to the client. We hijack this
        by adding a 'write' method to our test object, and stash away
        the message so that we can check it after the command
        completes.
        """
        self.data += data

    ########################################################################
    #
    def shutdown(self):
        """
        Handler for the 'shutdown' method that is inovked on a
        connection object by the IMAPProcessor when the user logs out.
        """
        self.data = None
        
    ########################################################################
    #
    def output_message(self):
        print "DATA:\n%s" % self.data
        self.data = ""

    ########################################################################
    #
    def do_command(self, *args):
        """
        A helper to do a complete IMAP command cycle.
        o create the imap command parser, tack on \r\n to the string
        o parse the command,
        o run the command through the authenticated imap command processor
        o wait for it to finish

        The input is expected to be a bunch of strings. The *args list is
        concatenated together with '\r\n' and has '\r\n' appended to
        it before being compared as a string
        """
        command = '\r\n'.join(args) + '\r\n'
        self.imap_command = IMAPClientCommand(command + "\r\n")
        cmd = AuthenticatedIMAPCommandProcessor(self.client,
                                                self.imap_command,
                                                self.usermhdir)
        cmd.start()
        cmd.join()
        return self.data

    ########################################################################
    #
    def dataEquals(self, *args):
        """
        Asserts that the given input matches the data from the
        commands so far processed.

        If the match succeeds the data is zeroed in preparation for
        the next command.

        The input is expected to be a bunch of strings. The *args list is
        concatenated together with '\r\n' and has '\r\n' appended to
        it before being compared as a string
        """
        cmp_data = '\r\n'.join(args) + '\r\n'
        self.assertEqual(self.data, cmp_data)
        self.data = ""

    ########################################################################
    #
    def setUp(self):
        # Make sure to remove any temp data files that may have been
        # lieing around.
        #
        commands.getoutput("rm -rf /tmp/mh-imap-test")

        # We re-create our tail mail directory from scratch each time
        # this is run.
        #
        commands.getoutput("tar -C /tmp/ -x -f test-data/test-data.tar")

        self.data = ""
        self.auth_system = TestAuth()
        self.client = Client(self)
        self.usermhdir = None
        self.logged_out = False

        self.imap_command = IMAPClientCommand("a01 LOGIN scanner blip\r\n")
        cmd = PasswordPreAuthenticatedIMAPCommandProcessor(self.client,
                                                           self.imap_command,
                                                           self.auth_system)
        cmd.start()
        cmd.join()
        self.data = ""
        self.usermhdir = UserMHDir(self.client.user.mh)
        self.usermhdir.resync_all_mailboxes()
        
    ########################################################################
    #
    def tearDown(self):
        self.do_command('a01 LOGOUT')
        self.usermhdir.shutdown()

    ########################################################################
    #
    def testNoop(self):
        self.do_command('a01 NOOP')
        self.dataEquals('a01 OK NOOP completed')

    ########################################################################
    #
    # XXX When we add a partial data match test we can uncomment this command
    # XXX but for now we leave it out. Every platform you run it on will
    # XXX produce a different result
##     def testId(self):
##         self.do_command('a01 ID NIL')
##         self.dataEquals('* ID ("vendor" "Apricot Systematic" "name" '
##                         '"py-mh-imap" "version" "0.1" "command" '
##                         '"./mhimap/test/functional_test.py" "os" "freebsd6" '
##                         '"support-url" "http://trac.apricot.com/py-mh-imap")',
##                         'a01 OK ID completed')

##         self.do_command('a01 ID ("name" "sodr" "version" "19.34" "vendor" '
##                         '"Pink Floyd Music Limited")')
##         self.dataEquals('* ID ("vendor" "Apricot Systematic" "name" '
##                         '"py-mh-imap" "version" "0.1" "command" '
##                         '"./mhimap/test/functional_test.py" "os" "freebsd6" '
##                         '"support-url" "http://trac.apricot.com/py-mh-imap")',
##                         'a01 OK ID completed')

    ########################################################################
    #
    def testCapability(self):
        self.do_command('a01 CAPABILITY')
        self.dataEquals('* CAPABILITY IMAP4rev1 IDLE NAMESPACE ID UIDPLUS',
                        'a01 OK CAPABILITY completed')

    ########################################################################
    #
    def testNamespace(self):
        self.do_command('a01 NAMESPACE')
        self.dataEquals('* NAMESPACE (("" "/")) NIL NIL',
                        'a01 OK NAMESPACE completed')

    ########################################################################
    #
    def testList(self):
        self.do_command('a01 LIST "" ""')
        self.dataEquals('* LIST (\Noselect) "/" ""',
                        'a01 OK LIST completed')

        self.do_command('a01 LIST "" "*"')
        self.dataEquals('* LIST (\Marked) "/" cels',
                        '* LIST (\Marked) "/" fibby',
                        '* LIST (\Marked) "/" fibby/yahoo',
                        '* LIST (\Unmarked \Marked) "/" fibby/yahoo/bloop',
                        '* LIST (\Marked) "/" fooby',
                        '* LIST (\Marked) "/" ghibli-box',
                        '* LIST (\Unmarked \Marked) "/" inbox',
                        '* LIST (\Marked) "/" lsub-todelete',
                        '* LIST (\Marked) "/" oldinbox',
                        'a01 OK LIST completed')

    ########################################################################
    #
    def testSelect(self):
        self.do_command('a01 SELECT INBOX')
        self.dataEquals('* 0 EXISTS',
                        '* 0 RECENT',
                        '* OK [UNSEEN 1]',
                        '* OK [UIDVALIDITY 8]',
                        '* FLAGS ()',
                        '* OK [PERMANENTFLAGS (\*)]',
                        'a01 OK [READ-WRITE] SELECT completed')

        self.do_command('a01 SELECT fibby')
        self.dataEquals('* 2 EXISTS',
                        '* 2 RECENT',
                        '* OK [UNSEEN 1]',
                        '* OK [UIDVALIDITY 3]',
                        '* FLAGS (\Recent \Seen)',
                        '* OK [PERMANENTFLAGS (\Recent \Seen \*)]',
                        'a01 OK [READ-WRITE] SELECT completed')

        self.do_command('a01 SELECT fibby/yahoo')
        self.dataEquals('* 8 EXISTS',
                        '* 8 RECENT',
                        '* OK [UIDVALIDITY 4]',
                        '* FLAGS (\Recent \Seen)',
                        '* OK [PERMANENTFLAGS (\Recent \Seen \*)]',
                        'a01 OK [READ-WRITE] SELECT completed')

        self.do_command('a01 SELECT blarg')
        self.dataEquals('a01 NO \'"No such mailbox \\\'blarg\\\'"\'')

        self.do_command('a01 SELECT inbox')
        self.dataEquals('* 0 EXISTS',
                        '* 0 RECENT',
                        '* OK [UNSEEN 1]',
                        '* OK [UIDVALIDITY 8]',
                        '* FLAGS ()',
                        '* OK [PERMANENTFLAGS (\*)]',
                        'a01 OK [READ-WRITE] SELECT completed')

    ########################################################################
    #
    def testExamine(self):
        self.do_command('a01 EXAMINE INBOX')
        self.dataEquals('* 0 EXISTS',
                        '* 0 RECENT',
                        '* OK [UNSEEN 1]',
                        '* OK [UIDVALIDITY 8]',
                        '* FLAGS ()',
                        '* OK [PERMANENTFLAGS ()]',
                        'a01 OK [READ-ONLY] EXAMINE completed')

        self.do_command('a01 EXAMINE fibby')
        self.dataEquals('* 2 EXISTS',
                        '* 2 RECENT',
                        '* OK [UNSEEN 1]',
                        '* OK [UIDVALIDITY 3]',
                        '* FLAGS (\Recent \Seen)',
                        '* OK [PERMANENTFLAGS ()]',
                        'a01 OK [READ-ONLY] EXAMINE completed')

        self.do_command('a01 EXAMINE fibby/yahoo')
        self.dataEquals('* 8 EXISTS',
                        '* 8 RECENT',
                        '* OK [UIDVALIDITY 4]',
                        '* FLAGS (\Recent \Seen)',
                        '* OK [PERMANENTFLAGS ()]',
                        'a01 OK [READ-ONLY] EXAMINE completed')

        self.do_command('a01 EXAMINE blarg')
        self.dataEquals('a01 NO \'"No such mailbox \\\'blarg\\\'"\'')

        self.do_command('a01 EXAMINE inbox')
        self.dataEquals('* 0 EXISTS',
                        '* 0 RECENT',
                        '* OK [UNSEEN 1]',
                        '* OK [UIDVALIDITY 8]',
                        '* FLAGS ()',
                        '* OK [PERMANENTFLAGS ()]',
                        'a01 OK [READ-ONLY] EXAMINE completed')
        
    ########################################################################
    #
    def testCreate(self):
        self.do_command('a01 LIST "" "*"')
        self.dataEquals('* LIST (\Marked) "/" cels',
                        '* LIST (\Marked) "/" fibby',
                        '* LIST (\Marked) "/" fibby/yahoo',
                        '* LIST (\Unmarked \Marked) "/" fibby/yahoo/bloop',
                        '* LIST (\Marked) "/" fooby',
                        '* LIST (\Marked) "/" ghibli-box',
                        '* LIST (\Unmarked \Marked) "/" inbox',
                        '* LIST (\Marked) "/" lsub-todelete',
                        '* LIST (\Marked) "/" oldinbox',
                        'a01 OK LIST completed')

        self.do_command('a01 CREATE inbox')
        self.dataEquals('a01 NO "Can not create a mailbox named \'inbox\'"')

        self.do_command('a01 CREATE blarg')
        self.dataEquals('a01 OK CREATE completed')

        self.do_command('a01 CREATE blarg/blip')
        self.dataEquals('a01 OK CREATE completed')

        self.do_command('a01 CREATE zip/zap')
        self.dataEquals('a01 OK CREATE completed')

        self.do_command('a01 LIST "" "*"')
        self.dataEquals('* LIST (\Unmarked) "/" blarg',
                        '* LIST (\Unmarked) "/" blarg/blip',
                        '* LIST (\Marked) "/" cels',
                        '* LIST (\Marked) "/" fibby',
                        '* LIST (\Marked) "/" fibby/yahoo',
                        '* LIST (\Unmarked \Marked) "/" fibby/yahoo/bloop',
                        '* LIST (\Marked) "/" fooby',
                        '* LIST (\Marked) "/" ghibli-box',
                        '* LIST (\Unmarked \Marked) "/" inbox',
                        '* LIST (\Marked) "/" lsub-todelete',
                        '* LIST (\Marked) "/" oldinbox',
                        '* LIST (\Unmarked) "/" zip',
                        '* LIST (\Unmarked) "/" zip/zap',
                        'a01 OK LIST completed')

    ########################################################################
    #
    def testDelete(self):
        self.do_command('a01 LIST "" "*"')
        self.dataEquals('* LIST (\Marked) "/" cels',
                        '* LIST (\Marked) "/" fibby',
                        '* LIST (\Marked) "/" fibby/yahoo',
                        '* LIST (\Unmarked \Marked) "/" fibby/yahoo/bloop',
                        '* LIST (\Marked) "/" fooby',
                        '* LIST (\Marked) "/" ghibli-box',
                        '* LIST (\Unmarked \Marked) "/" inbox',
                        '* LIST (\Marked) "/" lsub-todelete',
                        '* LIST (\Marked) "/" oldinbox',
                        'a01 OK LIST completed')

        self.do_command('a01 DELETE inbox')
        self.dataEquals('a01 NO "Can not delete the mailbox named \'inbox\'"')

        self.do_command('a01 DELETE zip/zap')
        self.dataEquals('a01 NO "No such mailbox \'zip/zap\'"')

        self.do_command('a01 DELETE cels')
        self.dataEquals('a01 OK DELETE completed')

        self.do_command('a01 DELETE fibby')
        self.dataEquals('a01 OK DELETE completed')

        self.do_command('a01 LIST "" "*"')
        self.dataEquals('* LIST (\Noselect) "/" fibby',
                        '* LIST (\Marked) "/" fibby/yahoo',
                        '* LIST (\Unmarked \Marked) "/" fibby/yahoo/bloop',
                        '* LIST (\Marked) "/" fooby',
                        '* LIST (\Marked) "/" ghibli-box',
                        '* LIST (\Unmarked \Marked) "/" inbox',
                        '* LIST (\Marked) "/" lsub-todelete',
                        '* LIST (\Marked) "/" oldinbox',
                        'a01 OK LIST completed')

        self.do_command('a01 DELETE fibby/yahoo/bloop')
        self.dataEquals('a01 OK DELETE completed')

        self.do_command('a01 DELETE fibby/yahoo')
        self.dataEquals('a01 OK DELETE completed')

        self.do_command('a01 LIST "" "*"')
        self.dataEquals('* LIST (\Marked) "/" fooby',
                        '* LIST (\Marked) "/" ghibli-box',
                        '* LIST (\Unmarked \Marked) "/" inbox',
                        '* LIST (\Marked) "/" lsub-todelete',
                        '* LIST (\Marked) "/" oldinbox',
                        'a01 OK LIST completed')

    ########################################################################
    #
    def testRename(self):
        self.do_command('a01 RENAME inbox testinbox')
        self.dataEquals('a01 OK RENAME completed')

        self.do_command('a01 LIST "" "*"')
        self.dataEquals('* LIST (\Marked) "/" cels',
                        '* LIST (\Marked) "/" fibby',
                        '* LIST (\Marked) "/" fibby/yahoo',
                        '* LIST (\Unmarked \Marked) "/" fibby/yahoo/bloop',
                        '* LIST (\Marked) "/" fooby',
                        '* LIST (\Marked) "/" ghibli-box',
                        '* LIST (\Unmarked \Marked) "/" inbox',
                        '* LIST (\Marked) "/" lsub-todelete',
                        '* LIST (\Marked) "/" oldinbox',
                        '* LIST (\Unmarked) "/" testinbox',
                        'a01 OK LIST completed')

        self.do_command('a01 RENAME oldinbox testinbox')
        self.dataEquals('a01 NO "The destination mailbox \'testinbox\' exists."')

        self.do_command('a01 RENAME oldinbox oldinbox2')
        self.dataEquals('a01 OK RENAME completed')

        self.do_command('a01 LIST "" "*"')
        self.dataEquals('* LIST (\Marked) "/" cels',
                        '* LIST (\Marked) "/" fibby',
                        '* LIST (\Marked) "/" fibby/yahoo',
                        '* LIST (\Unmarked \Marked) "/" fibby/yahoo/bloop',
                        '* LIST (\Marked) "/" fooby',
                        '* LIST (\Marked) "/" ghibli-box',
                        '* LIST (\Unmarked \Marked) "/" inbox',
                        '* LIST (\Marked) "/" lsub-todelete',
                        '* LIST (\Marked) "/" oldinbox2',
                        '* LIST (\Unmarked) "/" testinbox',
                        'a01 OK LIST completed')

    ########################################################################
    #
    def testSubscribeUnsubscribe(self):
        self.do_command('a01 SUBSCRIBE INBOX')
        self.dataEquals('a01 OK SUBSCRIBE completed')

        self.do_command('a01 UNSUBSCRIBE INBOX')
        self.dataEquals('a01 OK UNSUBSCRIBE completed')

        self.do_command('a01 SUBSCRIBE ghibli-box')
        self.dataEquals('a01 OK SUBSCRIBE completed')

        self.do_command('a01 UNSUBSCRIBE ghibli-box')
        self.dataEquals('a01 OK UNSUBSCRIBE completed')

        self.do_command('a01 UNSUBSCRIBE cels')
        self.dataEquals('a01 NO \'Not subscribed to mailbox cels\'')
        
    ########################################################################
    #
    def testLsub(self):
        self.do_command('a01 SUBSCRIBE INBOX')
        self.dataEquals('a01 OK SUBSCRIBE completed')

        self.do_command('a01 SUBSCRIBE ghibli-box')
        self.dataEquals('a01 OK SUBSCRIBE completed')

        self.do_command('a01 LSUB "" "*"')
        self.dataEquals('* LIST (\Marked) "/" ghibli-box',
                        '* LIST (\Unmarked \Marked) "/" inbox',
                        'a01 OK LSUB completed')

        self.do_command('a01 LSUB "" "*ghibli*"')
        self.dataEquals('* LIST (\Marked) "/" ghibli-box',
                        'a01 OK LSUB completed')
        
        self.do_command('a01 SUBSCRIBE lsub-todelete')
        self.dataEquals('a01 OK SUBSCRIBE completed')

        self.do_command('a01 LSUB "" "lsub*"')
        self.dataEquals('* LIST (\Marked) "/" lsub-todelete',
                        'a01 OK LSUB completed')

        # XXX This is not correct currently. Deleting a mailbox that is
        # XXX subscribed to MUST NOT make it leave the client's list of
        # XXX subscribed mailboxes. We need to set the \Noselect attribute
        # XXX on the mailbox (and delete it when all of the subscribed clients
        # XXX go away.
        #
        self.do_command('a01 DELETE lsub-todelete')
        self.dataEquals('a01 OK DELETE completed')

        self.do_command('a01 LSUB "" "lsub*"')

    ########################################################################
    #
    def testStatus(self):
        self.do_command("A042 STATUS blurdybloop (UIDNEXT MESSAGES)")
        self.dataEquals('A042 NO \'"No such mailbox \\\'blurdybloop\\\'"\'')

        self.do_command('a01 SELECT inbox')
        self.data = ""

        self.do_command("A042 STATUS oldinbox (UIDNEXT MESSAGES RECENT UNSEEN "
                        "UIDVALIDITY)")
        self.dataEquals('* STATUS oldinbox (UIDNEXT 100 MESSAGES 99 RECENT '
                        '99 UNSEEN 0 UIDVALIDITY 10)',
                        'A042 OK STATUS completed')

        self.do_command('a01 SELECT oldinbox')
        self.dataEquals('* 99 EXISTS',
                        '* 99 RECENT',
                        '* OK [UIDVALIDITY 10]',
                        '* FLAGS (\Recent \Seen)',
                        '* OK [PERMANENTFLAGS (\Recent \Seen \*)]',
                        'a01 OK [READ-WRITE] SELECT completed')

    ########################################################################
    #
    def testAppend(self):
        self.do_command("a01 CREATE saved-messages")
        self.dataEquals('a01 OK CREATE completed')

        self.do_command('A003 APPEND saved-messages (\Seen) {310}',
                        'Date: Mon, 7 Feb 1994 21:52:25 -0800 (PST)',
                        'From: Fred Foobar <foobar@Blurdybloop.COM>',
                        'Subject: afternoon meeting',
                        'To: mooch@owatagu.siam.edu',
                        'Message-Id: <B27397-0100000@Blurdybloop.COM>',
                        'MIME-Version: 1.0',
                        'Content-Type: TEXT/PLAIN; CHARSET=US-ASCII',
                        '',
                        'Hello Joe, do you think we can meet at 3:30 tomorrow?',
                        '')
        self.dataEquals('A003 OK [APPENDUID 11 1] APPEND completed')

        self.do_command("A042 STATUS saved-messages (UIDNEXT MESSAGES RECENT "
                        "UNSEEN UIDVALIDITY)")
        self.dataEquals("* STATUS saved-messages (UIDNEXT 2 MESSAGES 1 "
                        "RECENT 1 UNSEEN 0 UIDVALIDITY 11)",
                        "A042 OK STATUS completed")

        self.do_command('A003 APPEND saved-messages (\Seen \Flagged) '
                        '"05-jan-1999 20:55:23 +0000" {310}',
                        'Date: Mon, 7 Feb 1994 21:52:25 -0800 (PST)',
                        'From: Fred Foobar <foobar@Blurdybloop.COM>',
                        'Subject: afternoon meeting',
                        'To: mooch@owatagu.siam.edu',
                        'Message-Id: <B27398-0100000@Blurdybloop.COM>',
                        'MIME-Version: 1.0',
                        'Content-Type: TEXT/PLAIN; CHARSET=US-ASCII',
                        '',
                        'Hello Joe, do you think we can meet at 3:30 tomorrow?',
                        '')
        self.dataEquals('A003 OK [APPENDUID 11 2] APPEND completed')

        self.do_command("A042 STATUS saved-messages (UIDNEXT MESSAGES RECENT "
                        "UNSEEN UIDVALIDITY)")
        self.dataEquals('* STATUS saved-messages (UIDNEXT 3 MESSAGES 2 '
                        'RECENT 2 UNSEEN 0 UIDVALIDITY 11)',
                        'A042 OK STATUS completed')

        self.do_command("a01 SELECT saved-messages")
        self.dataEquals('* 2 EXISTS',
                        '* 2 RECENT',
                        '* OK [UIDVALIDITY 11]',
                        '* FLAGS (\Seen \Recent \Flagged)',
                        '* OK [PERMANENTFLAGS (\Seen \Recent \Flagged \*)]',
                        'a01 OK [READ-WRITE] SELECT completed')

        self.do_command('A003 APPEND saved-messages (\Flagged) '
                        '"05-jan-1999 20:55:23 +0000" {310}',
                        'Date: Mon, 7 Feb 1994 21:52:25 -0800 (PST)',
                        'From: Fred Foobar <foobar@Blurdybloop.COM>',
                        'Subject: afternoon meeting',
                        'To: mooch@owatagu.siam.edu',
                        'Message-Id: <B27398-0100000@Blurdybloop.COM>',
                        'MIME-Version: 1.0',
                        'Content-Type: TEXT/PLAIN; CHARSET=US-ASCII',
                        '',
                        'Hello Joe, do you think we can meet at 3:30 tomorrow?',
                        '')
        self.dataEquals('* 3 EXISTS',
                        '* 3 RECENT',
                        'A003 OK [APPENDUID 11 3] APPEND completed')

    ########################################################################
    #
    def testSelectFibby(self):
        self.do_command('a01 SELECT fibby')
        self.dataEquals('* 2 EXISTS',
                        '* 2 RECENT',
                        '* OK [UNSEEN 1]',
                        '* OK [UIDVALIDITY 3]',
                        '* FLAGS (\Recent \Seen)',
                        '* OK [PERMANENTFLAGS (\Recent \Seen \*)]',
                        'a01 OK [READ-WRITE] SELECT completed')

    ########################################################################
    #
    def testSelectOldInbox(self):
        self.do_command('a01 SELECT oldinbox')
        self.dataEquals('* 99 EXISTS',
                        '* 99 RECENT',
                        '* OK [UIDVALIDITY 10]',
                        '* FLAGS (\Recent \Seen)',
                        '* OK [PERMANENTFLAGS (\Recent \Seen \*)]',
                        'a01 OK [READ-WRITE] SELECT completed')

    ########################################################################
    #
    def testCheck(self):
        self.testSelectFibby()

        self.do_command('a02 CHECK')
        self.dataEquals('a02 OK CHECK completed')

    ########################################################################
    #
    def testClose(self):
        self.testSelectFibby()

        self.do_command('A341 CLOSE')
        self.dataEquals('A341 OK CLOSE completed')

        self.do_command('a02 CHECK')
        self.dataEquals("a02 BAD 'client is not in the selected state'")

    ########################################################################
    #
    # XXX NOTE: WE will have more expunge tests when we can set flags on 
    # XXX messages
    def testExpunge01(self):
        self.testSelectFibby()

        self.do_command('A202 EXPUNGE')
        self.dataEquals('A202 OK EXPUNGE completed')

    ########################################################################
    #
    def testSearch01(self):
        self.testSelectOldInbox()

        self.do_command('A282 SEARCH SINCE 9-Jul-2003 BEFORE 14-Jul-2003 NOT CC "april"')
        self.dataEquals('* SEARCH 18 19 20 21 22 23 29 30',
                        'A282 OK SEARCH completed')

    ########################################################################
    #
    def testSearch02(self):
        self.testSelectOldInbox()

        self.do_command('A282 SEARCH SINCE 9-Jul-2003 BEFORE 14-Jul-2003 CC "april"')
        self.dataEquals('* SEARCH 24 25 26 27 28',
                        'A282 OK SEARCH completed')

    ########################################################################
    #
    def testSearch03(self):
        self.testSelectOldInbox()

        self.do_command('A282 SEARCH (OR CC "april" 1:3,4,5,6,*) SINCE 1-Feb-1994 NOT FROM "scanner"')
        self.dataEquals('* SEARCH 1 2 3 4 5 6 24 25 26 27 28 32 99',
                        'A282 OK SEARCH completed')

    ########################################################################
    #
    def testFetch01(self):
        self.testSelectOldInbox()
        self.do_command('A282 FETCH 2:4 BODY[]')
        self.dataEquals('')
        
############################################################################   
#
def suite():

    suite = unittest.makeSuite(FunctionalIMAPTest)
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
