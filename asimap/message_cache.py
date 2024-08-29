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
from collections import Counter
from mailbox import MHMessage
from typing import Dict, List, Optional, Tuple, TypeAlias

# Project imports
#
from .generator import get_msg_size

logger = logging.getLogger("asimap.message_cache")
CACHE_SIZE = 20_971_520  # Max cache size (in bytes) -- 20MiB

# CacheEntry members are:
#
#    msg_key: int, msg_size:int, msg: MHMessage, time: float
#
CacheEntry: TypeAlias = Tuple[int, int, MHMessage, float]


##################################################################
##################################################################
#
class MessageCache:
    """
    Our message cache.

    Cache in memory MHMessage objects for quick future retrieval.

    Expire older messages from the cache when the total size of all of
    the messages exceeds a set limit.

    Allow a way to clear all messages in the cache that belong to a
    specific mailbox.

    Allow a way to clear all messages in the cache.
    """

    STAT_LOG_INTERVAL = 60.0  # In seconds. Probably be 300 in production.

    ##################################################################
    #
    def __init__(self, max_size=CACHE_SIZE):
        """
        - `max_size`: Limit in octets of how many messages we will
          store in the cache.
        """
        self.max_size: int = max_size
        self.cur_size: int = 0
        self.num_msgs: int = 0

        # We want to periodically say how large the message cache is, so
        # we keep a timestamp of when we last reported the size.
        #
        self.next_size_report = 0.0

        # A list of message entries ordered by entry age. The oldest entry is
        # at the end of the list.  Maybe this should be a heap, sorted by time.
        # still need to make sure deleting and inserting a node is cheap.
        #
        # XXX Never setup or used. Should evaluate if we need this optimizaton
        #
        # self.msgs_by_age: List[CacheEntry] = []

        # Messages are indexed by the string "<mbox name>:<msg key>"
        #
        # XXX Never setup or used. Should evaluate if we need this optimizaton
        #
        # self.msgs_by_mbox_msg_key: Dict[str, CacheEntry] = {}

        # A frequent operation getting all the msg keys for messages by
        # mbox. Called once every non-optional resync (when mbox mtimes change)
        #
        # XXX Never setup or used. Should evaluate if we need this optimizaton
        #
        # self.msg_keys_by_mbox: Dict[str, List[int]] = defaultdict(list)

        # The msgs_by_mailbox is our "LRU"
        # The key is for the mailbox.
        # Under each key is a list of tuples.
        # Each tuple has in it:
        #    msg_key: int, msg_size:int, msg: MHMessage, time: float
        # Older messages are at the end of the list.
        #
        self.msgs_by_mailbox: Dict[str, List[CacheEntry]] = {}

        self.cache_hits: Counter[str] = Counter()
        self.cache_misses: Counter[str] = Counter()

        # XXX We should probably add a dict `msgs_by_mailbox_by_msgkey` that
        #     lets us directly look up a message by its message key instead of
        #     having to loop through the list.  Currently this would not work
        #     because every time we get the message we will need to find it in
        #     the `msgs_by_mailbox` list anyways.
        #
        #     So this is currently unused.
        #
        #     We will likely need to figure this out soon. Some people have an
        #     `inbox` with 8,000+ messages and if we cache a large number of
        #     those the lookups are going to be intense (but do we cache that
        #     many?)
        #
        # self.msgs_by_mailbox_by_msg_key: Dict[str, Dict[int, CacheEntry]] = {}

    ####################################################################
    #
    def _log_stats(self, force: bool = False) -> None:
        """
        Dump to the log our stats every now and then.
        If `force` is True the stats will be dumped regardless.
        """
        now = time.monotonic()
        if force or now >= self.next_size_report:
            logger.info("Size report: %s", str(self))
            logger.info(
                "total cache hits: %d, total cache misses: %d",
                self.cache_hits.total(),
                self.cache_misses.total(),
            )
            for mbox in sorted(
                set(self.cache_hits.keys()) | set(self.cache_misses.keys())
            ):
                logger.info(
                    "mailbox '%s': cache hits: %d, cache misses: %d",
                    mbox,
                    self.cache_hits[mbox],
                    self.cache_misses[mbox],
                )
            self.cache_hits.clear()
            self.cache_misses.clear()
            self.next_size_report = now + self.STAT_LOG_INTERVAL

    ##################################################################
    #
    def __str__(self):
        """
        For string return the object and some stats about it.
        """
        return (
            f"<MessageCache: size: {self.cur_size}, "
            f"number of mboxes: {len(self.msgs_by_mailbox)}, "
            f"number of messages: {self.num_msgs}>"
        )

    ##################################################################
    #
    def add(self, mbox: str, msg_key: int, msg: MHMessage):
        """
        Add the given message to the given mailbox's cache.

        If the msg_key already exists in the mailbox's cache then
        update its message with the one passed in.

        Arguments:
        - `mbox`: name of the mailbox to add message to
        - `msg_key`: The key for this message (in the MH folder)
        - `msg`: message to be added to mailbox
        """
        # I think for now we will not worry about missing uid's in cached
        # messages. This should only happen during resync's, and at the end of
        # which all the messages we put in the cache will have uids in them
        # (and done in such a way that the messages in the cache have uid's.)
        #
        # # If you try to add a message to the cache without a UID header
        # # we are going to raise a MailboxInconsistency exception.
        # #
        # # Somewhere up the call stack it will see this and trigger a
        # # resync of the mailbox and then re-try the failed command.
        # #
        # # XXX Do we need a UID in a message we cache? Since we will eventually
        # #     update the message anyways.. and if all calls go through the
        # #     message cache we will update the message in the message cache.
        # #
        # if UID_HDR not in msg:
        #     logger.error(
        #         "add: mailbox '%s' inconsistency msg key %d has no"
        #         " UID header",
        #         mbox,
        #         msg_key,
        #     )
        #     raise MailboxInconsistency(mbox_name=mbox, msg_key=msg_key)

        if mbox not in self.msgs_by_mailbox:
            self.msgs_by_mailbox[mbox] = []

        try:
            msg_size = get_msg_size(msg)
        except Exception as e:
            # One of the first hints that we have a message that python's mail
            # module can not handle due to encoding errors happens when we try
            # to get the size of a message. When this happens we need enough
            # context to know which messsage in which mailbox caused the issue
            # so we catch the error here and log the relevat
            # information. (Maybe more reasons to treat all messages as bytes
            # all the time)
            #
            logger.error(
                "Unable to get message for msg key: %d, mbox: '%s': %s",
                msg_key,
                mbox,
                e,
            )
            raise

        self.cur_size += msg_size
        self.num_msgs += 1
        self.msgs_by_mailbox[mbox].append((msg_key, msg_size, msg, time.time()))

        # If we have exceeded our max size remove the oldest messages
        # until we go under our max size.
        #
        start = time.time()
        while self.cur_size > self.max_size:
            oldest = None
            for mbox_name in self.msgs_by_mailbox.keys():
                if not self.msgs_by_mailbox[mbox_name]:
                    continue
                if oldest is None:
                    oldest = (mbox_name, self.msgs_by_mailbox[mbox_name][0])
                elif oldest[1][3] > self.msgs_by_mailbox[mbox_name][0][3]:
                    oldest = (mbox_name, self.msgs_by_mailbox[mbox_name][0])
            if oldest is None:
                logger.warning(
                    "Unable to get cur_size %d under max size %d",
                    self.cur_size,
                    self.max_size,
                )
                return

            # Remove the message at the front of the list for the mailbox with
            # the oldest message in it. If the cache for that mailbox has no
            # messages, then remove the mailbox from dict of msgs by mailbox.
            #
            self.msgs_by_mailbox[oldest[0]].pop(0)
            if len(self.msgs_by_mailbox[oldest[0]]) == 0:
                del self.msgs_by_mailbox[oldest[0]]
            self.cur_size -= oldest[1][1]
            self.num_msgs -= 1
        duration = time.time() - start
        if duration >= 0.05:
            logger.debug("Message purge took %f.3s second", duration)
        self._log_stats()
        return

    ####################################################################
    #
    def msg_keys_for_mbox(self, mbox: str) -> List[int]:
        """
        Return a list of all the messages keys we have in the cache for a
        specific mailbox.

        Returns an empty list if that mailbox is not in the cache.
        """
        if mbox not in self.msgs_by_mailbox:
            return []

        return sorted([x[0] for x in self.msgs_by_mailbox[mbox]])

    ##################################################################
    #
    def _get(
        self,
        mbox: str,
        msg_key: int,
        remove: bool = False,
        do_not_update: bool = False,
        update_size: bool = False,
    ) -> Optional[CacheEntry]:
        """
        Get the message in the given mailbox under the given MH folder key.

        If there is no such message then return None

        Arguments:
        - `mbox`: name of the mbox we are looking in
        - `msg_key`: The MH folder key we are looking up
        - `remove`: instead of re-adding this message to the end of a
          mailbox's list we just remove it from the mailbox.
        - `do_not_update`: do not update this message's access time in the
          LRU. Usually called when doing many queries across all keys in the
          mailbox.
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

        if do_not_update:
            return result

        # If we did find our message then we remove it from the list and
        # append it to the end of the list, resetting its time.
        #
        self.msgs_by_mailbox[mbox].remove(result)
        if not remove:
            msg = result[2]
            if update_size:
                msg_size = get_msg_size(msg)
                self.cur_size -= result[1]
                self.cur_size += msg_size
            else:
                msg_size = result[1]
            result = (result[0], msg_size, result[2], time.time())
            self.msgs_by_mailbox[mbox].append(result)
        else:
            self.cur_size -= result[1]
            self.num_msgs -= 1
        return result

    ####################################################################
    #
    def get(
        self,
        mbox: str,
        msg_key: int,
        remove: bool = False,
        do_not_update: bool = False,
        update_size: bool = False,
    ) -> Optional[MHMessage]:
        """
        Return the cached message or none.
        """
        result = self._get(
            mbox,
            msg_key,
            remove=remove,
            do_not_update=do_not_update,
            update_size=update_size,
        )
        res = None
        if result:
            self.cache_hits[mbox] += 1
            res = result[2]
        else:
            self.cache_misses[mbox] += 1
        self._log_stats()
        return res

    ####################################################################
    #
    def update_message_sequences(self, mbox_name: str, msg_key: int, sequences):
        """
        current done by a function in `mbox` but should we move it into here?
        """
        raise NotImplementedError()

    ##################################################################
    #
    def remove(self, mbox: str, msg_key: int) -> None:
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
        self._log_stats()

    ##################################################################
    #
    def clear_mbox(self, mbox: str) -> None:
        """
        Clear all cached messages for the given mailbox.

        Arguments:
        - `mbox`: name of the nailbox cache to clear
        """
        if mbox not in self.msgs_by_mailbox:
            return
        self._log_stats(force=True)
        for msg_item in self.msgs_by_mailbox[mbox]:
            self.cur_size -= msg_item[1]
        self.num_msgs -= len(self.msgs_by_mailbox[mbox])
        del self.msgs_by_mailbox[mbox]
        self._log_stats(force=True)

    ##################################################################
    #
    def clear(self):
        """
        Clear the entire cache.
        """
        self._log_stats(force=True)
        self.msgs_by_mailbox = {}
        self.cur_size = 0
        self.num_msgs = 0
        self._log_stats(force=True)
