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
import optparse
import logging

# Application imports
#
import asimap

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
                        tls = True)

    parser.add_option("--port", action="store", type="int", dest="port",
                      help = "What port to listen on.")
    parser.add_option("--interface", action="store", type="string",
                      dest="interface", help = "The IP address to bind to.")
    parser.add_option("--debug", action="store_true", dest="debug",
                      help="Emit debugging statements. Do NOT daemonize.")
    parser.add_option("--no_tls", action="store_false", dest="tls",
                      help="Turn off TLS/SSL for the incoming IMAP4 "
                      "connections.")
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
                        format="%(asctime)s %(created)s level=%(levelname)s "
                        "thread=%(thread)d name=%(name)s %(message)s")
    log = logging.getLogger("asimap")

    server = asimap.server.IMAPServer(options)
    asyncore.loop()
        

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
