#!/usr/bin/env python
#
# File: $Id$
#
"""
The AS IMAP Daemon. This is intended to be run as root. It provides an
IMAP service that is typically backed by MH mail folders.

Usage:
  asimapd.py [--port=<p>] [--address=<i>] [--cert=<cert>] [--key=<key>]
             [--trace=<trace>] [--test] [--debug] [--pidfile=<pidfile>]
             [--foreground] [--logdir=<d>]

Options:
  --version
  -h, --help         Show this text and exit
  --port=<p>         Port to listen on. If `--cert` is specified it will
                     default to 993 if not stated specifically. Otherwise a
                     random non priveleged port will be picked.
  --address=<i>      The address to listen on. [default: '0.0.0.0']
  --trace=<trace>    The per user subprocesses will each open up a trace file
                     and write to it all messages sent and received. One line
                     per message. The message will be a timestamp, a relative
                     timestamp, the direction of the message (sent/received),
                     and the message itself.The tracefiles will be written to
                     the log dir and will be named <username>-asimap.trace
  --pidfile=<f>      The PID of the main process will be written to this file.
  --foreground       Do NOT run in daemon mode. Automatically selected if
                     `--test` is set.
  --logdir=<d>       Path to the directory where log files are stored. Since
                     this is a multiprocess server which each sub-process
                     running as a different user we have a log file for the
                     main server and then a separate log file for each
                     sub-process. One sub-process per account. The main logfile
                     will be called 'asimapd.log'. Each sub-process's logfile
                     will be called '<local user>-asimapd.log'. If this is set
                     to 'stderr' then we will not log to a file but emit all
                     messages for all processes on stderr. [default: stderr]
  --test             Run the server using the test mode environment. The server
                     will run as normal except it will use the 'test_auth'
                     authentication system and the MH mailbox it use will be
                     the one in '/var/tmp/testmaildir'. It will NOT create this
                     MH mailbox. You must have set it up previously. This mode
                     is obviously of limited value and exists primarily to run
                     a test server that does not attempt to muck with real MH
                     mailboxes or need to run as root.
  --debug            Debugging output messages enabled

"""
import asyncio
import logging

# system imports
#
import os
import random
import ssl
import sys
from pathlib import Path

# 3rd party imports
#
from docopt import docopt

from asimap.server import AsyncIMAPServer
from asimap.user_server import set_user_server_program

# Application imports
#
from asimap.utils import daemonize

VERSION = "2.0"

logger = logging.getLogger(__name__)


####################################################################
#
def setup_logging(logdir: str, debug: bool):
    """
    Set up the logger. We log either to files in 'logdir'
    or to stderr.

    NOTE: It does not make sense to log to stderr if we are running in
          daemon mode.. maybe we should exit with a warning before we
          try to enter daemon mode if logdir == 'stderr'

    Keyword Arguments:
    logdir: str --
    debug: bool --
    """
    if debug:
        level = logging.DEBUG
    else:
        level = logging.INFO

    # We define our logging config on the root loggger.
    #
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if logdir == "stderr":
        # Do not log to a file, log to stderr.
        #
        h = logging.StreamHandler()
    else:
        # Rotate on every 10mb, keep 5 files.
        #
        log_file_basename = os.path.join(logdir, "asimapd.log")
        h = logging.handlers.RotatingFileHandler(
            log_file_basename, maxBytes=10485760, backupCount=5
        )
    h.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(created)s %(process)d "
        "%(levelname)s %(name)s "
        "%(message)s"
    )
    h.setFormatter(formatter)
    root_logger.addHandler(h)


#############################################################################
#
def main():
    """
    Our main entry point. Parse the options, set up logging, go in to
    daemon mode if necessary, setup the asimap library and start
    accepting connections.
    """
    args = docopt(__doc__, version=VERSION)
    test = args["--test"]
    debug = args["--debug"]
    foreground = args["--foreground"]
    address = args["--address"]
    port = args["--port"]
    cert = args["--cert"]
    key = args["--key"]
    pidfile = args["--pidfile"]
    logdir = args["--logdir"]

    # Test mode sets up a bunch of defaults:
    #
    # XXX I imagine test_mode will go away when we have the tracefile
    #     runner.
    #
    # - disables daemonize
    # - sets logdir to be 'stderr'
    # - sets interface to be '127.0.0.1'
    # - sets ssl to False
    # - sets port to be 143
    # - sets pid file to None
    # - sets debug = True
    #
    if test:
        dirname = Path(__file__).parent
        sys.path.insert(0, dirname)

        print("asimap - enabling 'test_mode'.")

        test_mode_dir = None
        for path in ("test_mode", "test/test_mode", "asimap/test/test_mode"):
            tmd = Path.cwd() / path
            print(f"\tchecking for test_modir dir '{tmd}'")
            if tmd.is_dir():
                test_mode_dir = tmd
                break

        if test_mode_dir is None:
            raise RuntimeError("Unable to find suitable test mode dir")

        foreground = True
        address = "127.0.0.1"
        port = random.randint(1234, 32000)
        cert = None
        key = None
        logdir = "stderr"
        pidfile = None
        debug = True
        print(f"\tforeground: {foreground}")
        print(f"\taddress: {address}:{port}")
        print(f"\tlogdir: {logdir}")
        print(f"\tdebug: {debug}")

    # Enter daemon mode early on if it is selected. test_mode disabled
    # daemon mode.
    #
    if not foreground:
        print("asimap - Entering daemon mode")
        daemonize()

    setup_logging(logdir, debug)
    logger.info("Starting")

    try:
        if pidfile:
            with open(pidfile, "w+") as f:
                f.write(f"{os.getpid()}\n")
            logger.info(f"Wrote pid {os.getpid()} in to pid file '{pidfile}'")
    except Exception as exc:
        logger.exception(f"Unable to write PID file '{pidfile}': {exc}")

    # If you supply just a certificate then it is both the key and
    # certificate in one file (key first in the file).
    #
    # This forces the port to 993 if a ssl cert is specified
    #
    ssl_context = None
    if cert:
        ssl_context = ssl.create_default_context(ssl.CLIENT_AUTH)
        ssl_context.load_cert_chain(cert, keyfile=key)
        if port is None:
            port = 993

    # If the port is not yet set, pick a random one. Log our bind address.
    #
    if port is None:
        port = random.randint(1234, 32000)
    logger.info(f"Binding address: {address}:{port}")

    # Using the location of the server program determine the location of
    # the user_server program (if it was not set via a command line option.)
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

    server = AsyncIMAPServer(address, port, ssl_context, debug=debug, test=test)
    asyncio.run(server.get_server())
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
