#!/usr/bin/env python
#
# File: $Id$
#
"""
The AS IMAP Daemon. This is intended to be run as root. It provides an
IMAP service that is typically backed by MH mail folders.
"""

# system imports
#
import sys
import os.path
import optparse
import logging
import socket
import asyncore
import traceback
import select

# Application imports
#
import asimap
import asimap.server
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

    parser.set_defaults(port = 993, interface = "0.0.0.0", debug = False,
                        ssl = True, ssl_certificate = None)

    parser.add_option("--port", action="store", type="int", dest="port",
                      help = "What port to listen on.")
    parser.add_option("--interface", action="store", type="string",
                      dest="interface", help = "The IP address to bind to.")
    parser.add_option("--debug", action="store_true", dest="debug",
                      help="Emit debugging statements. Do NOT daemonize.")
    parser.add_option("--no_ssl", action="store_false", dest="ssl",
                      help="Turn off SSL for the incoming IMAP4 "
                      "connections.")
    parser.add_option("--ssl_certificate", action="store", type="string",
                      dest="ssl_certificate", help="Path to your SSL "
                      "certificate.")
    return parser

#############################################################################
#
def main():
    """
    Our main entry point. Parse the options, set up logging, go in to
    daemon mode if necessary, setup the asimap library and start
    accepting connections.
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
    log = logging.getLogger("asimapd")

    if options.ssl and options.ssl_certificate is None:
        log.error("If SSL is enabled you need to provide a SSL certificate "
                  "via the --ssl_certificate option")
        exit(-1)

    # Using the location of the server program determine the location of
    # the user_server program (if it was not set via a command line option.)
    #
    user_server_program = os.path.abspath(os.path.join(os.path.dirname(sys.argv[0]),"asimapd_user.py"))

    # Make sure the user server program exists and is executable before we go
    # any further..
    #
    if not os.path.exists(user_server_program) or \
            not os.path.isfile(user_server_program):
        log.error("User server program does not exist or is not a file: '%s'" \
                      % user_server_program)
        exit(-1)

    # Set this as a variable in the asimap.user_server module.
    #
    log.debug("user server program is: '%s'" % user_server_program)
    asimap.user_server.set_user_server_program(user_server_program)

    try:
        server = asimap.server.IMAPServer(options)
    except socket.error, e:
        log.error("Unable to create server object on %s:%d: socket " \
                  "error: %s" % (options.interface, options.port, e))
        return

    # XXX We should do the loop inside of 'while True' and at the end of each
    #     loop run through all of the subprocess handles and call 'is_alive()'
    #     on them to reap them so that when they go away due to idleness we do
    #     not leave zombie processes waiting around for their parent to reap
    #     them.
    #
    #     We have to do this because subprocesses will stay around after they
    #     have been started up until they have been idle for a certain amount
    #     of time with no active clients.
    #
    asyncore.loop()
    # while True:
    #     try:
    #         asyncore.loop()
    #     except select.error, e:
    #         tb = traceback.format_exc()
    #         log.error("asyncore.loop() returned select.error: %s\n%s" % \
    #                       (str(e), tb))
    #     else:
    #         break

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
