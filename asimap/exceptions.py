#!/usr/bin/env python
#
# File: $Id$
#
"""
Exceptions shared across multiple asimap modules.

These are defined here to avoid circular import dependencies. The module
provides both IMAP protocol-level exceptions (ProtocolException, No, Bad)
and authentication-related exceptions.
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
    """Base class for IMAP protocol-level exceptions.

    Subclasses map to IMAP tagged response codes (NO, BAD) that are sent
    back to the client when a command cannot be completed.
    """

    def __init__(self, value: str = "protocol exception") -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


##################################################################
##################################################################
#
class No(ProtocolException):
    """Raised when a valid IMAP command cannot be completed (NO response).

    Used to signal that the command was syntactically correct but failed
    for a logical reason, such as a mailbox not existing or a flag being
    read-only.
    """

    def __init__(self, value: str = "no") -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


##################################################################
##################################################################
#
class Bad(ProtocolException):
    """Raised when an IMAP command is malformed or violates protocol (BAD response).

    Used to signal client errors such as unknown commands, invalid arguments,
    or protocol violations.
    """

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
        value: str = "mailbox inconsistency",
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
        Args:
            value: Human-readable description of the lock failure.
            mbox: The Mailbox object we were unable to acquire a lock on.
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
    """Base class for authentication-related exceptions."""

    def __init__(self, value: str = "bad!") -> None:
        self.value = value

    def __str__(self) -> str:
        return repr(self.value)


############################################################################
#
class BadAuthentication(AuthenticationException):
    """Raised when a user provides an incorrect password."""

    pass


############################################################################
#
class NoSuchUser(AuthenticationException):
    """Raised when the given username does not exist in the password file."""

    pass


############################################################################
#
class AuthenticationError(AuthenticationException):
    """Raised for unexpected errors during the authentication process."""

    pass
