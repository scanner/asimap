#!/usr/bin/env python
#
# File: $Id$
#
"""
The AS IMAP Daemon. This is intended to be run as root. It provides an
IMAP service that is typically backed by MH mail folders.
"""

# system imports
#
import sys
import os.path
import optparse
import logging
import logging.handlers
import socket
import asyncore
import traceback
import select

# Application imports
#
import asimap
import asimap.user_server
import asimap.utils

############################################################################
#
def setup_option_parser():
    """
    This function uses the python OptionParser module to define an option
    parser for parsing the command line options for this script. This does not
    actually parse the command line options. It returns the parser object that
    can be used for parsing them.
    """
    parser = optparse.OptionParser(usage = "%prog [options]",
                                   version = asimap.__version__)

    parser.set_defaults(port = None, ssl_port = 993,
                        interface = "0.0.0.0", debug = False,
                        ssl = True, ssl_certificate = None,
                        daemonize = True,
                        test_mode = False,
                        errorstack_key = None,
                        pidfile = "/var/run/asimapd.pid",
                        logdir = "/var/log/asimapd")

    parser.add_option("--port", action="store", type="int", dest="port",
                      help = "What port to listen on for NON-SSL connections. "
                      "Note that is --port is NOT specified we will NOT "
                      "on it. This is how to disable non-encrypted "
                      "connections for this server.")
    parser.add_option("--test_mode",  action="store_true", dest="test_mode",
                      help="Run the server using the test mode environment. "
                      "The server will run as normal except it will use the "
                      "'test_auth' authentication system and the MH mailbox it "
                      "use will be the one in '/var/tmp/testmaildir'. It will "
                      "NOT create this MH mailbox. You must have set it up "
                      "previously. This mode is obviously of limited value "
                      "and exists primarily to run a test server that does not "
                      "attempt to muck with real MH mailboxes or need to run "
                      "as root.")
    parser.add_option("--ssl_port", action="store", type="int", dest="port",
                      help = "What port to listen on for SSL connections")
    parser.add_option("--interface", action="store", type="string",
                      dest="interface", help = "The IP address to bind to.")
    parser.add_option("--pidfile", action="store", type="string",
                      dest="pidfile", help = "The file to store the server's "
                      "pid in")
    parser.add_option("--debug", action="store_true", dest="debug",
                      help="Emit debugging statements.")
    parser.add_option("--foreground", action="store_false", dest="daemonize",
                      help="Do NOT run in daemon mode.")
    parser.add_option("--no_ssl", action="store_false", dest="ssl",
                      help="Turn off SSL for the incoming IMAP4 "
                      "connections.")
    parser.add_option("--ssl_certificate", action="store", type="string",
                      dest="ssl_certificate", help="Path to your SSL "
                      "certificate. This must be a file that contains a "
                      "private key and certificate chain in PEM format as "
                      "needed by the python 'ssl' module. Consult the Python "
                      "SSL document at "
                      "http://docs.python.org/library/ssl.html#ssl-certificates"
                      " for more information.")
    parser.add_option("--logdir", action="store", type="string",
                      dest="logdir", help="Path to the directory where log "
                      "files are stored. Since this is a multiprocess server "
                      "which each sub-process running as a different user "
                      "we have a log file for the main server and then "
                      "a separate log file for each sub-process. "
                      "One sub-process per account. The main logfile "
                      "will be called 'asimapd.log'. Each sub-process's "
                      "logfile will be called '<local user>-"
                      "asimapd.log'. If this is set to 'stderr' then we will "
                      "not log to a file but emit all messages for all "
                      "processes on stderr. NOTE: If you select --daemonize, "
                      "setting the logdir to 'stderr' makes no sense! "
                      "When we daemonize stderr is redirected to /dev/null.")
    parser.add_option("--errorstack_key", action="store", type="string",
                      dest="errorstack_key", help="If you are using "
                      "errorstack.com to track and analyze error and above "
                      "failures then supply the stack key here.")
    return parser

#############################################################################
#
def main():
    """
    Our main entry point. Parse the options, set up logging, go in to
    daemon mode if necessary, setup the asimap library and start
    accepting connections.
    """
    parser = setup_option_parser()
    (options, args) = parser.parse_args()

    # Enter daemon mode early on if it is selected.
    #
    if options.daemonize:
        print "asimap - Entering daemon mode"
        asimap.utils.daemonize()

    # Set up the logger. We log either to files in 'options.logdir' or to
    # stderr. NOTE: It does not make sense to log to stderr if we are running
    # in daemon mode.. maybe we should exit with a warning before we try to
    # enter daemon mode if logdir == 'stderr'
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
        log_file_basename = os.path.join(options.logdir, "asimapd.log")
        h = logging.handlers.RotatingFileHandler(log_file_basename,
                                                 maxBytes = 10485760,
                                                 backupCount = 5)
    h.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(created)s %(process)d "
                                  "%(levelname)s %(name)s %(message)s")
    h.setFormatter(formatter)
    log.addHandler(h)
    log.info("Starting")

    # XXXX Due to complications with how we detect/load the auth system we have
    #      to import asimap.server _after_ we have defined our log system.
    #
    import asimap.server

    try:
        with open(options.pidfile, "w+") as f:
            f.write("%d\n" % os.getpid())
        log.info("Wrote pid %d in to pid file '%s'" % (os.getpid(),
                                                       options.pidfile))
    except Exception, e:
        log.error("Unable to write PID file '%s': %s" % (options.pidfile,
                                                         str(e)))


    # If we are using SSL you must supply a certificate.
    #
    if options.ssl and options.ssl_certificate is None:
        log.error("If SSL is enabled you need to provide a SSL certificate "
                  "via the --ssl_certificate option")
        exit(-1)

    # Using the location of the server program determine the location of
    # the user_server program (if it was not set via a command line option.)
    #
    user_server_program = os.path.abspath(os.path.join(os.path.dirname(sys.argv[0]),"asimapd_user.py"))

    # Make sure the user server program exists and is executable before we go
    # any further..
    #
    if not os.path.exists(user_server_program) or \
            not os.path.isfile(user_server_program):
        log.error("User server program does not exist or is not a file: '%s'" \
                      % user_server_program)
        exit(-1)

    # Set this as a variable in the asimap.user_server module.
    #
    log.debug("user server program is: '%s'" % user_server_program)
    asimap.user_server.set_user_server_program(user_server_program)

    try:
        if options.port:
            non_ssl_server = asimap.server.IMAPServer(options.interface,
                                                      options.port,
                                                      options)
        if options.ssl:
            ssl_server =  asimap.server.IMAPServer(options.interface,
                                                   options.ssl_port,
                                                   options,
                                                   options.ssl_certificate)
    except socket.error, e:
        log.error("Unable to create server object on %s:%d: socket " \
                  "error: %s" % (options.interface, options.port, e))
        return

    # XXX We should do the loop inside of 'while True' and at the end of each
    #     loop run through all of the subprocess handles and call 'is_alive()'
    #     on them to reap them so that when they go away due to idleness we do
    #     not leave zombie processes waiting around for their parent to reap
    #     them.
    #
    #     We have to do this because subprocesses will stay around after they
    #     have been started up until they have been idle for a certain amount
    #     of time with no active clients.
    #
    asyncore.loop()
    # while True:
    #     try:
    #         asyncore.loop()
    #     except select.error, e:
    #         tb = traceback.format_exc()
    #         log.error("asyncore.loop() returned select.error: %s\n%s" % \
    #                       (str(e), tb))
    #     else:
    #         break

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
