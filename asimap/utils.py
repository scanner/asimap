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
import email.Utils
import calendar
import pytz

############################################################################
#
def parsedate(date_time_str):
    """All date time data is stored as a datetime.datetime object in UTC.
    This routine uses common routines provided by python to parse a rfc822
    formatted date time in to a datetime.datetime object.

    It is pretty simple, but makes the code a lot shorter and easier to read.
    """
    return datetime.datetime.fromtimestamp(\
            email.Utils.mktime_tz(email.Utils.parsedate_tz(date_time_str)),
            pytz.UTC)

############################################################################
#
def formatdate(datetime, localtime = False, usegmt = False):
    """This is the reverse. It will take a datetime object and format
    and do the deconversions necessary to pass it to email.Utils.formatdate()
    and thus return a string properly formatted as an RFC822 date.
    """
    return email.Utils.formatdate(calendar.timegm(datetime.utctimetuple()),
                                  localtime = localtime, usegmt = usegmt)
