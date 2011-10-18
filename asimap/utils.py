#!/usr/bin/env python
#
# File: $Id$
#
"""
This module contains utility functions that do not properly belong to any
class or module. This started with the utilities to pass an fd betwene
processes. If we build a decently sized set of messaging routines many of these
may move over in to a module dedicated for that.
"""

# system imports
#
import datetime
import email.utils
import calendar
import pytz
import logging

# asimap imports
#
from exceptions import Bad

LOG = logging.getLogger("%s" % (__name__,))

############################################################################
#
def parsedate(date_time_str):
    """
    All date time data is stored as a datetime.datetime object in UTC.
    This routine uses common routines provided by python to parse a rfc822
    formatted date time in to a datetime.datetime object.

    It is pretty simple, but makes the code a lot shorter and easier to read.
    """
    return datetime.datetime.fromtimestamp(\
            email.utils.mktime_tz(email.utils.parsedate_tz(date_time_str)),
            pytz.UTC)

############################################################################
#
def formatdate(datetime, localtime = False, usegmt = False):
    """
    This is the reverse. It will take a datetime object and format
    and do the deconversions necessary to pass it to email.utils.formatdate()
    and thus return a string properly formatted as an RFC822 date.
    """
    return email.utils.formatdate(calendar.timegm(datetime.utctimetuple()),
                                  localtime = localtime, usegmt = usegmt)

####################################################################
#
def sequence_set_to_list(seq_set, seq_max, uid_cmd = False):
    """
    Convert a squence set in to a list of numbers.

    We collapse any overlaps and return the list sorted.

    NOTE: Using '*' in a mailbox that has no messages raises the Bad
          exception. If any sequence number is greater than the size
          of the mailbox actually.

    Arguments:
    - `seq_set`: The sequence set we want to convert to a list of numbers.
    - `seq_max`: The largest possible number in the sequence. We
      replace '*' with this value.
    - `uid_cmd`: This is a UID command sequence and it can include
      numbers larger than seq_max.
    """
    result = []
    for elt in seq_set:
        # Any occurences of '*' we can just swap in the sequence max value.
        #
        if str(elt) == "*":
            if seq_max == 0 and not uid_cmd:
                raise Bad("Message index '*' is greater than the size of "
                          "the mailbox")
            result.append(seq_max)
        elif isinstance(elt, int):
            if elt > seq_max and not uid_cmd:
                raise Bad("Message index '%d' is greater than the size of "
                          "the mailbox" % elt)
            result.append(elt)
        elif isinstance(elt, tuple):
            start, end = elt
            if str(start) == "*":
                start = seq_max
            if str(end) == "*":
                end = seq_max
            if (start == 0 or end == 0 or start > seq_max or end > seq_max) \
                    and not uid_cmd:
                raise Bad("Message sequence '%s' is greater than the size of "
                          "the mailbox, start: %d, end: %d, seq_max: %d" % \
                              (str(elt), start, end, seq_max))
            if start > end:
                result.extend(range(end, start + 1))
            else:
                result.extend(range(start, end + 1))
    return sorted(set(result))
