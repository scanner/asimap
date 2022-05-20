#!/usr/bin/env python
#
# File: $Id$
#
"""
We have this concept of users.

A user has some name that they are known to IMAP clients by (for
authentication).

This name may be different than the one the operating system knows the
user by.

Every user that can use the IMAP server has a directory where their
mailspool is stored.
"""


##################################################################
##################################################################
#
class User(object):
    """
    The basic user object.

    It only stores the auth user name, what user we should setuid to,
    and what the path to their mailspool is.
    """

    ##################################################################
    #
    def __init__(self, imap_username, local_username, mailspool_dir):
        """ """
        self.imap_username = imap_username
        self.local_username = local_username
        self.maildir = mailspool_dir

        # We associate the auth system that was used to authenticate the
        # user with the user in case other parts of the system want to
        # know more about this authentication.
        #
        self.auth_system = None
        return

    ##################################################################
    #
    def __str__(self):
        return "IMAP username: '%s', local username: '%s'" % (
            self.imap_username,
            self.local_username,
        )
