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
from typing import Any


#######################################################################
#
# We have some basic exceptions used during the processing of commands
# to determine how we respond in exceptional situations
#
class ProtocolException(Exception):
    def __init__(self, value: str = "protocol exception") -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


##################################################################
##################################################################
#
class No(ProtocolException):
    def __init__(self, value: str = "no") -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


##################################################################
##################################################################
#
class Bad(ProtocolException):
    def __init__(self, value: str = "bad") -> None:
        self.value = value

    def __str__(self) -> str:
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
        self,
        value: str = "mailbox inconsistencey",
        mbox_name: str | None = None,
        msg_key: int | None = None,
    ) -> None:
        self.value = value
        self.mbox_name = mbox_name
        self.msg_key = msg_key

    def __str__(self) -> str:
        return f"{self.value} in mailbox '{self.mbox_name}', msg key: {str(self.msg_key)}"


##################################################################
##################################################################
#
class MailboxLock(ProtocolException):
    """
    Raised when we are unable to get a lock on a mailbox
    """

    ##################################################################
    #
    def __init__(self, value: str = "Mailbox lock", mbox: Any = None) -> None:
        """
        Arguments:
        - `value`:
        - `mbox`: the mbox.Mailbox object we had a problem getting a lock on
        """
        self.value = value
        self.mbox = mbox

    ##################################################################
    #
    def __str__(self) -> str:
        if self.mbox is None:
            return self.value
        else:
            return f"{self.value} on mailbox {self.mbox.name}"


############################################################################
#
# Our authentication system has its own set of exceptions.
#
class AuthenticationException(Exception):
    def __init__(self, value: str = "bad!") -> None:
        self.value = value

    def __str__(self) -> str:
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
