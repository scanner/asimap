#!/usr/bin/env python
#
# File: $Id$
#
"""
Setup our options and offer an optparse for command line argument parsing.
"""

# system imports
#
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
    parser = optparse.OptionParser(usage = "%prog [options] ",
                                   version = "%prog %s" % __version__)

    parser.set_defaults(port = 993, interface = "0.0.0.0", debug = False,
                        no_tls = False)

    parser.add_option("--port", action="store", type="int", dest="port",
                      help = "What port to listen on.")
    parser.add_option("--interface", action="store", type="string",
                      dest="interface", help = "The IP address to bind to.")
    parser.add_option("--debug", action="store_true", dest="debug",
                      help="Emit debugging statements. Do NOT daemonize.")
    parser.add_option("--no_tls", action="store_true", dest="no_tls",
                      help="Turn off TLS/SSL for the incoming IMAP4 "
                      "connections.")
    return parser

############################################################################
############################################################################
#
parser = setup_option_parser()

options = None
