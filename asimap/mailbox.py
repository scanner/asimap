#!/usr/bin/env python
#
# File: $Id$
#
"""
The module that deals with the mailbox objects.

There will be a mailbox per MH folder (but not one for the top level
that holds all the folders.)
"""

# system imports
#

# asimap import
#
from asimap.client import No, Bad

##################################################################
##################################################################
#
class MailboxException(No):
    def __init__(self, value = "no"):
        self.value = value
    def __str__(self):
        return repr(self.value)
class MailboxExists(MailboxException):
    pass
class NoSuchMailbox(MailboxException):
    pass
class InvalidMailbox(MailboxException):
    pass
        
##################################################################
##################################################################
#
class Mailbox(object):
    """
    """

    ##################################################################
    #
    def __init__(self, name, server):
        """
        This represents an active mailbox. You can only instantiate
        this class for mailboxes that actually in the file system.

        You need to use the class method 'create()' if you wish to
        create a mailbox that does not already exist.

        Arguments:
        - `name`: The mailbox name. This must represent a mailbox that exists.
        - `server`: A reference to the user_server object which ties
                    together all of the active mailboxes, the
                    database connection, and all of the IMAP clients
                    currently connected to us.
        """
        self.server = server
        self.name = name
        return

    #########################################################################
    #
    @classmethod
    def create(cls, name, server):
        """
        Creates a mailbox on disk that does not already exist and
        instantiates a Mailbox object for it.
        """
        if name == "inbox":
            raise InvalidMailbox("Can not create a mailbox named 'inbox'")

        # ... ....
        # ... .... Do useful stuff here
        # ... ....
        
        return cls(name, server)
    
        
    
