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
import asyncio
import logging
import logging.handlers
import optparse
import os
import pwd

# Application imports
#
import asimap

from .trace import trace
from .user_server import IMAPUserServer


############################################################################
#
def setup_option_parser():
    """
    This function uses the python OptionParser module to define an option
    parser for parsing the command line options for this script. This does not
    actually parse the command line options. It returns the parser object that
    can be used for parsing them.

    XXX Should probably pass the maildir as an option as well
    """
    parser = optparse.OptionParser(
        usage="%prog [options]",
        version=asimap.__version__,
    )

    parser.set_defaults(
        debug=False,
        logdir="/var/log/asimapd",
        trace_mode=False,
        trace_file=None,
        standalone_mode=False,
    )
    parser.add_option(
        "--debug",
        action="store_true",
        dest="debug",
        help="Emit debugging statements.",
    )
    parser.add_option(
        "--trace",
        action="store_true",
        dest="trace",
        help="The per user subprocesses will each open up a "
        "trace file and write to it all messages sent and "
        "received. One line per message. The message will be "
        "a timestamp, a relative timestamp, the direction of "
        "the message (sent/received), and the message itself. "
        "The tracefiles will be written to the log dir and "
        "will be named <username>-asimap.trace ",
    )
    parser.add_option(
        "--trace_file",
        action="store",
        type="string",
        dest="trace_file",
        help="If specified forces the "
        "trace to be written to the specified file instead "
        "of stderr or a file in the logdir.",
    )
    parser.add_option(
        "--logdir",
        action="store",
        type="string",
        dest="logdir",
        help="Path to the directory where log "
        "files are stored. Since this is a multiprocess server "
        "which each sub-process running as a different user "
        "we have a log file for the main server and then "
        "a separate log file for each sub-process. "
        "One sub-process per account. The main logfile "
        "will be called 'asimapd.log'. Each sub-process's "
        "logfile will be called '<imap user>-<local user>-"
        "asimapd.log'.",
    )
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
        level = logging.INFO

    log = logging.getLogger("asimap")
    log.setLevel(level)

    if options.logdir == "stderr":
        # Do not log to a file, log to stderr.
        #
        h = logging.StreamHandler()
    else:
        # Rotate on every 10mb, keep 5 files.
        #
        p = pwd.getpwuid(os.getuid())
        log_file_basename = os.path.join(
            options.logdir,
            f"{p.pw_name}-asimapd.log",
        )
        h = logging.handlers.RotatingFileHandler(
            log_file_basename, maxBytes=10485760, backupCount=5
        )
    h.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(created)s %(process)d "
        "%(levelname)s %(name)s %(message)s"
    )
    h.setFormatter(formatter)
    log.addHandler(h)

    if options.trace_enabled:
        log.debug("Tracing enabled")
        trace.trace_enabled = True
        trace.enable_tracing(options.logdir, options.trace_file)
        trace.trace({"trace_format": "1.0"})

    server = IMAPUserServer(
        options,
        os.getcwd(),
        debug=options.debug,
        trace=options.trace,
        trace_file=options.trace_file,
    )
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        log.warning("Keyboard interrupt, exiting")


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
