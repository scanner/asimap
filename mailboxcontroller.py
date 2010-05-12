#!/usr/bin/env python
#
# File: $Id: mailboxcontroller.py 1926 2009-04-02 17:00:17Z scanner $
#
"""
This module contains the classes and supporting methods for
controlling all of the mailboxes for a specific logged in user.

This maintains a list of all the mailboxes that exist for this
user. This makes sure those mailboxes are regularly checked for new
mail.

When the userserver wants a mailbox, it request it of this module.
"""

# system imports
#
