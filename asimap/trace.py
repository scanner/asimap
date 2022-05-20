#!/usr/bin/env python
#
# File: $Id$
#
"""
The support for writing (and eventually reading) trace files.

Defines a method for setting up the trace writer and writing messages
to trace writer if it has been initialized.
"""

import json
import logging
import logging.handlers

# system imports
#
import os
import pwd
import time

log = logging.getLogger("%s" % __name__)
trace_logger = logging.getLogger("trace")
trace_enabled = False


########################################################################
########################################################################
#
class TraceFormatter(logging.Formatter):
    """
    We define a subclass of the logging.Formatter to handle logging
    the timestamps. We want to log the delta since the formatter was
    instantiated and the time delta since the last message was logged.
    """

    ####################################################################
    #
    def __init__(self, *args, **kwargs):
        """ """
        super(TraceFormatter, self).__init__(*args, **kwargs)
        self.start = time.time()
        self.last_time = self.start

    ####################################################################
    #
    def formatTime(self, record, datefmt=None):
        """
        We return the string to use for the date entry in the logged message.
        Keyword Arguments:
        record  --
        datefmt -- (default None)
        """
        now = time.time()
        delta = now - self.start
        delta_trace = now - self.last_time
        self.last_time = now
        return "{:13.4f} {:8.4f}".format(delta, delta_trace)


####################################################################
#
def enable_tracing(logdir, trace_file=None):
    """
    Keyword Arguments:
    logdir -- The directory in to which write the trace files
    """
    trace_logger.setLevel(logging.INFO)

    if logdir == "stderr" and not trace_file:
        # Do not write traces to a file - write them to stderr.
        #
        log.debug("Logging trace records to stderr")
        h = logging.StreamHandler()
    else:
        # XXX NOTE: We should make a custom logger that writes a trace
        # version string at the start of every file.
        #
        # Rotate on every 10mb, keep 5 files.
        #
        if trace_file:
            trace_file_basename = trace_file
        else:
            p = pwd.getpwuid(os.getuid())
            trace_file_basename = os.path.join(
                logdir, "%s-asimapd.trace" % p.pw_name
            )

        log.debug("Logging trace records to '{}'".format(trace_file_basename))

        h = logging.handlers.RotatingFileHandler(
            trace_file_basename, maxBytes=20971520, backupCount=5
        )
    h.setLevel(logging.INFO)
    formatter = TraceFormatter("%(asctime)s %(message)s")
    h.setFormatter(formatter)
    trace_logger.addHandler(h)


####################################################################
#
def trace(msg):
    """
    Keyword Arguments:
    msg --
    """
    if trace_enabled:
        trace_logger.info(json.dumps(msg))
