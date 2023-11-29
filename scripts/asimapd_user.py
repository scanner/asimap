#!/usr/bin/env python
#
# File: $Id$
#
"""
This is the 'user mail store' agent for the asimpad server. This is invoked
as a subprocess by asimapd when a user has authenticated. It is not intended to
be run directly from the command line by a user.

It runs as the user whose mailbox is being accessed.

All IMAP connections authenticated as the same user will all use the same
instance of the asimapd_user.py process.

It expects to be run within the directory where the user's asimapd db file for
their mail spool is.

Usage:
  asimapd_user.py [--trace] [--logdir=<logdir>] [--debug] <username>

Options:
  --version
  -h, --help    Show this text and exit
  --debug       Debugging output messages enabled

  --logdir=<d>  Path to the directory where log files are stored. Since this is
                a multiprocess server which each sub-process running as a
                different user we have a log file for the main server and then
                a separate log file for each sub-process. One sub-process per
                user. The main logfile will be called 'asimapd.log'. Each
                sub-process's logfile will be called '<user>-asimapd.log'. If
                this is set to 'stderr' then we will not log to a file but emit
                all messages for all processes on stderr. [default: stderr]

  --trace       The per user subprocesses will each open up a trace file and
                write to it all messages sent and received. One line per
                message. The message will be a timestamp, a relative timestamp,
                the direction of the message (sent/received), and the message
                itself.The tracefiles will be written to the log dir and will
                be named <username>-asimap.trace. Traces will be written to the
                directory specified by `--logdir` (or stderr if not specified.)

XXX We communicate with the server via localhost TCP sockets. We REALLY should
    set up some sort of authentication key that the server must use when
    connecting to us. Perhaps we will use stdin for that in the
    future. Otherwise this is a bit of a nasty security hole.
"""
# system imports
#
import asyncio
import logging
from pathlib import Path

# 3rd party imports
#
from docopt import docopt
from dotenv import load_dotenv
from rich.traceback import install as rich_install

# Application imports
#
import asimap.trace
from asimap import __version__ as VERSION
from asimap.user_server import IMAPUserServer
from asimap.utils import setup_asyncio_logging, setup_logging

rich_install(show_locals=True)

logger = logging.getLogger("asimapd_user")


#############################################################################
#
async def create_and_start_user_server(
    maildir: Path, debug: bool, trace_enabled: bool
):
    server = await IMAPUserServer.new(
        Path.cwd(), debug=debug, trace_enabled=trace_enabled
    )
    await server.run()


#############################################################################
#
def main():
    """
    Parse arguments, setup logging, setup tracing, create the user server
    object and start the asyncio main event loop on the user server.
    """
    load_dotenv()
    args = docopt(__doc__, version=VERSION)
    trace_enabled = args["--trace"]
    debug = args["--debug"]
    logdir = args["--logdir"]
    username = args["<username>"]

    # After we setup our logging handlers and formatters set up for asyncio
    # logging so that logging calls do not block the asyncio event loop.
    #
    setup_logging(logdir, debug, username=username)
    setup_asyncio_logging()

    if trace_enabled:
        logger.debug("Tracing enabled")
        asimap.trace.TRACE_ENABLED = True
        asimap.trace.enable_tracing(logdir)
        asimap.trace.trace({"trace_format": "1.0"})

    try:
        asyncio.run(
            create_and_start_user_server(Path.cwd(), debug, trace_enabled)
        )
    except KeyboardInterrupt:
        logger.warning("Keyboard interrupt, exiting")

    server = IMAPUserServer(
        Path.cwd(), debug=debug, trace_enabled=trace_enabled
    )
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        logger.warning("Keyboard interrupt, exiting")


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
