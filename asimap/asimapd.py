#!/usr/bin/env python
#
# File: $Id$
#
"""
The AS IMAP Daemon. This is intended to be run as root. It provides an
IMAP service that is typically backed by MH mail folders.

NOTE: For all command line options that can also be specified via an env. var:
      the command line option will override the env. var if set.

Usage:
  asimapd [--address=<i>] [--port=<p>] [--cert=<cert>] [--key=<key>]
          [--trace=<trace>] [--debug] [--log-config=<lc>]
          [--pwfile=<pwfile>]

Options:
  --version
  -h, --help         Show this text and exit
  --address=<i>      The address to listen on. Defaults to '0.0.0.0'.
                     The env. var is `ADDRESS`.
  --port=<p>         Port to listen on. Defaults to: 993.
                     The env. var is `PORT`
  --cert=<cert>      SSL Certificate file. If not set defaults to
                     `/opt/asimap/ssl/cert.pem`. The env var is SSL_CERT
  --key=<key>        SSL Certificate key file. If not set defaults to
                     `/opt/asimap/ssl/key.pem`. The env var is SSL_KEY
  --trace=<trace>    The per user subprocesses will each open up a trace file
                     and write to it all messages sent and received. One line
                     per message. The message will be a timestamp, a relative
                     timestamp, the direction of the message (sent/received),
                     and the message itself.The tracefiles will be written to
                     the log dir and will be named <username>-asimap.trace

  --debug            Will set the default logging level to `DEBUG` thus
                     enabling all of the debug logging. The env var is `DEBUG`

  --log-config=<lc>  The log config file. This file may be either a JSON file
                     that follows the python logging configuration dictionary
                     schema or a file that coforms to the python logging
                     configuration file format. If no file is specified it will
                     check in /opt/asimap, /etc, /usr/local/etc, /opt/local/etc
                     for a file named `asimapd_log.cfg` or `asimapd_log.json`.
                     If no valid file can be found or loaded it will defaut to
                     logging to stdout. The env. var is `LOG_CONFIG`

  --pwfile=<pwfile>  The file that contains the users and their hashed passwords
                     The env. var is `PWFILE`. Defaults to `/opt/asimap/pwfile`
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
from dotenv import dotenv_values

# Application imports
#
from asimap import __version__ as VERSION
from asimap import auth
from asimap.server import IMAPServer
from asimap.user_server import set_user_server_program
from asimap.utils import setup_asyncio_logging, setup_logging

logger = logging.getLogger("asimap.asimapd")


#############################################################################
#
def main():
    """
    Our main entry point. Parse the options, set up logging, go in to
    daemon mode if necessary, setup the asimap library and start
    accepting connections.
    """
    args = docopt(__doc__, version=VERSION)
    address = args["--address"]
    port = int(args["--port"])
    ssl_cert_file = args["--cert"]
    ssl_key_file = args["--key"]
    trace = args["--trace"]
    debug = args["--debug"]
    log_config = args["--log-config"]
    pwfile = args["--pwfile"]

    config = dotenv_values()

    # If docopt is not, see if the option is set in the config. If it not set
    # there either, then set it to the default value.
    #
    if address is None:
        address = config["ADDRESS"] if "ADDRESS" in config else "0.0.0.0"
    if port is None:
        port = config["PORT"] if "PORT" in config else 993
    if ssl_cert_file is None:
        ssl_cert_file = (
            config["SSL_CERT"]
            if "SSL_CERT" in config
            else "/opt/asimap/ssl/cert.pem"
        )
    if ssl_key_file is None:
        ssl_key_file = (
            config["SSL_KEY"]
            if "SSL_KEY" in config
            else "/opt/asimap/ssl/key.pem"
        )
    if debug is None:
        debug = config["DEBUG"] if "DEBUG" in config else False
    if log_config is None:
        log_config = config["LOG_CONFIG"] if "LOG_CONFIG" in config else None
    if pwfile is None:
        pwfile = (
            config["PWFILE"] if "PWFILE" in config else "/opt/asimap/pwfile"
        )

    # If a password file was specified overwrote the default location for the
    # password file in the asimap.auth module.
    #
    if pwfile:
        setattr(auth, "PW_FILE_LOCATION", pwfile)

    # After we setup our logging handlers and formatters set up for asyncio
    # logging so that logging calls do not block the asyncio event loop.
    #
    setup_logging(log_config, debug)
    setup_asyncio_logging()
    logger.info("Starting")

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.check_hostname = False
    ssl_context.load_cert_chain(ssl_cert_file, ssl_key_file)

    logger.info("Binding address: %s:%d", address, port)

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
            "User server program does not exist or is not a file: '%s'",
            user_server_program,
        )
        exit(-1)

    # Set this as a variable in the asimap.user_server module.
    #
    logger.debug("user server program is: '%s'", user_server_program)
    set_user_server_program(user_server_program)

    server = IMAPServer(
        address,
        port,
        ssl_context,
        trace=trace,
        log_config=log_config,
        debug=debug,
    )
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        logger.warning("Keyboard interrupt, exiting")
    finally:
        logging.shutdown()
        print("Finally exited asyncio main loop")


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
