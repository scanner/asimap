#!/usr/bin/env python
#
# File: $Id$
#
"""
The support for writing (and eventually reading) trace files.

Defines a method for setting up the trace writer and writing messages
to trace writer if it has been initialized.
"""

# system imports
#
import os
import pwd
import time
import logging
import logging.handlers
import json

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
        """
        """
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
        return "{:12.3f} {:7.3f}".format(delta, delta_trace)


####################################################################
#
def enable_tracing(logdir):
    """
    Keyword Arguments:
    logdir -- The directory in to which write the trace files
    """
    trace_logger.setLevel(logging.INFO)

    if logdir == "stderr":
        # Do not write traces to a file - write them to stderr.
        #
        h = logging.StreamHandler()
    else:
        # XXX NOTE: We should make a custom logger that writes a trace
        # version string at the start of every file.
        #
        # Rotate on every 10mb, keep 5 files.
        #
        p = pwd.getpwuid(os.getuid())
        trace_file_basename = os.path.join(logdir,
                                           "%s-asimapd.trace" % p.pw_name)
        h = logging.handlers.RotatingFileHandler(trace_file_basename,
                                                 maxBytes=20971520,
                                                 backupCount=5)
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
