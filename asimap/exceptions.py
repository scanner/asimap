#!/usr/bin/env python
#
# File: $Id$
#
"""
Some exceptions need to be generally available to many modules so they are
kept in this module to avoid ciruclar dependencies.
"""

# system imports
#


#######################################################################
#
# We have some basic exceptions used during the processing of commands
# to determine how we respond in exceptional situations
#
class ProtocolException(Exception):
    def __init__(self, value="protocol exception"):
        self.value = value

    def __str__(self):
        return self.value


##################################################################
##################################################################
#
class No(ProtocolException):
    def __init__(self, value="no"):
        self.value = value

    def __str__(self):
        return self.value


##################################################################
##################################################################
#
class Bad(ProtocolException):
    def __init__(self, value="bad"):
        self.value = value

    def __str__(self):
        return self.value


##################################################################
##################################################################
#
class MailboxInconsistency(ProtocolException):
    """
    When processing commands on a mailbox it is possible to hit a
    state where what is on disk is not what we expected it to be.

    Frequently the base action in these cases is to punt (because we
    are usually in a place where we can not regain consistency and
    maintain state).

    The upper layer is expected to catch this, initiate actions to
    regain consistent state, and then likely try the command again.
    """

    def __init__(
        self, value="mailbox inconsistencey", mbox_name=None, msg_key=None
    ):
        self.value = value
        self.mbox_name = mbox_name
        self.msg_key = msg_key

    def __str__(self):
        return "%s in mailbox '%s', msg key: %s" % (
            self.value,
            self.mbox_name,
            str(self.msg_key),
        )


##################################################################
##################################################################
#
class MailboxLock(ProtocolException):
    """
    Raised when we are unable to get a lock on a mailbox
    """

    ##################################################################
    #
    def __init__(self, value="Mailbox lock", mbox=None):
        """
        Arguments:
        - `value`:
        - `mbox`: the mbox.Mailbox object we had a problem getting a lock on
        """
        self.value = value
        self.mbox = mbox

    ##################################################################
    #
    def __str__(self):
        if self.mbox is None:
            return self.value
        else:
            return "%s on mailbox %s" % (self.value, self.mbox.name)


############################################################################
#
# Our authentication system has its own set of exceptions.
#
class AuthenticationException(Exception):
    def __init__(self, value="bad!"):
        self.value = value

    def __str__(self):
        return repr(self.value)


############################################################################
#
class BadAuthentication(AuthenticationException):
    pass


############################################################################
#
class NoSuchUser(AuthenticationException):
    pass


############################################################################
#
class AuthenticationError(AuthenticationException):
    pass
