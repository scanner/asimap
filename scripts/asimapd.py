#!/usr/bin/env python
#
# File: $Id$
#
"""
The AS IMAP Daemon. This is intended to be run as root. It provides an
IMAP service that is typically backed by MH mail folders.

Usage:
  asimapd.py [--address=<i>] [--port=<p>] [--cert=<cert>] [--key=<key>]
             [--trace=<trace>] [--debug] [--logdir=<d>] [--pwfile=<pwfile>]

Options:
  --version
  -h, --help         Show this text and exit
  --address=<i>      The address to listen on. [default: '0.0.0.0']
  --port=<p>         Port to listen on. [default: 993]
  --cert=<cert>      SSL Certificate file
  --key=<key>        SSL Certificate key file
  --trace=<trace>    The per user subprocesses will each open up a trace file
                     and write to it all messages sent and received. One line
                     per message. The message will be a timestamp, a relative
                     timestamp, the direction of the message (sent/received),
                     and the message itself.The tracefiles will be written to
                     the log dir and will be named <username>-asimap.trace
  --debug            Debugging output messages enabled
  --logdir=<d>       Path to the directory where log files are stored. Since
                     this is a multiprocess server which each sub-process
                     running as a different user we have a log file for the
                     main server and then a separate log file for each
                     sub-process. One sub-process per user. The main logfile
                     will be called 'asimapd.log'. Each sub-process's logfile
                     will be called '<user>-asimapd.log'. If this is set
                     to 'stderr' then we will not log to a file but emit all
                     messages for all processes on stderr. [default: stderr]
  --pwfile=<pwfile>  The file that contains the users and their hashed passwords

"""
# system imports
#
import asyncio
import logging
import ssl
from pathlib import Path

# 3rd party imports
#
from docopt import docopt
from dotenv import load_dotenv
from rich.traceback import install as rich_install

# Application imports
#
from asimap import __version__ as VERSION
from asimap import auth
from asimap.server import IMAPServer
from asimap.user_server import set_user_server_program
from asimap.utils import setup_asyncio_logging, setup_logging

rich_install(show_locals=True)

logger = logging.getLogger("asimapd")


#############################################################################
#
def main():
    """
    Our main entry point. Parse the options, set up logging, go in to
    daemon mode if necessary, setup the asimap library and start
    accepting connections.
    """
    load_dotenv()
    args = docopt(__doc__, version=VERSION)
    address = args["--address"]
    port = int(args["--port"])
    ssl_cert_file = args["--cert"]
    ssl_key_file = args["--key"]
    trace = args["--trace"]
    debug = args["--debug"]
    logdir = args["--logdir"]
    pwfile = args["--pwfile"]

    # If a password file was specified overwrote the default location for the
    # password file in the asimap.auth module.
    #
    if pwfile:
        setattr(auth, "PW_FILE_LOCATION", pwfile)

    # After we setup our logging handlers and formatters set up for asyncio
    # logging so that logging calls do not block the asyncio event loop.
    #
    setup_logging(logdir, debug)
    setup_asyncio_logging()
    logger.info("Starting")

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.check_hostname = False
    ssl_context.load_cert_chain(ssl_cert_file, ssl_key_file)

    logger.info(f"Binding address: {address}:{port}")

    # ASIMap is run internally as a main server that accepts connections and
    # authenticates them, and once it authenticates them it passes control to a
    # subprocess. One subprocess per authenticated user.
    #
    # We use the program `asimapd_user.py` as the entry point for this user's
    # subprocess. One subprocess per user. Multiple connections from the same
    # user go to this one subprocess.
    #
    # Using the location of the server program determine the location of
    # the user server program.
    #
    user_server_program = Path(__file__).parent / "asimapd_user.py"
    user_server_program.resolve(strict=True)

    # Make sure the user server program exists and is executable before we go
    # any further..
    #
    if not user_server_program.exists() or not user_server_program.is_file():
        logger.error(
            "User server program does not exist or is not a file: "
            f"'{user_server_program}'"
        )
        exit(-1)

    # Set this as a variable in the asimap.user_server module.
    #
    logger.debug(f"user server program is: '{user_server_program}'")
    set_user_server_program(user_server_program)

    server = IMAPServer(address, port, ssl_context, trace=trace, debug=debug)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        logger.warning("Keyboard interrupt, exiting")
    finally:
        print("Finally exited asyncio main loop")


############################################################################
############################################################################
#
# Here is where it all starts
#
if __name__ == "__main__":
    main()
    print("Shutting down logging")
    logging.shutdown()
    print("After logging shutdown")
#
#
############################################################################
############################################################################
