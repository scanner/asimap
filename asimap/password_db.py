#!/usr/bin/env python
#
# File: $Id$
#
"""
A simplistic password database. We use a plain text file that follows
this simple format:

<username>:<password>

If a line begins with '#' or is a blank line it is ignored.

The password is represented by:

    '[algo]$[salt]$[hexdigest]'

Where algo is one of: 'crypt', 'md5', 'sha1'

Like mentioned before the system is very simplistic. When we want to
check a password we see if the mtime on the password file is newer
than the mtime of the password was when we last checked.

If it is newer we read it all in to memory in to a dict.

We then look up the username in the dict.
"""

import logging

# system imports
#
import os
import os.path

import asimap.exceptions

# asimap imports
#
import asimap.utils

# XXX Not configurable! this is where the password db for the asimap
#     server goes. Letting it be configurable is deemed a security risk.
#
PASSWORD_DB_LOCATION = "/var/db/asimapd_passwords.txt"


##################################################################
##################################################################
#
class PasswordDB(object):
    """
    The in-memory password database.

    NOTE: This is not scaleable. If anyone is using this for a large
    set of users they are nuts, and since they have a large set of
    users the undoubtedly have the resources to either write a better
    password system or use a real IMAP server.
    """

    ##################################################################
    #
    def __init__(self, password_db):
        self.log = logging.getLogger(
            "%s.%s" % (__name__, self.__class__.__name__)
        )

        self.password_db = password_db

        # This is the dict mapping usernames to their hashed passwords.
        #
        self.passwords = {}

        # The mtime of the actual password db file last time we checked.
        #
        self.last_mtime = 0

        self.read_passwords()
        return

    ##################################################################
    #
    def read_passwords(self):
        """
        See if the password file's mtime has changed. If it has throw
        out the current password db and read in the new one.
        """
        mtime = os.path.getmtime(self.password_db)
        if mtime <= self.last_mtime:
            return

        new_passwords = {}

        with open(self.password_db, "r") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if len(line) == 0 or line[0] == "#":
                    continue

                # Make sure this is a valid input line. If it is not
                # then log an error.
                if ":" not in line:
                    self.log.error(
                        "Bad entry in password file at line %d: "
                        "'%s'" % (i + 1, line)
                    )
                    continue

                user, pw = line.split(":")
                new_passwords[user.strip()] = pw.strip()

        # We got this far, replace the password db with the new one.
        #
        self.passwords = new_passwords
        return

    ##################################################################
    #
    def check_password(self, user, raw_password):
        """
        Check to see if the given raw password is valid for this user.

        Arguments:
        - `user`: user name to check in the db.
        - `raw_password`: the unencoded password
        """

        # Conditionally check and re-read the password db on ever attempt.
        #
        self.read_passwords()

        # NOTE: We expect our caller to catch these exceptions and throttle
        # clients that ask for too many users that do not exist too often.
        #
        if user not in self.passwords:
            raise asimap.exceptions.NoSuchUser("There is no user '%s'." % user)
        return asimap.utils.check_password(raw_password, self.passwords[user])


# We maintain a singleon password db instance that everyone else refers to.
#
password_db = PasswordDB(PASSWORD_DB_LOCATION)
