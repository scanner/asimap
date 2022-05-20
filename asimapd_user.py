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

import asyncore
import logging
import logging.handlers
import optparse

# system imports
#
import os
import pwd
import sys
import time

# Application imports
#
import asimap
import asimap.trace as trace
import asimap.user_server


############################################################################
#
def setup_option_parser():
    """
    This function uses the python OptionParser module to define an option
    parser for parsing the command line options for this script. This does not
    actually parse the command line options. It returns the parser object that
    can be used for parsing them.
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
        dest="trace_enabled",
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
        "--standalone_mode",
        action="store_true",
        dest="standalone_mode",
        help="Indicates that the user server object is to be "
        "run without actually establshing an "
        "asyncore.dispatcher.This is used as part of the "
        "debugging and utilitiesn process so that we can run a "
        "user_server without actually having it listen to "
        "network connections. Useful for running subsystems and "
        "feeding it commands in a test harness.",
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

    server = asimap.user_server.IMAPUserServer(options, os.getcwd())

    # Print on stdout the port we are listening on so that the asimapd server
    # knows how to talk to us.
    #
    ip, port = server.address

    # We need to make sure stdout is unbuffered so that whatever we write here
    # will be immediately be sent to our calling process instead of waiting
    # for however many bytes stdout wants before it flushes the the output.
    #
    sys.stdout = os.fdopen(sys.stdout.fileno(), "w", 0)
    sys.stdout.write("%d\n" % port)
    sys.stdout.flush()
    sys.stdout.close()

    # Before we start our main loop find all folders and potentially update
    # their \Marked and \Unmarked attributes (and at least populating our
    # db with all of the folders that we can find.)
    #
    server.find_all_folders()
    server.check_all_folders()
    last_full_check = time.time()

    # And now loop forever.. breaking out of the loop every now and then to
    # see if we have had no active clients for awhile (and if we do not then
    # we exit.)
    #
    log.info("Starting main loop.")
    last_active_check = 0
    now = time.time()

    while True:

        # If any folders have queued commands then set the timeout waiting for
        # data from clients to 0 so we can process the command queues.
        #
        timeout = 30.0
        if server.has_queued_commands():
            timeout = 0

        asyncore.loop(count=1, timeout=timeout)

        # At the end of each loop if we have had no clients for <n> minutes
        # then we should exit to save resources because no one is using us.
        #
        if server.expiry is not None and server.expiry < now:
            break

        # If any mailboxes have queued commands in process then run those.
        #
        server.process_queued_commands()

        # Now handle any other house cleaning tasks we need, all of which are
        # dependent on running after certain time delays.
        #
        now = time.time()

        # Check all active folders that have clients in IDLE and do a
        # resync on them, every 30 seconds.
        #
        # XXX Since we now store the last time we checked a folder maybe we
        #     should skip checking active folders that have actually been
        #     checked in the last 30 seconds? Not sure we will get any real
        #     savings from this.
        #
        if now - last_active_check > 30:
            server.check_all_active_folders()
            server.expire_inactive_folders()
            last_active_check = time.time()

        # Do a run through all of our folders and see if any of
        # them have changed. But we only do this once every 5 minutes.
        #
        if now - last_full_check > 300:
            server.check_all_folders()
            last_full_check = time.time()

    # Exiting!
    #
    log.info("Idle with no clients for at least 15 minutes. Exiting.")
    asyncore.close_all()

    # Close our handle to the sqlite3 database and our MH mailbox.
    #
    server.db.commit()
    server.db.close()
    server.mailbox.close()

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
