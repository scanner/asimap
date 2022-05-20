#!/usr/bin/env python
#
# File: $Id: auth.py 1926 2009-04-02 17:00:17Z scanner $
#
"""
This module defines classes that are used by the main server to
authenticate users. You sub-class the BaseAuth class to support
different authentication systems.
"""

import logging

# system imports
#
import os
import os.path
import pwd
import random
import string

from asimap.exceptions import BadAuthentication, NoSuchUser

# asimapd imports
#
from asimap.user import User

# Our module logger..
#
log = logging.getLogger(__name__)


############################################################################
#
class BaseAuth(object):
    """
    This is an abstract authentication system class. It defines the basic
    methods any authentication system used by mhimap will need to implement.

    It is important to note that we want to allow several
    authentication systems to exist at once and that these can be used
    in different ways.. the way that the client authenticates to the
    IMAP server will pick a system, and some systems are layered in
    that they will basically combine several systems in a certain
    ordering.
    """

    #########################################################################
    #
    def __init__(self):
        """ """
        self.log = logging.getLogger(__name__)

    #########################################################################
    #
    def authenticate(self, username, password):
        """This method must be implemented in each subclass. It will likely
        take very different arguments. For example a kerberos auth system is
        likely to take a username, and a network connection which it will use
        to communicate to the actual client of the IMAP server to do its
        specific authentication system.

        This one takes the prototypical arguments of username and password

        This method is expected to return an appropriate User object that
        represents the user that the client authenticated as.

        It will raise BadAuthentication if the credentials presented are not
        valid.
        """
        raise NotImplemented


############################################################################
#
class TestAuth(BaseAuth):
    """
    TestAuth is used by `test_mode`. In test_mode there is only one
    user, and the main server process does not need to run as root as
    it does not do a 'setuid' when creating the user-specific
    subprocess.

    `test_mode` is intended to be part of a test harness so it makes
    assumptions about where it is being run and uses that to write the
    dyanmically generated credentials in a to file with a well known
    name. Every run generates new credentials.

    We use the 'current working directory' of the process to look for
    the directory 'test/test_mode' or just 'test_mode' as the mail dir
    for the single user.

    The credentials are written in to a file named
    `test_mode_creds.txt`. It contains a single line of the format
    `<username>:<password>`
    """

    ####################################################################
    #
    def __init__(self):
        """
        Determine the mail dir.
        Dynamically generate and store the username and password.
        """
        super(TestAuth, self).__init__()
        self.cwd = os.getcwd()
        self.maildir = None

        alpha_num = string.uppercase + string.lowercase + string.digits

        self.username = "".join(random.choice(alpha_num) for i in range(16))
        self.password = "".join(random.choice(alpha_num) for i in range(16))

        for path in ("test_mode", "test/test_mode"):
            maildir = os.path.join(self.cwd, path)
            if os.path.isdir(maildir):
                self.maildir = maildir
                break

        # There is no maildir to use so do nothing.
        # No possibiity of logging in via test_mode.
        if self.maildir is None:
            self.log.debug(
                "Unable to initialize the TestAuth module, "
                "no suitable test_mode maildir found"
            )
            return

        creds_file = os.path.join(self.maildir, "test_mode_creds.txt")
        with open(creds_file, "w") as f:
            f.write("{}:{}".format(self.username, self.password))

        self.log.debug(
            "Using maildir: {}".format(os.path.abspath(self.maildir))
        )
        self.log.debug("Credentials file: {}".format(creds_file))
        self.log.debug(
            "Username: '{}', password: '{}'".format(
                self.username, self.password
            )
        )

    #########################################################################
    #
    def authenticate(self, username, password):
        if username == self.username and password == self.password:
            return User(self.username, os.getlogin(), self.maildir)
        raise NoSuchUser("There is no user '%s'." % username)


##################################################################
##################################################################
#
class SimpleAuth(BaseAuth):
    """
    A simple authentication system that uses a plain text password
    database with hashed and salted passwords.
    """

    ##################################################################
    #
    def __init__(self):
        super(BaseAuth, self).__init__()
        from asimap.password_db import password_db

        self.password_db = password_db
        return

    #########################################################################
    #
    def authenticate(self, username, password):

        # if the password does not pan out..
        #
        if not self.password_db.check_password(username, password):
            raise BadAuthentication

        # Otherwise get their homedir and their maildir and setup the
        # user object.
        #
        p = pwd.getpwnam(username)
        homedir = p.pw_dir
        maildir = os.path.join(homedir, "Mail")

        return User(username, username, maildir)


AUTH_SYSTEMS = {"test_auth": TestAuth()}

try:
    AUTH_SYSTEMS["simple_auth"] = SimpleAuth()
except (OSError, IOError) as e:
    log.warn("Unable to initialize the SimpleAuth module: %s" % str(e))
