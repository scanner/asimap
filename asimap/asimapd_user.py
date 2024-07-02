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
  asimapd_user.py [--trace] [--trace-dir=<td>] [--log-config=<lc>]
                  [--debug] <username>

Options:
  --version
  -h, --help    Show this text and exit

  --debug            Will set the default logging level to `DEBUG` thus
                     enabling all of the debuggign logging.

  --log-config=<lc>  The log config file. This file may be either a JSON file
                     that follows the python logging configuration dictionary
                     schema or a file that coforms to the python logging
                     configuration file format. If no file is specified it will
                     check in /etc, /usr/local/etc, /opt/local/etc for a file
                     named `asimapd_log.cfg` or `asimapd_log.json`.  If no
                     valid file can be found or loaded it will defaut to
                     logging to stdout.

  --trace            For debugging and generating protocol test data `trace`
                     can be enabled. When enabled messages will appear on the
                     `asimap.trace` logger where the `message` part of the log
                     message is a JSON dump of the message being sent or
                     received. This only happens for post-authentication IMAP
                     messages (so nothing about logging in is recorded.)
                     However the logs are copious! The default logger will dump
                     trace logs where `--trace-dir` specifies.

  --trace-dir=<td>   The directory trace log files are written to. Unless
                     overriden by specifying a custom log config! Since traces
                     use the logging system if you supply a custom log config
                     and turn tracing on that will override this. By default
                     trace logs will be written to `/opt/asimap/traces/`. By
                     default the traces will be written using a
                     RotatingFileHandler with a size of 20mb, and backup count
                     of 5 using the pythonjsonlogger.jsonlogger.JsonFormatter.

XXX We communicate with the server via localhost TCP sockets. We REALLY should
    set up some sort of authentication key that the server must use when
    connecting to us. Perhaps we will use stdin for that in the
    future. Otherwise this is a bit of a nasty security hole.
"""
# system imports
#
import asyncio
import logging
import sys
from pathlib import Path

# 3rd party imports
#
from docopt import docopt
from dotenv import load_dotenv

# Application imports
#
import asimap.trace
from asimap import __version__ as VERSION
from asimap.user_server import IMAPUserServer
from asimap.utils import setup_asyncio_logging, setup_logging

logger = logging.getLogger("asimap.asimapd_user")


#############################################################################
#
async def create_and_start_user_server(maildir: Path, debug: bool, trace: bool):
    server = await IMAPUserServer.new(maildir, debug=debug, trace_enabled=trace)
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
    trace = args["--trace"]
    trace_dir = args["--trace-dir"]
    debug = args["--debug"]
    log_config = args["--log-config"]
    username = args["<username>"]

    # After we setup our logging handlers and formatters set up for asyncio
    # logging so that logging calls do not block the asyncio event loop.
    #
    setup_logging(log_config, debug, username=username, trace_dir=trace_dir)
    setup_asyncio_logging()
    maildir = Path.cwd()
    logger.info(
        "Starting new user server for '%s', maildir: '%s'", username, maildir
    )

    if trace:
        logger.debug("Tracing enabled")
        asimap.trace.TRACE_ENABLED = True
        asimap.trace.trace({"trace_format": "1.0"})

    try:
        asyncio.run(create_and_start_user_server(maildir, debug, trace))
    except KeyboardInterrupt:
        logger.warning("Keyboard interrupt, exiting, user: %s", username)
    except Exception as e:
        logger.exception(
            "For user %s Failed with uncaught exception %s", username, str(e)
        )
        sys.exit(1)


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
