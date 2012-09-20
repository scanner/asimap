#!/usr/bin/env python
#
# File: $Id$
#
"""
A simplistic IMAP test client to use against our server.
"""

# system imports
#
import imaplib
import time
import optparse
import getpass

############################################################################
#
def setup_option_parser():
    """
    This function uses the python OptionParser module to define an option
    parser for parsing the command line options for this script. This does not
    actually parse the command line options. It returns the parser object that
    can be used for parsing them.
    """
    parser = optparse.OptionParser(usage = "%prog [options]",
                                   version = "0.1")

    parser.set_defaults(port = 993, server = "127.0.0.1", tls = True)

    parser.add_option("--port", action="store", type="int", dest="port",
                      help = "What port to listen on.")
    parser.add_option("--server", action="store", type="string",
                      dest="server", help = "The address of the IMAP server.")
    parser.add_option("--no_tls", action="store_false", dest="tls",
                      help="Do not use TLS/SSL for IMAP connections")
    return parser

#############################################################################
#
def main():
    """
    Parse options.. connect to the IMAP server.. do some commands.
    """

    print "Debug is: %s" % str(__debug__)
    parser = setup_option_parser()
    (options, args) = parser.parse_args()
    imaplib.Debug = 1
    username = raw_input("IMAP Username: ")
    password = getpass.getpass("IMAP Password: ")
    if options.tls:
        c = imaplib.IMAP4_SSL(options.server)
    else:
        c = imaplib.IMAP4(options.server)

    print "Logging in as %s" % username
    c.login(username, password)
    print "Getting list of mailboxes"
    mbox_list = c.list("")
    for mbox_name in mbox_list[1]:
        print mbox_name
    # print "List: %s" % str(c.list(""))
    # print "Select inbox: %s" % str(c.select("inbox"))
    # print "Find unseen messages: %s" % str(c.search(None, 'unseen'))
    # print "LSUB: %s" % str(c.lsub("", "*"))
    # print "Fetch: %s" % str(c.fetch("1:2", "(FLAGS UID)"))
    # print "Fetch 2:"
    # for res in c.fetch("1", "(FLAGS RFC822.SIZE INTERNALDATE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT REFERENCES IN-REPLY-TO MESSAGE-ID MIME-VERSION CONTENT-TYPE CONTENT-CLASS X-CALENDAR-ATTACHMENT X-MAILING-LIST X-LOOP LIST-ID LIST-POST MAILING-LIST ORIGINATOR X-LIST SENDER RETURN-PATH X-BEENTHERE)])"):
    #     print "   Got result: %s" % res
    print "Bye bye: %s" % str(c.logout())
    # while True:
    #     print "Noop: %s" % str(c.noop())
    #     time.sleep(5)
    return

############################################################################
############################################################################
#
# Here is where it all starts
#
if __name__ == "__main__":
    main()
#
#
############################################################################
############################################################################
