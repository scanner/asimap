#!/usr/bin/env python
#
# File: $Id$
#
"""
While doing many operations in quick succession we will refer to the
same message over and over again. These will usually be when a mailbox
is locked and once we retrieve a message from disk for a mailbox we
can be pretty sure that it is not going to change any time soon.

So to avoid continually fetching and re-fetching messages from disk
that have not changed we setup a LRU cache.

The cache is shared between all mailboxes so that we can enforce a
global limit to how much memory it will consume. This size limit will
be what forces the LRU algorithm to take place.

At times when the Mailbox knows that the cache is probably invalid it
can tell us to clear all the entries for that mailbox.
"""

# system imports
#

import logging
import time
from functools import reduce

# asimap imports
#
from asimap.exceptions import MailboxInconsistency

CACHE_SIZE = 20971520  # Max cache size (in bytes) -- 20MiB


##################################################################
##################################################################
#
class MessageCache(object):
    """
    Our message cache.

    Cache in memory MHMessage objects for quick future retrieval.

    Expire older messages from the cache when the total size of all of
    the messages exceeds a set limit.

    Allow a way to clear all messages in the cache that belong to a
    specific mailbox.

    Allow a way to clear all messages in the cache.
    """

    ##################################################################
    #
    def __init__(self, max_size=CACHE_SIZE):
        """
        Default size: 20mb

        Arguments:

        - `max_size`: Limit in octets of how many messages we will
          store in the cache.
        """
        self.log = logging.getLogger(
            "%s.%s" % (__name__, self.__class__.__name__)
        )
        self.max_size = max_size
        self.cur_size = 0
        self.num_msgs = 0
        self.msgs_by_mailbox = {}
        return

    ##################################################################
    #
    def __str__(self):
        """
        For string return the object and some stats about it.
        """
        num_msgs = reduce(
            lambda x, y: x + y, [len(z) for z in self.msgs_by_mailbox.values()]
        )
        return (
            "<%s.%s: size: %d, number of mboxes: %d, number of "
            "messages: %d"
            % (
                __name__,
                self.__class__.__name__,
                self.cur_size,
                len(self.msgs_by_mailbox),
                num_msgs,
            )
        )

    ##################################################################
    #
    def add(self, mbox, msg_key, msg):
        """
        Add the given message to the given mailbox's cache.

        If the msg_key already exists in the mailbox's cache then
        update its message with the one passed in.

        Arguments:
        - `mbox`: name of the mailbox to add message to
        - `msg_key`: The key for this message (in the MH folder)
        - `msg`: message to be added to mailbox
        """
        # If you try to add a message to the cache without a UID header
        # we are going to raise a MailboxInconsistency exception.
        #
        # Somewhere up the call stack it will see this and trigger a
        # resync of the mailbox and then re-try the failed command.
        #
        if "x-asimapd-uid" not in msg:
            self.log.error(
                "add: mailbox '%s' inconsistency msg key %d has no"
                " UID header" % (mbox, msg_key)
            )
            raise MailboxInconsistency(mbox_name=mbox, msg_key=msg_key)

        if mbox not in self.msgs_by_mailbox:
            self.msgs_by_mailbox[mbox] = []

        msg_size = len(msg.as_string())
        self.cur_size += msg_size
        self.msgs_by_mailbox[mbox].append(
            (msg_key, msg_size, msg, time.time())
        )

        # If we have exceeded our max size remove the oldest messages
        # until we go under our max size.
        #
        while self.cur_size > self.max_size:
            oldest = None
            for mbox_name in self.msgs_by_mailbox.keys():
                if len(self.msgs_by_mailbox[mbox_name]) == 0:
                    continue
                if oldest is None:
                    oldest = (mbox_name, self.msgs_by_mailbox[mbox_name][0])
                elif oldest[1][3] > self.msgs_by_mailbox[mbox_name][0][3]:
                    oldest = (mbox_name, self.msgs_by_mailbox[mbox_name][0])
            if oldest is None:
                self.log.warn(
                    "Unable to get cur_size %d under max size %d"
                    % (self.cur_size, self.max_size)
                )
                return
            self.msgs_by_mailbox[oldest[0]].pop(0)
            if len(self.msgs_by_mailbox[oldest[0]]) == 0:
                del self.msgs_by_mailbox[oldest[0]]
            # self.log.debug("removing from cache: %s" % str(oldest))
            self.cur_size -= oldest[1][1]
        return

    ##################################################################
    #
    def get(self, mbox, msg_key, remove=False):
        """
        Get the message in the given mailbox under the given MH folder key.

        If there is no such message then return None

        XXX maybe we should raise an exception?

        Arguments:
        - `mbox`: name of the mbox we are looking in
        - `msg_key`: The MH folder key we are looking up
        - `remove`: instead of re-adding this message to the end of a
          mailbox's list we just remove it from the mailbox.
        """
        if mbox not in self.msgs_by_mailbox:
            return None

        result = None
        for msg_item in self.msgs_by_mailbox[mbox]:
            if msg_item[0] == msg_key:
                result = msg_item
                break

        if result is None:
            return None

        # If we did find our message then we remove it from the list and
        # append it to the end of the list, resetting its time.
        #
        self.msgs_by_mailbox[mbox].remove(result)
        if not remove:
            result = (result[0], result[1], result[2], time.time())
            self.msgs_by_mailbox[mbox].append(result)
        else:
            self.cur_size -= result[1]
        return result[2]

    ##################################################################
    #
    def remove(self, mbox, msg_key):
        """
        Sometimes we need to remove a message from the cache. usually
        when we are doing things like changing which sequences it is
        in. This will frequently be followed by an add.

        If the message is not in the cache we do nothing.

        Arguments:
        - `mbox`: Name of the mailbox this message is in
        - `msg_key`: the MH folder key for the message.
        """
        self.get(mbox, msg_key, remove=True)

    ##################################################################
    #
    def clear_mbox(self, mbox):
        """
        Clear all cached messages for the given mailbox.

        Arguments:
        - `mbox`: name of the nailbox cache to clear
        """
        if mbox not in self.msgs_by_mailbox:
            return
        for msg_item in self.msgs_by_mailbox[mbox]:
            self.cur_size -= msg_item[1]
        del self.msgs_by_mailbox[mbox]
        self.log.debug(
            "Clear mbox %s from the message cache, "
            "new size: %d (%.1f%% full, %.1fMib)"
            % (
                mbox,
                self.cur_size,
                (self.cur_size / self.max_size) * 100,
                (self.cur_size / 1048576),
            )
        )
        return

    ##################################################################
    #
    def clear(self):
        """
        Clear the entire cache.
        """
        self.msgs_by_mailbox = {}
        self.cur_size = 0
        return
