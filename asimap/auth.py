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

# asimapd imports
#
from asimap.user import User

############################################################################
#
# Our authentication system has its own set of exceptions.
#
class AuthenticationException(Exception):
    def __init__(self, value = "bad!"):
        self.value = value
    def __str__(self):
        return repr(self.value)
class BadAuthentication(AuthenticationException):
    pass
class NoSuchUser(AuthenticationException):
    pass
class AuthenticationError(AuthenticationException):
    pass

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
class TestAuth(object):
    """This is an authentication class to use in our simple test server.
    It uses a dictionary of users & passwords that is in a module
    in our tests/ module.
    """
    #########################################################################
    #
    def authenticate(self, username, password):
        if username == "test" and password == "test":
            return User("test", "nobody", "/var/tmp/testmaildir")
        raise NoSuchUser("There is no user '%s'." % username)

AUTH_SYSTEMS = { "test_auth" : TestAuth() }
