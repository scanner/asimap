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

import calendar

# system imports
#
import datetime
import email.utils
import hashlib
import logging
import os
import pwd
import random
import re
import sys

import pytz

# asimap imports
#
from .exceptions import Bad

LOG = logging.getLogger("%s" % (__name__,))

# RE used to suss out the digits of the uid_vv/uid header in an email
# message
#
uid_re = re.compile(r"(\d+)\s*\.\s*(\d+)")


############################################################################
#
def parsedate(date_time_str):
    """
    All date time data is stored as a datetime.datetime object in UTC.
    This routine uses common routines provided by python to parse a rfc822
    formatted date time in to a datetime.datetime object.

    It is pretty simple, but makes the code a lot shorter and easier to read.
    """
    return datetime.datetime.fromtimestamp(
        email.utils.mktime_tz(email.utils.parsedate_tz(date_time_str)),
        pytz.UTC,
    )


############################################################################
#
def formatdate(datetime, localtime=False, usegmt=False):
    """
    This is the reverse. It will take a datetime object and format
    and do the deconversions necessary to pass it to email.utils.formatdate()
    and thus return a string properly formatted as an RFC822 date.
    """
    return email.utils.formatdate(
        calendar.timegm(datetime.utctimetuple()),
        localtime=localtime,
        usegmt=usegmt,
    )


####################################################################
#
def sequence_set_to_list(seq_set, seq_max, uid_cmd=False):
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
                raise Bad(
                    "Message index '*' is greater than the size of "
                    "the mailbox"
                )
            result.append(seq_max)
        elif isinstance(elt, int):
            if elt > seq_max and not uid_cmd:
                raise Bad(
                    f"Message index '{elt}' is greater than the size of "
                    "the mailbox"
                )
            result.append(elt)
        elif isinstance(elt, tuple):
            start, end = elt
            if str(start) == "*":
                start = seq_max
            if str(end) == "*":
                end = seq_max
            if (
                start == 0 or end == 0 or start > seq_max or end > seq_max
            ) and not uid_cmd:
                raise Bad(
                    f"Message sequence '{elt}' is greater than the size of "
                    f"the mailbox, start: {start}, end: {end}, "
                    f"seq_max: {seq_max}"
                )
            if start > end:
                result.extend(list(range(end, start + 1)))
            else:
                result.extend(list(range(start, end + 1)))
    return sorted(set(result))


############################################################################
#
# This was copied from django's daemonize module,
#
# http://www.djangoproject.org/
#
if os.name == "posix":

    def daemonize(our_home_dir=".", out_log="/dev/null", err_log="/dev/null"):
        "Robustly turn into a UNIX daemon, running in our_home_dir."
        # First fork
        try:
            if os.fork() > 0:
                sys.exit(0)  # kill off parent
        except OSError as e:
            sys.stderr.write(f"fork #1 failed: ({e.errno}) {e.strerror}\n")
            sys.exit(1)
        os.setsid()
        os.chdir(our_home_dir)
        os.umask(0)

        # Second fork
        try:
            if os.fork() > 0:
                os._exit(0)
        except OSError as e:
            sys.stderr.write(f"fork #2 failed: {e.errno} {e.strerror}\n")
            os._exit(1)

        si = open("/dev/null", "r")
        so = open(out_log, "a+", 0)
        se = open(err_log, "a+", 0)
        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(se.fileno(), sys.stderr.fileno())
        # Set custom file descriptors so that they get proper buffering.
        sys.stdout, sys.stderr = so, se

else:

    def daemonize(our_home_dir=".", out_log=None, err_log=None):
        """
        If we're not running under a POSIX system, just simulate the daemon
        mode by doing redirections and directory changing.
        """
        os.chdir(our_home_dir)
        os.umask(0)
        sys.stdin.close()
        sys.stdout.close()
        sys.stderr.close()
        if err_log:
            sys.stderr = open(err_log, "a", 0)
        else:
            sys.stderr = NullDevice()
        if out_log:
            sys.stdout = open(out_log, "a", 0)
        else:
            sys.stdout = NullDevice()

    class NullDevice:
        "A writeable object that writes to nowhere -- like /dev/null."

        def write(self, s):
            pass


############################################################################
#
def become_user(user=None):
    """
    Change to run as the specified user. If 'None' then we just return.
    If we are already running as the given user, also do nothing and return.
    """
    if user is None:
        return

    current_user = pwd.getpwuid(os.getuid())
    if current_user[0] == user:
        return

    pwinfo = pwd.getpwnam(user)
    os.setregid(pwinfo[3], pwinfo[3])
    os.setreuid(pwinfo[2], pwinfo[2])
    return


############################################################################
#
def get_hexdigest(algorithm, salt, raw_password):
    """
    Returns a string of the hexdigest of the given plaintext password and salt
    using the given algorithm ('md5', 'sha1' or 'crypt').

    Borrowed from the django User auth model.
    """
    if algorithm == "crypt":
        try:
            import crypt
        except ImportError:
            raise ValueError(
                '"crypt" password algorithm not supported in '
                "this environment"
            )
        return crypt.crypt(raw_password, salt)

    if algorithm == "md5":
        return hashlib.md5(salt + raw_password).hexdigest()
    elif algorithm == "sha1":
        return hashlib.sha1(salt + raw_password).hexdigest()
    raise ValueError("Got unknown password algorithm type in password.")


############################################################################
#
def check_password(raw_password, enc_password):
    """
    Returns a boolean of whether the raw_password was correct. Handles
    encryption formats behind the scenes.
    """
    algo, salt, hsh = enc_password.split("$")
    return hsh == get_hexdigest(algo, salt, raw_password)


####################################################################
#
def hash_password(raw_password):
    """
    Convert the given raw password in to the hex digest we store.

    Arguments:
    - `raw_password`: The plain text password
    """
    algo = "sha1"
    salt = get_hexdigest(algo, str(random.random()), str(random.random()))[:5]
    hsh = get_hexdigest(algo, salt, raw_password)
    return f"{algo}${salt}${hsh}"


####################################################################
#
def get_uidvv_uid(hdr):
    """
    Given a string that is supposedly the value of the 'x-asimapd-uid'
    header from an email message return a tuple comprised of the
    uid_vv, and uid parsed out of that header's contents.

    This deals with the case where we get a malformed header that
    actually has a continuation of the next line mangled into it. It
    does not happen often but some historical messages look like this.

    If we can not parse the uid_vv, uid then we return (None, None)
    which is supposed to be a signal to our caller that this message
    does not have a valid uid_vv, uid.

    Arguments:
    - `hdr`: A string that is the contents of the 'x-asimapd-uid' header from
             an email message.
    """
    s = uid_re.search(hdr)
    if s:
        return tuple((int(x) for x in s.groups()))
    return (None, None)
