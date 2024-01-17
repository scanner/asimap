"""
Classes and their supporting methods that represent an IMAP Search
structure.
"""
# system imports
#
import asyncio
import logging
import os.path
from datetime import datetime, timezone
from email import message_from_string
from email.message import EmailMessage
from email.policy import SMTP
from enum import StrEnum
from mailbox import MHMessage
from typing import TYPE_CHECKING, List, Optional, cast

# 3rd party imports
#
import aiofiles

# asimap imports
#
from .constants import flag_to_seq
from .exceptions import MailboxInconsistency
from .generator import get_msg_size, msg_as_string
from .utils import UID_HDR, get_uidvv_uid, parsedate

if TYPE_CHECKING:
    from .mbox import Mailbox, Sequences

logger = logging.getLogger("asimap.search")


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
        self,
        mailbox: "Mailbox",
        msg_key: int,
        msg_number: int,
        seq_max: int,
        uid_max: int,
        sequences: "Sequences",
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
        self.mailbox = mailbox
        self.msg_key = msg_key
        self.seq_max = seq_max
        self.uid_max = uid_max
        self.msg_number = msg_number
        self.mailbox_sequences = sequences
        self.path = os.path.join(mailbox.mailbox._path, str(msg_key))

        # msg & uid are looked up and set ONLY if the search actually reaches
        # in to the message. We use read only attributes to fill in these
        # values.
        #
        self._internal_date: Optional[datetime] = None
        self._msg: Optional[MHMessage] = None
        self._email_msg: Optional[EmailMessage] = None
        self._msg_size: Optional[int] = None
        self._uid_vv: Optional[int] = None
        self._uid: Optional[int] = None
        self._sequences: Optional[List[str]] = None

    ####################################################################
    #
    def __str__(self) -> str:
        return (
            f"<SearchContext, mailbox: {self.mailbox.name}, msg key: "
            f"{self.msg_key}, IMAP sequence num: {self.msg_number}, "
            f"path: {self.path}>"
        )

    ####################################################################
    #
    async def internal_date(self) -> datetime:
        if self._internal_date:
            return self._internal_date
        internal_date = datetime.fromtimestamp(
            await aiofiles.os.path.getmtime(self.path), timezone.utc
        )
        self._internal_date = internal_date
        return self._internal_date

    ##################################################################
    #
    async def msg_size(self) -> int:
        if self._msg_size:
            return self._msg_size

        # Just to make sure that the message is cached.
        #
        msg = await self.email_message()
        self._msg_size = get_msg_size(msg)  # len(msg_as_string(msg))
        return self._msg_size

    ##################################################################
    #
    async def msg(self) -> MHMessage:
        """
        The message parsed in to a MHMessage object
        """
        # XXX remove when confident
        #
        assert self.mailbox.lock.this_task_has_read_lock()

        if self._msg:
            return self._msg

        # We have not actually loaded the message yet..
        #
        self._msg = await self.mailbox.get_and_cache_msg(self.msg_key)

        if self._uid is None:
            if UID_HDR in self._msg:
                self._uid_vv, self._uid = get_uidvv_uid(self._msg[UID_HDR])
        else:
            # NOTE: This should never happen. It used to happen in the past but
            #       if our locking is correct, it should never happen again.
            #       So this is more for making sure that the code is correct
            #       now wrt locking.
            #
            if UID_HDR in self._msg:
                uid_vv, uid = get_uidvv_uid(self._msg[UID_HDR])
                if self._uid != uid or uid is None:
                    logger.error(
                        "Mailbox: %s, msg: %d, uid mismatch, was: %d, "
                        "now is: %d",
                        self.mailbox.name,
                        self.msg_key,
                        self._uid,
                        uid,
                    )
                    raise MailboxInconsistency(
                        mbox_name=self.mailbox.name, msg_key=self.msg_key
                    )
        return self._msg

    ####################################################################
    #
    async def email_message(self) -> EmailMessage:
        """
        When operating on the message itself during FETCH operations an
        EmailMessage is more modern and easier to work with, so this method
        will re-parse the message into an EmailMessage.

        NOTE: Why do we have to do this? Because we use the MHMessage's sequence
              features and that is by force an email.Message.
        """
        if self._email_msg:
            return self._email_msg

        self._email_msg = cast(
            EmailMessage,
            message_from_string(msg_as_string(await self.msg()), policy=SMTP),
        )
        return self._email_msg

    ##################################################################
    #
    async def uid(self) -> Optional[int]:
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
            self._uid_vv, self._uid = await self.mailbox.get_uid_from_msg(
                self.msg_key
            )
        return self._uid

    ##################################################################
    #
    async def uid_vv(self) -> Optional[int]:
        """
        The IMAP UID Validity Value for the mailbox
        """
        if self._uid_vv:
            return self._uid_vv
        # Use the fast method of getting the uid/uidvv.
        #
        self._uid_vv, self._uid = await self.mailbox.get_uid_from_msg(
            self.msg_key
        )
        return self._uid_vv

    ##################################################################
    #
    @property
    def sequences(self) -> List[str]:
        """
        The list of sequences that this message is in. If the message is not
        loaded we avoid loading the message object by just getting the
        sequences directly from the mailbox and computing which sequences this
        message is in.
        """
        # If the message is loaded use its sequence information.
        #
        if self._msg:
            return self._msg.get_sequences()

        # Otherwise we populate sequence information from the folder.
        if self._sequences:
            return self._sequences

        # Look at the mailbox sequences and figure out which ones this message
        # is in, if any.
        #
        self._sequences = []
        for name, key_list in self.mailbox_sequences.items():
            if self.msg_key in key_list:
                self._sequences.append(name)
        return self._sequences


