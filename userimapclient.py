#!/usr/bin/env python
#
# File: $Id: userimapclient.py 1926 2009-04-02 17:00:17Z scanner $
#
"""
This module contains the UserIMAPClient class. This is used by the
UserServer and various objects for a specific user's set of
mailboxes. There is one of these for every IMAP client that has
authenticated specifically to a single user's mailbox.

The UserIMAPClient class defines methods that things like the
UserServer, MailboxController and various Mailboxes can invoke to send
asynchronous messages back to the actual IMAP client.

The UserIMAPClient is also what a parsed IMAP command is sent to by
the UserServer to be processes.

The UserIMAPClient also maintains the bits of state associated with a
single IMAP connection (like what mailbox is selected and such.)
"""

# system imports
#
