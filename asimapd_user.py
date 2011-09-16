#!/usr/bin/env python
#
# File: $Id$
#
"""
This is the 'user mail store' agent for the asimpad server. This is invoked as
a subprocess by asimapd when a user has authenticated.

It runs as the user whose mailbox is being accessed.

All IMAP connections authenticated as the same user will all use the same
instance of the asimapd_user.py process.

It expects to be run within the directory where the user's asimapd db file for
their mail spool is.

It accepts one command line arguments: --debug which causes extra logging to
happen.

XXX We communicate with the server via localhost TCP sockets. We REALLY should
    set up some sort of authentication key that the server must use when
    connecting to us. Perhaps we will use stdin for that in the
    future. Otherwise this is a bit of a nasty security hole.
"""

# system imports
#
import os
import sys
import optparse
import logging
import asyncore

# Application imports
#
import asimap
import asimap.user_server

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
                                   version = asimap.__version__)

    parser.set_defaults(debug = False)
    parser.add_option("--debug", action="store_true", dest="debug",
                      help="Emit debugging statements.")
    return parser

#############################################################################
#
def main():
    """
    Setup our logger.

    Find and open the mail spool's database.

    Setup the asynchat server to listen for connections from the asimapd
    server.

    Loop.
    """

    parser = setup_option_parser()
    (options, args) = parser.parse_args()

    # If 'options.debug' is true we log at the debug level. Otherwise
    # log at warning.
    #
    if options.debug:
        level = logging.DEBUG
    else:
        level = logging.WARNING

    logging.basicConfig(level=level,
                        format="%(asctime)s %(created)s %(process)d "
                        "%(levelname)s %(name)s %(message)s")
    log = logging.getLogger("asimap_user")

    server = asimap.user_server.IMAPUserServer(options, os.getcwd())

    # Print on stdout the port we are listening on so that the asimapd server
    # knows how to talk to us.
    #
    ip,port = server.address

    # We need to make sure stdout is unbuffered so that whatever we write here
    # will be immediately be sent to our calling process instead of waiting
    # for however many bytes stdout wants before it flushes the the output.
    #
    sys.stdout = os.fdopen(sys.stdout.fileno(), "w", 0)
    sys.stdout.write("%d\n" % port)
    sys.stdout.flush()
    sys.stdout.close()

    # And now loop forever.. breaking out of the loop every now and then to
    # see if we have had no active clients for awhile (and if we do not then
    # we exit.)
    #
    log.info("Starting main loop.")
    while True:
        asyncore.loop(count = 30)

        # XXX Is this kosher? Using the length of the global socket_map
        #     to see if there any channels outside of our listening one?
        #
        if len(asyncore.socket_map) == 1:
            break

    # Exiting!
    #
    log.info("Idle for at least 15 minutes. Exiting.")
    asyncore.close_all()

    # Close our handle to the sqlite3 database.
    #
    server.db.commit()
    server.db.close()

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