########################################################################
########################################################################
#
class SearchOp(StrEnum):
    """
    Valid Search ops as an Enum
    """

    ALL = "all"
    AND = "and"
    BEFORE = "before"
    BODY = "body"
    HEADER = "header"
    KEYWORD = "keyword"
    LARGER = "larger"
    MESSAGE_SET = "message_set"
    NOT = "not"
    ON = "on"
    OR = "or"
    SENTBEFORE = "sentbefore"
    SENTON = "senton"
    SENTSINCE = "sentsince"
    SINCE = "since"
    SMALLER = "smaller"
    TEXT = "text"
    UID = "uid"


STR_TO_SEARCH_OP = {op_enum.value: op_enum for op_enum in SearchOp}


############################################################################
############################################################################
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

    #########################################################################
    #
    def __init__(self, op, **kwargs):
        """This sets up the IMAPSearch object. It must be given at least the
        'search operation' keyword and a bunch of keyword arguments that are
        required for that search operation.
        """
        if op not in STR_TO_SEARCH_OP:
            raise BadSearchOp(f"'{op}' is not a valid search op")
        self.op = STR_TO_SEARCH_OP[op]
        self.args = kwargs
        self.ctx: SearchContext

    #########################################################################
    #
    def __repr__(self):
        return f"IMAPSearch, operation: {self.op.value}"

    #########################################################################
    #
    def __str__(self):
        result = [f"IMAPSearch('{self.op.value}'"]
        match self.op:
            case SearchOp.AND | SearchOp.OR:
                elt: List[str] = []
                for search in self.args["search_key"]:
                    elt.append(str(search))
                result.append(f", [{', '.join(elt)}]")
            case SearchOp.NOT:
                result.append(f", search_key = {self.args['search_key']}")
            case SearchOp.BEFORE | SearchOp.ON | SearchOp.SENTON | SearchOp.SENTBEFORE | SearchOp.SENTSINCE | SearchOp.SINCE:
                result.append(f', date = "{self.args["date"]}"')
            case SearchOp.LARGER | SearchOp.SMALLER:
                result.append(f", n = {self.args['n']}")
            case SearchOp.TEXT | SearchOp.BODY:
                result.append(f', string = "{self.args["string"]}"')
            case SearchOp.MESSAGE_SET:
                result.append(f', msg_set = {self.args["msg_set"]}')
            case SearchOp.HEADER:
                result.append(
                    f', header = "{self.args["header"]}", '
                    f'string = "{self.args["string"]}"'
                )
            case SearchOp.KEYWORD:
                result.append(f', keyword = "{self.args["keyword"]}"')
        result.append(")")
        return "".join(result)

    ##################################################################
    #
    async def match(self, ctx: SearchContext) -> bool:
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
        return await getattr(self, f"_match_{self.op.value}")()

    #########################################################################
    #########################################################################
    #
    #

    #########################################################################
    #
    async def _match_keyword(self) -> bool:
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
        keyword = flag_to_seq(self.args["keyword"])
        result = keyword in self.ctx.sequences
        return result

    #########################################################################
    #
    async def _match_header(self) -> bool:
        """
        Messages that have a header with the specified field-name (as
        defined in [RFC-822]) and that contains the specified string
        in the [RFC-822] field-body.
        """
        header = self.args["header"]
        msg = await self.ctx.msg()
        return (
            header in msg
            and msg[header].lower().find(self.args["string"]) != -1
        )

    #########################################################################
    #
    async def _match_and(self) -> bool:
        """
        We have a list of search keys. All of them must be True.
        """
        tasks = []
        async with asyncio.TaskGroup() as tg:
            for search_op in self.args["search_key"]:
                tasks.append(tg.create_task(search_op.match(self.ctx)))
        if all(x.result() for x in tasks):
            return True
        return False

    #########################################################################
    #
    async def _match_all(self) -> bool:
        """
        All messages in the mailbox; the default initial key for
        ANDing.
        """
        return True

    #########################################################################
    #
    async def _match_or(self) -> bool:
        """
        We have a list of search keys. If any of these are true then
        the match is true.
        """
        tasks = []
        async with asyncio.TaskGroup() as tg:
            for search_op in self.args["search_key"]:
                tasks.append(tg.create_task(search_op.match(self.ctx)))
        if any(x.result() for x in tasks):
            return True
        return False

    #########################################################################
    #
    async def _match_before(self) -> bool:
        """
        Messages whose internal date is earlier than the specified
        date.
        """
        internal_date = (await self.ctx.internal_date()).date()
        return internal_date < self.args["date"]

    #########################################################################
    #
    async def _match_body(self) -> bool:
        """
        Messages that contain the specified string in the body of the
        message.
        """
        text = self.args["string"]
        msg = await self.ctx.msg()
        for msg_part in msg.walk():
            if msg_part.is_multipart():
                continue
            if text in msg_as_string(msg, headers=False).lower():
                return True
        return False

    #########################################################################
    #
    async def _match_larger(self) -> bool:
        """
        Messages with an [RFC-822] size larger than the specified
        number of octets.
        """
        size = await self.ctx.msg_size()
        return size > self.args["n"]

    #########################################################################
    #
    async def _match_message_set(self) -> bool:
        """
        Messages with message sequence numbers corresponding to the
        specified message sequence number set

        The sequence will be a list of integers and tuples. An integer
        indicates a specific sequence number. A tuple indicates a range.

        One trick, an integer may be '*' which means the last message
        sequence number in our mailbox.
        """
        msg_number = self.ctx.msg_number
        for elt in self.args["msg_set"]:
            if isinstance(elt, str) and elt == "*":
                if msg_number == self.ctx.seq_max:
                    return True
            elif isinstance(elt, int):
                if elt == msg_number:
                    return True
            elif isinstance(elt, tuple):
                if isinstance(elt[1], str) and elt[1] == "*":
                    elt = (elt[0], self.ctx.seq_max)
                if msg_number >= elt[0] and msg_number <= elt[1]:
                    return True
        return False

    #########################################################################
    #
    async def _match_not(self) -> bool:
        """
        Messages that do not match the specified search key.
        """
        return not await self.args["search_key"].match(self.ctx)

    #########################################################################
    #
    async def _match_on(self) -> bool:
        """
        Messages whose internal date is within the specified date.

        NOTE: We use the 'date' aspect of the datetime objects to
        compare 'on'ness. Ie: if they are on the same day. (rfc2060 is
        vague about this and just says what is listed above 'within
        the specific date')
        """
        internal_date = (await self.ctx.internal_date()).date()
        return internal_date == self.args["date"]

    #########################################################################
    #
    async def _match_sentbefore(self) -> bool:
        """
        Messages whose [RFC-822] Date: header is earlier than the
        specified date.
        """
        msg = await self.ctx.msg()
        if "date" not in msg:
            return False
        msg_date = parsedate(msg["date"]).date()
        return msg_date < self.args["date"]

    #########################################################################
    #
    async def _match_senton(self) -> bool:
        """
        Messages whose [RFC-822] Date: header is within the specified
        date.
        """
        msg = await self.ctx.msg()
        if "date" not in msg:
            return False
        msg_date = parsedate(msg["date"]).date()
        return msg_date == self.args["date"]

    #########################################################################
    #
    async def _match_sentsince(self) -> bool:
        """
        Messages whose [RFC-822] Date: header is later than the
        specified date.
        """
        msg = await self.ctx.msg()
        if "date" not in msg:
            return False
        msg_date = parsedate(msg["date"]).date()
        return msg_date >= self.args["date"]

    #########################################################################
    #
    async def _match_since(self) -> bool:
        """
        Messages whose internal date is within or later than the
        specified date.
        """
        internal_date = (await self.ctx.internal_date()).date()
        return internal_date >= self.args["date"]

    #########################################################################
    #
    async def _match_smaller(self) -> bool:
        """
        Messages with an [RFC-822] size larger than the specified
        number of octets.
        """
        size = await self.ctx.msg_size()
        return size < self.args["n"]

    #########################################################################
    #
    async def _match_text(self) -> bool:
        """
        Messages that contain the specified string in the header
        (including MIME header fields) or body of the message.  Servers
        are allowed to implement flexible matching for this search key,
        for example, matching "swim" to both "swam" and "swum" in English
        language text or only performing full-word matching (where "swim"
        will not match "swimming").

        NOTE: We do not do such fancy text searching.
        """
        # Look in the headers.. and if it is not in the headers, look
        # in the body.
        #
        text = self.args["string"]
        msg = await self.ctx.msg()
        msg_text = msg_as_string(msg, headers=True).lower()
        if text in msg_text:
            return True
        return False

    #########################################################################
    #
    async def _match_uid(self) -> bool:
        """
        Messages with unique identifiers corresponding to the
        specified unique identifier set.
        """
        uid = await self.ctx.uid()
        for elt in self.args["msg_set"]:
            if isinstance(elt, str) and elt == "*":
                if uid == self.ctx.uid_max:
                    return True
            elif isinstance(elt, int):
                if elt == uid:
                    return True
            elif isinstance(elt, tuple):
                if isinstance(elt[1], str) and elt[1] == "*":
                    elt = (elt[0], self.ctx.uid_max)
                if uid >= elt[0] and uid <= elt[1]:
                    return True
        return False
