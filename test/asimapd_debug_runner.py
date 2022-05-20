#!/usr/bin/env python
#
# File: $Id$
#
"""
This is a debug runner for the asimapd_user.py process.

It takes as input a IMAP trace file and runs the asimapd_user.py
process feeding it the trace file.  The trace file is expected to
contain both messages to the IMAP sub-process and responses from the
IMAP sub-process. If the responses we get back from the sub-process do
not match what is in the trace file for expected responses this
program will exit with a non-zero return code (NOTE: the program will
not exit early on the first error.. it will just set a non-zero return
code. Unless the '--exit-on-mismatch' flag is set. In that case we
will exit the first time we do not get the expected response back from
the IMAP server.)

This lets us play back arbitrary sets of IMAP messages to an
asimap.user_server and do a complete functional test of the imap
server. This lets us test everything except login, ssl, and
multiplexing.

NOTE: The asimapd_user.py program does its own logging. The log file
      will be in the working directory of this script.

NOTE: need to document the tracefile format and features.

Usage:
  asimapd_debug_runner.py [--asimapd_user=<user_server>] [--log=<logfile>]
                          [--quiet] [--no-rc] [--exit-on-mismatch]
                          <trace_file>...

Options:
  --version
  -h, --help        Show this text and exit
  -a <user_server>, --asimapd_user=<user_server>  The asimapd_user.py server
                    to run. [default: ../asimapd_user.py]
  -l <logfile>, --log=<logfile>  Write all of our output also to the named
                                 log file
  -q, --quiet  If specified do not write any of our output to standard out.
               It is expected that this would be used in combination
               with '--log' in a test rig.
  -n, --no-rc  If specified always exit with a '0' return code even if there
               were failures in the responses from the IMAP server that do
               not match the expected ones in the trace file.
  -e, --exit-on-mismatch  Exit immediately when the output from the IMAP
                          subprocess does not match the expected
                          output as set in the tracefile being played back.
"""

import fileinput

# system imports
#
import json

# 3rd party imports
#
from docopt import docopt

# Project imports
#
import asimap.user_server


########################################################################
########################################################################
#
class DebugRunner(object):
    """ """

    ####################################################################
    #
    def __init__(self, args):
        pass


#############################################################################
#
def main():
    """ """
    args = docopt(__doc__, version="0.1")
    user_server = args["<user_server>"]
    asimap.user_server.set_user_server_program(user_server)

    DebugRunner()


############################################################################
############################################################################
#
# Here is where it all starts
#
if __name__ == "__main__":
    main()
#
############################################################################
############################################################################
