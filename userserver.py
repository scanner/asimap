#!/usr/bin/env python
#
# File: $Id: userserver.py 1926 2009-04-02 17:00:17Z scanner $
#
"""
This module contains the classes and definitions that are the
controlling server for a single logged in user's IMAP connection.

An instance of the UserServer is what receives commands from IMAP
clients for a specific authenticated user, maintains the instance of
the IMAP account with all of its mailboxes, causes IMAP commands to be
parsed and executed and sends the results back to the calling client.

NOTE: This is the root of a separate process. There is only one
      process per user, so if a user is logged in to the IMAP server
      from multiple locations, there is still only one process serving
      all the requests for that user.
"""

# system imports
#
