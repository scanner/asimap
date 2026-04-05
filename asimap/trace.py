#!/usr/bin/env python
#
# File: $Id$
#
"""
The support for writing (and eventually reading) trace files.

Defines a method for setting up the trace writer and writing messages
to trace writer if it has been initialized.
"""

import logging
import logging.handlers

# system imports
#
import time
from typing import Any

trace_logger = logging.getLogger("asimap.trace")
logger = logging.getLogger("asimap.trace_logger")

TRACE_ENABLED = False

# Trace message timestamps are seconds.microseconds relative to the first
# logged message for the life time of a user subprocess.
#
TRACE_START_TIME = time.monotonic()
TRACE_LAST_TIME = 0.0

# ########################################################################
# ########################################################################
# #
# class TraceFormatter(logging.Formatter):
#     """
#     We define a subclass of the logging.Formatter to handle logging
#     the timestamps. We want to log the delta since the formatter was
#     instantiated and the time delta since the last message was logged.
#     """

#     ####################################################################
#     #
#     def __init__(self, *args, **kwargs):
#         """ """
#         super(TraceFormatter, self).__init__(*args, **kwargs)
#         self.start = time.time()
#         self.last_time = self.start

#     ####################################################################
#     #
#     def formatTime(self, record, datefmt=None):
#         """
#         We return the string to use for the date entry in the logged message.
#         Keyword Arguments:
#         record  --
#         datefmt -- (default None)
#         """
#         now = time.time()
#         delta = now - self.start
#         delta_trace = now - self.last_time
#         self.last_time = now
#         return "{:13.4f} {:8.4f}".format(delta, delta_trace)


# ####################################################################
# #
# def enable_tracing(logdir, trace_file=None):
#     """
#     Keyword Arguments:
#     logdir -- The directory in to which write the trace files
#     """
#     trace_logger.setLevel(logging.INFO)

#     h: Union[logging.StreamHandler, logging.handlers.RotatingFileHandler]
#     if logdir == "stderr" and not trace_file:
#         # Do not write traces to a file - write them to stderr.
#         #
#         log.debug("Logging trace records to stderr")
#         h = logging.StreamHandler()
#     else:
#         # XXX NOTE: We should make a custom logger that writes a trace
#         # version string at the start of every file.
#         #
#         # Rotate on every 10mb, keep 5 files.
#         #
#         if trace_file:
#             trace_file_basename = trace_file
#         else:
#             p = pwd.getpwuid(os.getuid())
#             trace_file_basename = os.path.join(
#                 logdir, "%s-asimapd.trace" % p.pw_name
#             )

#         log.debug("Logging trace records to '{}'".format(trace_file_basename))

#         h = logging.handlers.RotatingFileHandler(
#             trace_file_basename, maxBytes=20971520, backupCount=5
#         )
#     h.setLevel(logging.INFO)
#     formatter = TraceFormatter("%(asctime)s %(message)s")
#     h.setFormatter(formatter)
#     trace_logger.addHandler(h)


####################################################################
#
def toggle_trace(turn_on: bool | None = None) -> None:
    """Enable, disable, or toggle protocol tracing.

    When tracing is turned on the ``TRACE_START_TIME`` is reset and a
    ``{"trace_format": "1.0"}`` header record is emitted so that trace files
    are self-describing.

    Args:
        turn_on: ``True`` to enable tracing, ``False`` to disable it, or
            ``None`` to toggle the current state.
    """
    global TRACE_ENABLED, TRACE_LAST_TIME, TRACE_START_TIME
    match turn_on:
        case True:
            if TRACE_ENABLED is False:
                TRACE_ENABLED = True
                logger.info("Tracing is enabled")
                TRACE_START_TIME = time.monotonic()
                TRACE_LAST_TIME = 0.0
                trace({"trace_format": "1.0"})
        case False:
            if TRACE_ENABLED is True:
                TRACE_ENABLED = False
                logger.info("Tracing is disabled")
        case None:
            if TRACE_ENABLED is True:
                TRACE_ENABLED = False
                logger.info("Tracing is disabled")
            else:
                TRACE_ENABLED = True
                logger.info("Tracing is enabled")
                TRACE_START_TIME = time.monotonic()
                TRACE_LAST_TIME = 0.0
                trace({"trace_format": "1.0"})


####################################################################
#
def trace(msg: dict[str, Any]) -> None:
    """Emit a trace record if tracing is currently enabled.

    Injects ``trace_time`` (seconds since tracing was enabled) and
    ``trace_delta_time`` (seconds since the previous trace record) into the
    message dict before logging it via the ``asimap.trace`` logger.

    Args:
        msg: Arbitrary dict of key/value pairs to include in the trace record.
            The dict is mutated in place to add timing fields.
    """
    global TRACE_ENABLED, TRACE_LAST_TIME, TRACE_START_TIME
    if TRACE_ENABLED:
        now = time.monotonic() - TRACE_START_TIME
        trace_delta_time = now - TRACE_LAST_TIME
        TRACE_LAST_TIME = now
        msg["trace_time"] = now
        msg["trace_delta_time"] = trace_delta_time
        trace_logger.info(msg)
