#!/usr/bin/env python
#
# File: $Id$
#
"""
Classes and their supporting methods that represent an IMAP Search
structure.
"""

# system imports
#
import logging
import os.path
from datetime import datetime

import pytz

import asimap.constants

# asimap imports
#
import asimap.utils
from asimap.exceptions import MailboxInconsistency


############################################################################
#
class BadSearchOp(Exception):
    def __init__(self, value="bad search operation"):
        self.value = value

    def __str__(self):
        return "BadSearchOp: %s" % self.value


##################################################################
##################################################################
#
class SearchContext(object):
    """
    When running searches on we store various bits of context information that
    the IMAPSearch object may need to determine if a specific message is
    matched or not.
    """

    ##################################################################
    #
    def __init__(
        self, mailbox, msg_key, msg_number, seq_max, uid_max, sequences
    ):
        """
        A container to hold the contextual information an IMAPSearch
        objects to actually perform its matching function.

        Arguments:
        - `mailbox`: The mailbox the message lives in
        - `msg_key`: The message key (mailbox.get_message(msg_key))
        - `msg_number`: The imap message number for this message
        - `seq_max`: The largest message sequence number in this mailbox
        - `uid_max`: The largest assigned uid, or next_uid if there
          are no messages in this mailbox
        - `sequences`: The sequences for the mailbox. Passed in to save us from
          having to load and parse it separately for every message.
        """
        self.log = logging.getLogger(
            "%s.%s.%s.msg-%d"
            % (__name__, self.__class__.__name__, mailbox.name, msg_key)
        )

        self.mailbox = mailbox
        self.msg_key = msg_key
        self.seq_max = seq_max
        self.uid_max = uid_max
        self.msg_number = msg_number
        self.mailbox_sequences = sequences
        # self.msg = mailbox.mailbox.get_message(msg_key)
        # self.uid_vv, self.uid = [int(x) for x in
        #                          self.msg['x-asimapd-uid'].strip().split('.')]
        self.path = os.path.join(mailbox.mailbox._path, str(msg_key))
        self.internal_date = datetime.fromtimestamp(
            os.path.getmtime(self.path), pytz.UTC
        )

        # msg & uid are looked up and set ONLY if the search actually reaches
        # in to the message. We use read only attributes to fill in these
        # values.
        #
        self._msg = None
        self._uid_vv = None
        self._uid = None
        self._sequences = None
        return

    ##################################################################
    #
    @property
    def msg(self):
        """
        The message parsed in to a MHMessage object
        """
        if self._msg:
            return self._msg

        # We have not actually loaded the message yet..
        #
        self._msg = self.mailbox.get_and_cache_msg(self.msg_key)

        # If the uid is not set, then set it also at the same time.
        #
        if self._uid is None:
            self._uid_vv, self._uid = asimap.utils.get_uidvv_uid(
                self.msg["x-asimapd-uid"]
            )
            if self._uid is None:
                raise MailboxInconsistency(
                    mbox_name=self.mailbox.name, msg_key=self.msg_key
                )
        else:
            # If, after we get the message and if the UID is defined and if the
            # UID in the message does NOT match the UID we have then raise a
            # mailboxinconsistency error.
            #
            uid_vv, uid = asimap.utils.get_uidvv_uid(self.msg["x-asimapd-uid"])
            if self._uid != uid or uid is None:
                raise MailboxInconsistency(
                    mbox_name=self.mailbox.name, msg_key=self.msg_key
                )

        return self._msg

    ##################################################################
    #
    @property
    def uid(self):
        """
        The IMAP UID of the message
        """
        if self._uid:
            return self._uid

        # Check the uids cache in the mailbox first.
        #
        try:
            self._uid = self.mailbox.uids[self.msg_number - 1]
        except IndexError:
            self._uid_vv, self._uid = self.mailbox.get_uid_from_msg(
                self.msg_key
            )
        return self._uid

    ##################################################################
    #
    @property
    def uid_vv(self):
        """
        The IMAP UID Validity Value for the mailbox
        """
        if self._uid_vv:
            return self._uid_vv
        # Use the fast method of getting the uid/uidvv.
        #
        self._uid_vv, self._uid = self.mailbox.get_uid_from_msg(self.msg_key)
        return self._uid_vv

    ##################################################################
    #
    @property
    def sequences(self):
        """
        The list of sequences that this message is in. If the message is not
        loaded we avoid loading the message object by just getting the
        sequences directly from the mailbox and computing which sequences this
        message is in.
        """
        if self._sequences:
            return self._sequences

        # If the message is loaded use its sequence information.
        #
        if self._msg:
            return self._msg.get_sequences()

        # Look at the mailbox sequences and figure out which ones this message
        # is in, if any.
        #
        self._sequences = []
        for name, key_list in self.mailbox_sequences.items():
            if self.msg_key in key_list:
                self._sequences.append(name)
        return self._sequences


