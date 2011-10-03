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

    parser.set_defaults(port = 993, interface = "0.0.0.0", tls = True)

    parser.add_option("--port", action="store", type="int", dest="port",
                      help = "What port to listen on.")
    parser.add_option("--interface", action="store", type="string",
                      dest="interface", help = "The IP address to bind to.")
    parser.add_option("--no_tls", action="store_false", dest="tls",
                      help="Turn off TLS/SSL for the incoming IMAP4 "
                      "connections.")
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
    c = imaplib.IMAP4("localhost", 2021)
    c.login("test", "test")
    print "List: %s" % str(c.list(""))
    print "Select inbox: %s" % str(c.select("inbox"))
    print "Find unseen messages: %s" % str(c.search(None, 'unseen'))
    print "LSUB: %s" % str(c.lsub("", "*"))
    print "Fetch: %s" % str(c.fetch("1:2", "(FLAGS UID)"))
    print "Fetch 2:" 
    for res in c.fetch("1", "(FLAGS RFC822.SIZE INTERNALDATE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT REFERENCES IN-REPLY-TO MESSAGE-ID MIME-VERSION CONTENT-TYPE CONTENT-CLASS X-CALENDAR-ATTACHMENT X-MAILING-LIST X-LOOP LIST-ID LIST-POST MAILING-LIST ORIGINATOR X-LIST SENDER RETURN-PATH X-BEENTHERE)])"):
        print "   Got result: %s" % res
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
