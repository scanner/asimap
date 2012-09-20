#!/usr/bin/env python
#
# File: $Id: auth.py 1926 2009-04-02 17:00:17Z scanner $
#
"""
This module defines classes that are used by the main server to
authenticate users. You sub-class the BaseAuth class to support
different authentication systems.
"""

# system imports
#
import os
import os.path
import pwd
import logging

# asimapd imports
#
from asimap.user import User
from asimap.exceptions import NoSuchUser, BadAuthentication

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
        """
        """
        pass

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
    There is only the user 'test', its password is 'test' and the
    'local user' is whatever 'os.getlogin()' returns.

    The maildir is fixed to '/var/tmp/testmaildir'
    """
    #########################################################################
    #
    def authenticate(self, username, password):
        # homedir = os.path.expanduser("~")
        # maildir = os.path.join(homedir, "Mail")
        maildir = "/var/tmp/testmaildir"

        if username == "foobie" and password == "test":
            return User("test", os.getlogin(), maildir)
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

AUTH_SYSTEMS = { "test_auth" : TestAuth() }
try:
    AUTH_SYSTEMS['simple_auth'] = SimpleAuth()
except (OSError,IOError), e:
    log.warn("Unable to initialize the SimpleAuth module: %s" % str(e))