############################################################################
#
#
class IMAPSearch(object):
    """THis is an IMAPSearch object. It can instantiate all the possible
    criteria for a search of the messages in a mailbox. The possible search
    parameters are as defined in rfc2060.

    What this object does is it takes a provided search strng (that we have
    gotten from a client) and converts it into a Python expression that when
    applied in an execution environment that has various variables representing
    a message can return True or False depending on whether that message
    matches the given search criteria.
    """

    OP_ALL = "all"
    OP_AND = "and"
    OP_BEFORE = "before"
    OP_BODY = "body"
    OP_HEADER = "header"
    OP_KEYWORD = "keyword"
    OP_LARGER = "larger"
    OP_MESSAGE_SET = "message_set"
    OP_NOT = "not"
    OP_ON = "on"
    OP_OR = "or"
    OP_SENTBEFORE = "sentbefore"
    OP_SENTON = "senton"
    OP_SENTSINCE = "sentsince"
    OP_SINCE = "since"
    OP_SMALLER = "smaller"
    OP_TEXT = "text"
    OP_UID = "uid"

    VALID_OPS = (
        OP_ALL,
        OP_AND,
        OP_BEFORE,
        OP_BODY,
        OP_HEADER,
        OP_KEYWORD,
        OP_LARGER,
        OP_MESSAGE_SET,
        OP_NOT,
        OP_ON,
        OP_OR,
        OP_SENTON,
        OP_SENTBEFORE,
        OP_SENTSINCE,
        OP_SINCE,
        OP_SMALLER,
        OP_TEXT,
        OP_UID,
    )

    #########################################################################
    #
    def __init__(self, op, **kwargs):
        """This sets up the IMAPSearch object. It must be given at least the
        'search operation' keyword and a bunch of keyword arguments that are
        required for that search operation.
        """
        self.log = logging.getLogger(
            "%s.%s" % (__name__, self.__class__.__name__)
        )
        if op not in self.VALID_OPS:
            raise BadSearchOp("'%s' is not a valid search op" % op)
        self.op = op
        self.args = kwargs

    #########################################################################
    #
    def __repr__(self):
        return "IMAPSearch, operation: %s" % self.op

    #########################################################################
    #
    def __str__(self):
        result = "IMAPSearch('%s'" % self.op
        if self.op in (self.OP_AND, self.OP_OR):
            elt = []
            for search in self.args["search_key"]:
                elt.append(str(search))
            result += ", [%s]" % ", ".join(elt)
        elif self.op in (self.OP_NOT):
            result += ", search_key = %s" % self.args["search_key"]
        elif self.op in (
            self.OP_BEFORE,
            self.OP_ON,
            self.OP_SENTON,
            self.OP_SENTBEFORE,
            self.OP_SENTSINCE,
            self.OP_SINCE,
        ):
            result += ', date = "%s"' % self.args["date"]
        elif self.op in (self.OP_LARGER, self.OP_SMALLER):
            result += ", n = %d" % self.args["n"]
        elif self.op in (self.OP_TEXT, self.OP_BODY):
            result += ', string = "%s"' % self.args["string"]
        elif self.op in (self.OP_HEADER):
            result += ', header = "%s", string = "%s"' % (
                self.args["header"],
                self.args["string"],
            )
        elif self.op in (self.OP_KEYWORD):
            result += ', keyword = "%s"' % self.args["keyword"]
        return result + ")"

    ##################################################################
    #
    def match(self, ctx):
        """
        Apply this IMAPSearch instance against the message and its
        meta information contained in the 'ctx' (SearchContext)
        objects.

        We return True if it matches, False if it does not.

        Arguments:
        - `ctx`: The SearchContext that contains the message we are
          applying this search object against and its meta-information
        """
        self.ctx = ctx

        # We look up the method on ourselves that is the search op we
        # are to perform and we call that operation.
        #
        return getattr(self, "_match_%s" % self.op)()

    #########################################################################
    #########################################################################
    #
    #

    #########################################################################
    #
    def _match_keyword(self):
        """
        True if the given flag is set on this message. In our implementaton
        keywords (aka flags) are indicated by the sequences a message is in.
        """
        # Get the sequences this message is in.. remember we have to map from
        # the IMAP system flag map to the flags we use in our sequences
        # (because '\' is not valid in a sequence
        #
        # NOTE: If the keyword being looked for is '\Recent' and this message
        #       did indeed have '\Recent' set then we set the 'matched_recent'
        #       attribute on our ctx. This is so the entity calling us can make
        #       a decision on whether or not the message is removed from the
        #       recent sequence or not.
        #
        keyword = asimap.constants.flag_to_seq(self.args["keyword"])
        result = keyword in self.ctx.sequences
        # XXX Decided that I am not going to reset the \Recent flag on a search
        #     match.
        # if result and self.args['keyword'] == '\\Recent':
        #     self.ctx.matched_recent = True
        return result

    #########################################################################
    #
    def _match_header(self):
        """
        Messages that have a header with the specified field-name (as
        defined in [RFC-822]) and that contains the specified string
        in the [RFC-822] field-body.
        """
        header = self.args["header"]
        return (
            header in self.ctx.msg
            and self.ctx.msg[header].lower().find(self.args["string"]) != -1
        )

    #########################################################################
    #
    def _match_and(self):
        """
        We have a list of search keys. If any of these are false then
        the match is false.
        """
        for search_op in self.args["search_key"]:
            if not search_op.match(self.ctx):
                return False
        return True

    #########################################################################
    #
    def _match_all(self):
        """
        All messages in the mailbox; the default initial key for
        ANDing.
        """
        return True

    #########################################################################
    #
    def _match_or(self):
        """
        We have a list of search keys. If any of these are false then
        the match is false.
        """
        for search_op in self.args["search_key"]:
            if search_op.match(self.ctx):
                return True
        return False

    #########################################################################
    #
    def _match_before(self):
        """
        Messages whose internal date is earlier than the specified
        date.
        """
        return self.ctx.internal_date < self.args["date"]

    #########################################################################
    #
    def _match_body(self):
        """
        Messages that contain the specified string in the body of the
        message.
        """
        text = self.args["string"]
        for msg_part in self.ctx.msg.walk():
            if msg_part.is_multipart():
                continue
            if msg_part.get_payload(decode=True).lower().find(text) != -1:
                return True
        return False

    #########################################################################
    #
    def _match_larger(self):
        """
        Messages with an [RFC-822] size larger than the specified
        number of octets.
        """
        return os.path.getsize(self.ctx.path) > self.args["n"]

    #########################################################################
    #
    def _match_message_set(self):
        """
        Messages with message sequence numbers corresponding to the
        specified message sequence number set

        The sequence will be a list of integers and tuples. An integer
        indicates a specific sequence number. A tuple indicates a range.

        One trick, an integer may be '*' which means the last message
        sequence number in our mailbox.
        """
        for elt in self.args["msg_set"]:
            if (
                isinstance(elt, str)
                and elt == "*"
                and self.ctx.msg_number == self.id_max
            ):
                return True
            elif isinstance(elt, int) and elt == self.ctx.msg_number:
                return True
            elif isinstance(elt, tuple) and (
                self.ctx.msg_number >= elt[0] and self.ctx.msg_number <= elt[1]
            ):
                return True
        return False

    #########################################################################
    #
    def _match_not(self):
        """
        Messages that do not match the specified search key.
        """
        return not self.args["search_key"].match(self.ctx)

    #########################################################################
    #
    def _match_on(self):
        """
        Messages whose internal date is within the specified date.

        NOTE: We use the 'date' aspect of the datetime objects to
        compare 'on'ness. Ie: if they are on the same day. (rfc2060 is
        vague about this and just says what is listed above 'within
        the specific date')
        """
        return self.ctx.internal_date.date() == self.args["date"].date()

    #########################################################################
    #
    def _match_sentbefore(self):
        """
        Messages whose [RFC-822] Date: header is earlier than the
        specified date.
        """
        return "date" in self.ctx.msg and self.args[
            "date"
        ] > asimap.utils.parsedate(self.ctx.msg["date"])

    #########################################################################
    #
    def _match_senton(self):
        """
        Messages whose [RFC-822] Date: header is within the specified
        date.
        """
        return (
            "date" in self.ctx.msg
            and self.args["date"].date()
            == asimap.utils.parsedate(self.ctx.msg["date"]).date()
        )

    #########################################################################
    #
    def _match_sentsince(self):
        """
        Messages whose [RFC-822] Date: header is later than the
        specified date.
        """
        return "date" in self.ctx.msg and self.args[
            "date"
        ] < asimap.utils.parsedate(self.ctx.msg["date"])

    #########################################################################
    #
    def _match_since(self):
        """
        Messages whose internal date is within or later than the
        specified date.
        """
        return (
            self.ctx.internal_date > self.args["date"]
            or self.ctx.internal_date.date() == self.args["date"].date()
        )

    #########################################################################
    #
    def _match_smaller(self):
        """
        Messages with an [RFC-822] size larger than the specified
        number of octets.
        """
        return os.path.getsize(self.ctx.path) < self.args["n"]

    #########################################################################
    #
    def _match_text(self):
        """
        Messages that contain the specified string in the header or
        body of the message.
        """
        # Look in the headers.. and if it is not in the headers, look
        # in the body.
        #
        text = self.args["string"]
        for header in list(self.ctx.msg.values()):
            if header.lower().find(text) != -1:
                return True
        return self._match_body()

    #########################################################################
    #
    def _match_uid(self):
        """
        Messages with unique identifiers corresponding to the
        specified unique identifier set.
        """
        for elt in self.args["msg_set"]:
            if isinstance(elt, str) and elt == "*":
                if self.ctx.uid == self.ctx.uid_max:
                    return True
            elif isinstance(elt, int) and elt == self.ctx.uid:
                return True
            elif isinstance(elt, tuple) and (
                self.ctx.uid >= elt[0] and self.ctx.uid <= elt[1]
            ):
                return True
        return False
