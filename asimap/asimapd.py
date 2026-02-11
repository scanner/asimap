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
          [--trace] [--trace-dir=<td>] [--debug] [--log-config=<lc>]
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
import os
import ssl
from pathlib import Path

# 3rd party imports
#
from docopt import docopt
from dotenv import load_dotenv

# Application imports
#
import asimap.trace
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
    port = args["--port"]
    port = int(port) if port else None
    ssl_cert_file = args["--cert"]
    ssl_key_file = args["--key"]
    trace = args["--trace"]
    trace_dir = args["--trace-dir"]
    debug = args["--debug"]
    log_config = args["--log-config"]
    pwfile = args["--pwfile"]

    load_dotenv()

    # If docopt is not, see if the option is set in the os.environ. If it not
    # set there either, then set it to the default value.
    #
    if address is None:
        address = (
            os.environ["ADDRESS"] if "ADDRESS" in os.environ else "0.0.0.0"
        )
    if port is None:
        port = os.environ["PORT"] if "PORT" in os.environ else 993
    if ssl_cert_file is None:
        ssl_cert_file = (
            os.environ["SSL_CERT"]
            if "SSL_CERT" in os.environ
            else "/opt/asimap/ssl/ssl_crt.pem"
        )
    ssl_cert_file = Path(ssl_cert_file)
    if ssl_key_file is None:
        ssl_key_file = (
            os.environ["SSL_KEY"]
            if "SSL_KEY" in os.environ
            else "/opt/asimap/ssl/ssl_key.pem"
        )
    ssl_key_file = Path(ssl_key_file)
    # If debug is not enabled via the command line, see if it is enabled via
    # the env var.
    #
    if not debug:
        debug = bool(os.environ["DEBUG"]) if "DEBUG" in os.environ else False
    if log_config is None:
        log_config = (
            os.environ["LOG_CONFIG"] if "LOG_CONFIG" in os.environ else None
        )
    if trace is None:
        trace = os.environ["TRACE"] if "TRACE" in os.environ else False
    if trace_dir is None:
        trace_dir = (
            os.environ["TRACE_DIR"]
            if "TRACE_DIR" in os.environ
            else Path("/opt/asimap/traces")
        )
    if pwfile is None:
        pwfile = (
            os.environ["PWFILE"]
            if "PWFILE" in os.environ
            else "/opt/asimap/pwfile"
        )

    # If a password file was specified overwrote the default location for the
    # password file in the asimap.auth module.
    #
    if pwfile:
        auth.PW_FILE_LOCATION = pwfile

    if not ssl_cert_file.exists() or not ssl_key_file.exists():
        raise FileNotFoundError(
            f"Both '{ssl_cert_file}' and '{ssl_key_file}' must exist."
        )

    # After we setup our logging handlers and formatters set up for asyncio
    # logging so that logging calls do not block the asyncio event loop.
    #
    setup_logging(log_config, debug, trace_dir=trace_dir)
    setup_asyncio_logging()
    logger.info("ASIMAPD Starting, version: %s", VERSION)

    if trace:
        asimap.trace.toggle_trace(turn_on=True)

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
        trace_dir=trace_dir,
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
