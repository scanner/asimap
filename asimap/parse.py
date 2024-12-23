"""
This module contains the classes and structures that are used to parse an
IMAP message received from an IMAP client in to structures and invocations of
commands in other parts of the server.
"""

# system imports
#
import asyncio
import email.policy
import logging
import os.path
import re
from contextlib import asynccontextmanager
from datetime import date
from email import message_from_string
from email.message import EmailMessage
from enum import Enum, StrEnum
from typing import (
    TYPE_CHECKING,
    Callable,
    List,
    Optional,
    Set,
    Tuple,
    Union,
    cast,
)

from .fetch import STR_TO_FETCH_OP, FetchAtt, FetchOp
from .search import IMAPSearch

# asimapd imports
#
from .utils import MsgSet, parsedate

if TYPE_CHECKING:
    from .mbox import Mailbox


logger = logging.getLogger("asimap.parse")


#######################################################################
#
class BadCommand(Exception):
    def __init__(self, value="bad command"):
        self.value = value

    def __str__(self):
        return f"BadCommand: {self.value}"


#######################################################################
#
class NoMatch(BadCommand):
    def __init__(self, value="no match"):
        self.value = value

    def __str__(self):
        return f"NoMatch: {self.value}"


#######################################################################
#
class UnknownCommand(BadCommand):
    def __init__(self, value="unknown command"):
        self.value = value

    def __str__(self):
        return f"UnknownCommand: {self.value}"


#######################################################################
#
class BadLiteral(BadCommand):
    def __init__(self, value="bad literal"):
        self.value = value

    def __str__(self):
        return f"BadLiteral: {self.value}"


#######################################################################
#
class BadSyntax(BadCommand):
    def __init__(self, value="bad syntax"):
        self.value = value

    def __str__(self):
        return f"BadSyntax: {self.value}"


#######################################################################
#
class UnknownSearchKey(BadCommand):
    def __init__(self, value="unknown search key"):
        self.value = value

    def __str__(self):
        return f"UnknownSearchKey: {self.value}"


#######################################################################
#######################################################################
#
# Constants used by IMAPClientCommand
#
class StoreAction(Enum):
    REPLACE_FLAGS = 0
    ADD_FLAGS = 1
    REMOVE_FLAGS = 2


# For debugging messages.. mapping the flags back to strings.
#
flag_to_str = {
    StoreAction.REPLACE_FLAGS: "FLAGS",
    StoreAction.ADD_FLAGS: "+FLAGS",
    StoreAction.REMOVE_FLAGS: "-FLAGS",
}


#######################################################################
#
class IMAPCommand(StrEnum):
    APPEND = "append"
    AUTHENTICATE = "authenticate"
    CAPABILITY = "capability"
    CHECK = "check"
    CLOSE = "close"
    COPY = "copy"
    CREATE = "create"
    DELETE = "delete"
    EXAMINE = "examine"
    EXPUNGE = "expunge"
    FETCH = "fetch"
    ID = "id"
    IDLE = "idle"
    LIST = "list"
    LOGIN = "login"
    LOGOUT = "logout"
    LSUB = "lsub"
    NAMESPACE = "namespace"
    NOOP = "noop"
    RENAME = "rename"
    SEARCH = "search"
    SELECT = "select"
    STATUS = "status"
    STORE = "store"
    SUBSCRIBE = "subscribe"
    UID = "uid"
    UNSELECT = "unselect"
    UNSUBSCRIBE = "unsubscribe"


#######################################################################
#
class StatusAtt(StrEnum):
    MESSAGES = "messages"
    RECENT = "recent"
    UIDNEXT = "uidnext"
    UIDVALIDITY = "uidvalidity"
    UNSEEN = "unseen"


#######################################################################
#
# Attributes of a fetch command. Note that the order is important. We need to
# match the longest strings with the common prefix first to insure that we
# fully match the proper keyword (ie: if we look for 'rfc822' first we will
# incorrectly not identify a 'rfc822.text')
#
class ParseFetchAtt(StrEnum):
    ENVELOPE = "envelope"
    FLAGS = "flags"
    INTERNALDATE = "internaldate"
    RFC822_HEADER = "rfc822.header"
    RFC822_SIZE = "rfc822.size"
    RFC822_TEXT = "rfc822.text"
    RFC822 = "rfc822"
    UID = "uid"
    BODYSTRUCTURE = "bodystructure"
    BODY_PEEK = "body.peek"
    BODY = "body"


# The conflicting commands. These commands are known to change the mailbox
# in a significant way.
#
# We know that a fetch with peek = False can change the mailbox (the flags)
# but we are going to let that happen for now.
#
# NOTE: Conflicting commands may also do mailbox resync's,which they can only
#       do since they are the sole task executing against a mailbox.
#       APPEND, and RENAME take advantage of this.
#
# CHECK - forces a resync and commit to db
# CLOSE - may cause an expunge
# DELETE - mailbox going away, so obviously must be by itslf.
# EXPUNGE - may change sequences and message lists
# RENAME - like mailbox going away, but worse.
#
CONFLICTING_COMMANDS = (
    IMAPCommand.APPEND,
    IMAPCommand.CHECK,
    IMAPCommand.CLOSE,
    IMAPCommand.DELETE,
    IMAPCommand.EXPUNGE,
    IMAPCommand.RENAME,
)


# This is the list of flags we know specifically about.
system_flags = [
    r"\answered",
    r"\flagged",
    r"\deleted",
    r"\seen",
    r"\draft",
    r"\recent",
]

# The list of commands that can be called via 'UID'
#
uid_commands = ("copy", "fetch", "search", "store", "expunge")

_month = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# Lots of regular expressions.

# search key token - search keys are one of a set of words - just alpha
# characters, no specials, no numerics.
#
_search_atom = r"[a-zA-Z]+"
_search_atom_re = re.compile(_search_atom)

# fetch att token - fetch atts are one of a set of words - just alpha
# characters and a dot, no other specials, no numerics.
#
_fetch_att_atom = r"[a-zA-Z82\.]+"
_fetch_att_atom_re = re.compile(_fetch_att_atom)

# a positive integer.
#
_number = r"\d+"
_number_re = re.compile(_number)

# a message sequence number - a non-zero integer or "*"
#
_msg_seq_num = r"(\d+|\*)"
_msg_seq_num_re = re.compile(_msg_seq_num)

# a message set - some combination of message squence numbers separated by
# ',' or ':'
#
_msg_set_pair = r"^(\d+|\*):(\d+|\*)$"
_msg_set_pair_re = re.compile(_msg_set_pair)
_msg_set = r"[\d,:*]+"
_msg_set_re = re.compile(_msg_set)
# An atom is one or more characters that is not an atom special
# ie: "(" / ")" / "{" / SPACE / CTL / list_wildcards / quoted_specials
#
_atom = r'[^\(\)\{\} \000-\037\177%\*"\\]+'
_atom_re = re.compile(_atom)

# There are some pre-defined fetch attribute macros we need to look for
# before we try to parse individual fetch attributes. This re is used to
# find those.
#
_fetch_att_macros = r"(all)|(full)|(fast)"
_fetch_att_macros_re = re.compile(_fetch_att_macros, flags=re.IGNORECASE)

# A list_atom is just like an atom, except we allow list_wildcards ('*'
# and '%')
#
_list_atom = r'[^\(\)\{\} \000-\037\177"\\]+'
_list_atom_re = re.compile(_list_atom)

# A simple "+" or "-" choice.
#
_plus_or_minus = r"[-\+]"
_plus_or_minus_re = re.compile(_plus_or_minus)

# A tag is an atom, except '+' is not allowed also.
#
_tag = r'[^\+\(\)\{\} \000-\037\177%\*"\\]+'
_tag_re = re.compile(_tag)

# A quoted string is any text char except quoted specials, unless they
# are quoted (those are: " and \)
#
_quoted = r'"(([^\015\012\\"]|\\["\\])*)"'
_quoted_re = re.compile(_quoted)

# A literal string has a 'literal prefix' which is of the from {\d}?+CRLF.
# The "+" indicates a non-synchronizing literal
#
_lit_ref = r"\{(\d+)\+?\}\015\012"
_lit_ref_re = re.compile(_lit_ref)

# Date regular expressions
#
_date_day_fixed = r"[ \d]\d"
_date_year = r"\d\d\d\d"
_date_month = (
    r"(Jan)|(Feb)|(Mar)|(Apr)|(May)|(Jun)|(Jul)|(Aug)|(Sep)|"
    + r"(Oct)|(Nov)|(Dec)"
)
_time = r"\d\d:\d\d:\d\d"
_zone = r"[-+]\d\d\d\d"
_date_time = (
    r'"(?P<day>[ \d]\d)-(?P<month>(Jan)|(Feb)|(Mar)|(Apr)|(May)|'
    + r"(Jun)|(Jul)|(Aug)|(Sep)|(Oct)|(Nov)|(Dec))-"
    + r"(?P<year>\d\d\d\d) (?P<hour>\d\d):(?P<sec>\d\d):"
    + r'(?P<min>\d\d) (?P<tz_hr>[-+]\d\d)(?P<tz_min>\d\d)"'
)
_date = (
    r'(")?(?P<day>\d?\d)-(?P<month>(Jan)|(Feb)|(Mar)|(Apr)|(May)|'
    + r"(Jun)|(Jul)|(Aug)|(Sep)|(Oct)|(Nov)|(Dec))-"
    + r'(?P<year>\d\d\d\d)(?(1)")'
)
_date_re = re.compile(_date, re.I)
_date_time_re = re.compile(_date_time, re.I)
_date_day_fixed_re = re.compile(_date_day_fixed)
_date_year_re = re.compile(_date_year)
_date_month_re = re.compile(_date_month, re.I)
_time_re = re.compile(_time)
_zone_re = re.compile(_zone)

#
# Done constants
#
#######################################################################
#######################################################################


####################################################################
#
def msg_set_to_str(msg_set: Optional[MsgSet]) -> str:
    """
    Turn a message set into the equivalent IMAP protocol string representation
    """
    if msg_set is None:
        return ""

    return ",".join(
        [
            (f"{x[0]}:{x[1]}" if isinstance(x, tuple) else str(x))
            for x in msg_set
        ]
    )


############################################################################
#
class IMAPClientCommand:
    """This is an IMAP Client Command parser. Given a complete IMAP command it
    will parse in to a structure that can be easily processed by the rest of
    this server.

    It breaks the command in to its components such that the object that is
    created gives easy access via attributes to various parts of the command
    from the IMAP client.
    """

    #######################################################################
    #
    # XXX the IMAP messages should be in bytes, so we should accept a ByteStr,
    #     and if it is a string encode it to bytes (no reason to use anything
    #     besides utf-8?)
    #
    def __init__(self, imap_command: str):
        """
        Create the IMAPClientCommand object. This does NOT parse the string we
        were given, though. You need to call the 'parse()' method for that.
        """
        self.input = imap_command
        self.uid_command: bool = False
        self.tag: Optional[str] = None
        self.command: Optional[str] = None
        self.msg_set: MsgSet = []
        self.fetch_atts: List[FetchAtt] = []
        self.message: EmailMessage

        # If we are doing a `STORE.SILENT` command then `silent` is True
        #
        self.silent = False

        # `fetch_peek` is set to False if this is a FETCH command and any of
        # the fetch atts are BODY (not BODY.PEEK). This helpful information
        # when trying to determine if IMAP Commands would conflict when
        # running.
        #
        self.fetch_peek = True

        # NOTE: This attribute is set by the mbox's management task before the
        #       task is allowed to run (since it needs the context of a mbox to
        #       know the max seq and how to map uid's to IMAP message sequence
        #       numbers)
        #
        self.msg_set_as_set: Optional[Set[int]] = None

        # If the IMAP Command is currently operating under an asyncio.Timeout
        # context manager, that context manager is set here so that when a
        # command is being processed, if it knows it is going to run longer it
        # can reschedule the timeout..
        #
        # The timeout is primarily used to guard against deadlocks or some
        # process taking far longer than expected. (we should not have any,
        # this is to be safe.)
        #
        self.timeout_cm: Optional[asyncio.Timeout] = None

        # When the task executing this IMAPClientCommand wants to begin it must
        # wait for the 'ready' event to be set. This is the management task for
        # the mailbox this command is operating on telling this task it has
        # permission to go ahead.
        #
        self.ready = asyncio.Event()

        # when the task executing this IMAPClientCommand finished it sets
        # `completed` to True so that the mbox management task knows that this
        # command has finished.
        #
        self.completed = False

    ##################################################################
    #
    @asynccontextmanager
    async def ready_and_okay(self, mbox: "Mailbox"):
        """
        Awaits the `ready` event. No matter what happens, we set the
        command to be completed before exiting.
        """
        try:
            mbox.task_queue.put_nowait(self)
            await self.ready.wait()
            if mbox.deleted:
                from .mbox import NoSuchMailbox

                raise NoSuchMailbox(
                    f"Mailbox '{mbox.name}' has been deleted or shutdown"
                )
            yield
        finally:
            self.completed = True
            if mbox.task_queue:
                mbox.task_queue.task_done()

    ##################################################################
    #
    def parse(self):
        """
        Do the actual parsing of the IMAP command. This is separated from the
        init method so that if we hit a parsing exception the actual object
        gets created at least and potentially has self.tag set.
        """
        self._parse()
        return self

    ####################################################################
    #
    def qstr(self):
        """
        For debugging output this gives a short string representing this
        command. Just the tag and command string (upper cased)
        """
        tag = self.tag if self.tag else "*"
        command = "none" if self.command is None else self.command
        if self.uid_command:
            return f"{tag} UID {command.upper()}"

        return f"{tag} {command.upper()}"

    #######################################################################
    #
    def __str__(self):
        result = []
        if self.tag:
            result.append(self.tag)
        else:
            result.append("*")
        if self.command:
            if self.uid_command:
                result.append("UID")
            result.append(self.command.upper())
            match self.command:
                case (
                    IMAPCommand.AUTHENTICATE
                    | IMAPCommand.CAPABILITY
                    | IMAPCommand.CHECK
                    | IMAPCommand.CLOSE
                    | IMAPCommand.IDLE
                    | IMAPCommand.LOGOUT
                    | IMAPCommand.NAMESPACE
                    | IMAPCommand.NOOP
                    | IMAPCommand.UNSELECT
                ):
                    # These command have no parameters
                    pass
                case IMAPCommand.EXPUNGE:
                    if self.uid_command:
                        result.append(msg_set_to_str(self.msg_set))
                case IMAPCommand.COPY:
                    result.append(msg_set_to_str(self.msg_set))
                    result.append(self.mailbox_name)
                case IMAPCommand.FETCH:
                    result.append(msg_set_to_str(self.msg_set))
                    fetch_atts = " ".join(
                        x.dbg(show_peek=True) for x in self.fetch_atts
                    )
                    result.append(f"({fetch_atts})")
                case IMAPCommand.STATUS:
                    result.append(self.mailbox_name)
                    result.append(f"({' '.join(self.status_att_list)})")
                case (
                    IMAPCommand.APPEND
                    | IMAPCommand.CREATE
                    | IMAPCommand.DELETE
                    | IMAPCommand.EXAMINE
                    | IMAPCommand.SELECT
                    | IMAPCommand.SUBSCRIBE
                    | IMAPCommand.UNSUBSCRIBE
                ):
                    result.append(self.mailbox_name)
                case IMAPCommand.LIST | IMAPCommand.LSUB:
                    result.append(f'"{self.mailbox_name}"')
                    result.append(f'"{self.list_mailbox}"')
                case IMAPCommand.SEARCH:
                    result.append(str(self.search_key))
                case IMAPCommand.STORE:
                    result.append(msg_set_to_str(self.msg_set))
                    if self.silent:
                        result.append(
                            f"{flag_to_str[self.store_action]}.SILENT"
                        )
                    else:
                        result.append(flag_to_str[self.store_action])
                    result.append(f"({','.join(self.flag_list)})")
                case IMAPCommand.LOGIN:
                    result.append(self.user_name)
                case IMAPCommand.ID:
                    id_r = ", ".join(
                        f"{x}:'{y}'" for x, y in self.id_dict.items()
                    )
                    result.append(f"({id_r})")
                case IMAPCommand.RENAME:
                    result.append(f'"{self.mailbox_src_name}"')
                    result.append(f'"{self.mailbox_dst_name}"')
                case _:
                    raise UnknownCommand(value=self.command)

        return " ".join(result)

    #######################################################################
    #
    def __repr__(self):
        result = "<IMAPClientCommand "
        if self.tag is not None:
            result += "tag: '%s'" % self.tag
        if self.command is not None:
            result += " command: '%s'" % self.command
            if self.command == "fetch":
                result += ": " + " ".join([str(x) for x in self.fetch_atts])
        result += ">"
        return result

    #######################################################################
    #######################################################################
    #
    # The following functions are internal to this class. They are all the
    # bits and pieces to parse out our input stream.
    #
    # The command parser functions have the notable attribute that as they
    # parse the message from the imap client they set attributes in the
    # IMAPClientCommand object instance that are the parsed out bits of the
    # command from the client. The whole purpose is to parse a message from an
    # IMAP client and present it in a fashion that easily understood by the
    # rest of the server.
    #

    #######################################################################
    #
    def _parse(self):
        """Parse a command from the input stream. This is the highest level
        node in our parse tree. It all starts here. This is what calls all of
        the other parsing routines.

        A command is:

        tag SPACE command command_arguments* CRLF
        """
        # We must always begin with a tag. Pull it off. If this fails it will
        # raise an exception that is caught by our caller.
        #
        self.tag = self._p_re(
            _tag_re,
            syntax_error="missing expected tag that prefixes a command",
        )
        self._p_simple_string(" ", syntax_error="expected ' ' after tag")
        cmd = self._p_re(
            _atom_re, syntax_error="expected an atom for " "the command name"
        )
        if not cmd:
            raise BadCommand(value="No command")
        self.command = cmd.lower()
        self._parse_command(self.command)

    ####################################################################
    #
    def _parse_command(self, command: str) -> None:
        """
        Parse the valid commands and fill in attributes on self based on
        that parsing.
        """

        match command:
            # These commands require no further parsing.
            #
            case (
                IMAPCommand.CAPABILITY
                | IMAPCommand.NOOP
                | IMAPCommand.NAMESPACE
                | IMAPCommand.IDLE
                | IMAPCommand.LOGOUT
                | IMAPCommand.CHECK
                | IMAPCommand.CLOSE
                | IMAPCommand.UNSELECT
            ):
                pass
            case IMAPCommand.EXPUNGE:
                if self.uid_command:
                    self._p_simple_string(" ")
                    self.msg_set = self._p_msg_set()
            case IMAPCommand.AUTHENTICATE:
                self._p_simple_string(" ")
                self.auth_mechanism_name = self._p_re(_atom_re)
            case IMAPCommand.LOGIN:
                self._p_simple_string(" ")
                self.user_name = self._p_astring()
                self._p_simple_string(" ")
                self.password = self._p_astring()
            case (
                IMAPCommand.SELECT
                | IMAPCommand.EXAMINE
                | IMAPCommand.CREATE
                | IMAPCommand.DELETE
                | IMAPCommand.SUBSCRIBE
                | IMAPCommand.UNSUBSCRIBE
            ):
                self._p_simple_string(" ")
                self.mailbox_name = self._p_mailbox()
            case IMAPCommand.RENAME:
                self._p_simple_string(" ")
                self.mailbox_src_name = self._p_mailbox()
                self._p_simple_string(" ")
                self.mailbox_dst_name = self._p_mailbox()
            case IMAPCommand.LIST | IMAPCommand.LSUB:
                self._p_simple_string(" ")
                self.mailbox_name = self._p_mailbox()
                self._p_simple_string(" ")
                self.list_mailbox = self._p_list_mailbox()
            case IMAPCommand.STATUS:
                self._p_simple_string(" ")
                self.mailbox_name = self._p_mailbox()
                self._p_simple_string(" ")
                self.status_att_list = self._p_paren_list_of(self._p_status_att)
            case IMAPCommand.ID:
                self._p_id()
            case IMAPCommand.APPEND:
                self._p_append()
            case IMAPCommand.SEARCH:
                self._p_search()
            case IMAPCommand.FETCH:
                self._p_simple_string(" ")
                self.msg_set = self._p_msg_set()
                self._p_simple_string(" ")
                self.fetch_atts = self._p_fetch_atts()
                # If any of the fetch_atts are BODY (no peek) then set
                # self.fetch_peek = False.
                #
                self.fetch_peek = not any(
                    x.attribute == FetchOp.BODY and not x.peek
                    for x in self.fetch_atts
                )

            case IMAPCommand.STORE:
                self._p_store()
            case IMAPCommand.COPY:
                self._p_simple_string(" ")
                self.msg_set = self._p_msg_set()
                self._p_simple_string(" ")
                self.mailbox_name = self._p_mailbox()
            case IMAPCommand.UID:
                self._p_uid()
            case _:
                raise UnknownCommand(value=self.command)

    #######################################################################
    #
    def _p_id(self):
        """id ::= "ID" SPACE id_params_list

        id_params_list ::= "(" #(string SPACE nstring) ")" / nil
            ;; list of field value pairs

        """
        self._p_simple_string(" ")
        self.id_dict = {}

        # We either get nil or a list of key/value pairs.
        #
        if self._p_simple_string("nil", silent=True):
            return
        self._p_simple_string(
            "(",
            swallow=False,
            syntax_error="expected " "a parenthesized list of key/value pairs",
        )
        kv_pairs = self._p_paren_list_of(self._p_string_nstring_pairs)
        for k, v in kv_pairs:
            self.id_dict[k] = v
        return

    #######################################################################
    #
    def _p_append(self):
        """append ::= "APPEND" SPACE mailbox [SPACE flag_list]
                      [SPACE date_time] SPACE literal

        the flag_list and the date_time are optional parameters.
        If not specified they will be set but be none in this object"""

        self._p_simple_string(" ", syntax_error="expected ' ' after APPEND")
        self.mailbox_name = self._p_mailbox()
        self._p_simple_string(" ", syntax_error="expected ' ' after mailbox")

        # Now we have two optional arguments and a last required argument. How
        # do we know what we have next? Well, the first optional argument is a
        # flag list. This means it will begin with '('. The second optional
        # argument is a date_time, which must begin with a '"'. The final
        # argument is a string literal which means it begins with '{'. Since
        # the rfc states that they are specified in this order we can basically
        # check for each one to decide what to do next.
        #

        # Is this a list? If so, parse our list of flags.
        #
        self.flag_list = []
        if self._p_simple_string("(", silent=True, swallow=False):
            self.flag_list = self._p_paren_list_of(self._p_flag)
            self._p_simple_string(
                " ", syntax_error="expected ' ' after flag " "list"
            )

        # The next thing is either a date_time or a string literal. If the next
        # character is a '"' then it must be a date time.
        #
        self.date_time = None
        if self._p_simple_string('"', silent=True, swallow=False):
            self.date_time = self._p_date_time()
            self._p_simple_string(
                " ", syntax_error="expected ' ' after " "rfc822 date-time"
            )

        # and the last thing _must_ be a string literal, and it is an email
        # message so we are going to cut out the middle man and just parse it
        # as a message structure right away (I hope this works in all cases,
        # even with draft messages.)
        #
        self.message = cast(
            EmailMessage,
            message_from_string(self._p_string(), policy=email.policy.SMTP),
        )
        # XXX Remove this after we are sure our MHMessage -> EmailMessage
        #     conversion.
        # self.message = mailbox.MHMessage(self._p_string())
        return

    #######################################################################
    #
    def _p_search(self):
        """search ::= "SEARCH" SPACE ["CHARSET" SPACE astring SPACE]
                   1#search_key
        [CHARSET] MUST be registered with IANA

        The "search" command has what amounts to its own little grammar.
        We parse out the initial part of the message and then we pass the
        last bit ("1#search_key") in to a sub-parsing routine. We expect
        back a list of mhimap.IMAPSearch objects.
        """
        self._p_simple_string(" ")

        # If the next token is 'CHARSET' then we need to pull aside the
        # CHARSET. If not, we default the charset to 'us-ascii'
        #
        if self._p_simple_string("charset", silent=True):
            self._p_simple_string(" ")
            self.charset = self._p_astring().lower()
            self._p_simple_string(" ")
        else:
            self.charset = "us-ascii"

        # If we get back a list search key's then this is really a bunch of
        # search keys with AND's between them.
        #
        self.search_key = IMAPSearch(
            "and", search_key=self._p_list_of(self._p_search_key)
        )

    #######################################################################
    #
    def _p_store(self):
        """store ::= "STORE" SPACE set SPACE store_att_flags"""
        self._p_simple_string(" ")
        self.msg_set = self._p_msg_set()
        self._p_simple_string(" ")

        plus_or_minus = self._p_re(_plus_or_minus_re, silent=True)
        if plus_or_minus == "-":
            self.store_action = StoreAction.REMOVE_FLAGS
        elif plus_or_minus == "+":
            self.store_action = StoreAction.ADD_FLAGS
        else:
            self.store_action = StoreAction.REPLACE_FLAGS

        self._p_simple_string("flags")
        if self._p_simple_string(".silent", silent=True):
            self.silent = True
        else:
            self.silent = False
        self._p_simple_string(" ")

        if self._p_simple_string("(", silent=True, swallow=False):
            self.flag_list = self._p_paren_list_of(self._p_flag)
        else:
            self.flag_list = [self._p_flag()]

    #######################################################################
    #
    def _p_uid(self):
        """uid ::= "UID" SPACE (copy / fetch / search / store / expunge)

        a "UID" command is basically a copy, fetch, search, or store command.
        It is parsed the same way for each of those possibilities. The main
        difference in how the data is interpreted. For copy, fetch, store
        the "message set" is interpreted as being UIDs, not message sequence
        numbers.

        For "search" not only is the message parsed the same way, but it is
        interpreted the same way. The difference is that the result passed back
        to the client is in UIDs, not message sequence numbers.

        NOTE: 'UID EXPUNGE' is part of UIDPLUS (rfc4315) support
        """
        self.uid_command = True
        self._p_simple_string(" ")

        # Basically we re-interpret a UID command as a regular command. The
        # 'uid_command' flag will tell the interpreter how to interpret the
        # command.
        #
        command = self._p_re(_atom_re)
        if not command:
            raise BadCommand("UID command does not list command to run")
        self.command = command.lower()
        if self.command not in uid_commands:
            raise BadSyntax(
                "%s is not a valid UID command: %s"
                % (command, str(uid_commands))
            )
        self._parse_command(self.command)

    #
    # Here ends the list of supported commands
    #
    #######################################################################
    #######################################################################
    #
    # Here are all the utility parsing functions to parse various bits of the
    # IMAP message (and these are all called by the above functions.
    #
    # The one significant difference of these methods is that they return what
    # they have been asked to parse, or raise an exception. They swallow input
    # upon success.
    #

    #######################################################################
    #
    def _p_list_of(self, func):
        """This is similar to p_paren_list_of() except that it has a slightly
        more difficult job. The elements are separated by a SPACE, yes. But
        there is no paren beginning or ending this list.

        The way it will work is that we will parse elements until the next
        element to parse is NOT a SPACE (or we encounter a failure along the
        way.)

        We return a list of whatever the passed in function returns to us.

        The list MUST have at least one element.
        """
        result = []

        # We loop, pulling off parseable expressions. If after a parse returns
        # successfully and the next character is a space, do our loop over
        # again. If the next character is NOT a space, then we have parsed all
        # we can for this list and we return what we have.
        #
        while True:
            # We should have a token next. If this fails it will raise an
            # exception all the way up
            #
            result.append(func())

            # The next element may be a ' '. If it is then we continue our
            # loop. If it is not then we break out of our loop - we are done
            # processing elements in this list.
            #
            if self._p_simple_string(" ", silent=True, swallow=False) is None:
                # Nope.. next character was not a space! we are done.
                break

            # The next character is a space. Swallow it and continue on with
            # our loop.
            #
            self.input = self.input[1:]

        # all done
        #
        return result

    #######################################################################
    #
    def _p_paren_list_of(self, func: Callable) -> List[str]:
        """This function does not parse a specific type of singleton
        element. It is specifically for parsing lists of elements that follow a
        specific convention.

        This function has no backout facility currently. If we encounter an
        error in processing it is passed up and some of the input may have been
        consumed.

        We are called with a function that is used to consume tokens. We expect
        the input stream to be '('<list of tokens>')' where the list of tokens
        is separated by a single spae. There are no spaces between the
        parentheses and the first and last token.

        We return a list of whatever the passed in function returns to us.
        """

        result: List[str] = []
        self._p_simple_string(
            "(",
            syntax_error="expected a '(' beginning " "a parenthesized list",
        )
        # If we hit a ')' then it was an empty list.
        #
        if self._p_simple_string(")", silent=True) is not None:
            return result

        # Go through the list looking for tokens
        #
        while True:
            # We should have a token next. If this fails it will raise an
            # exception all the way up
            #
            result.append(func())

            # if the next element is a ')' then we have hit the end of our
            # list.
            #
            if self._p_simple_string(")", silent=True) is not None:
                break

            # It was not a ')' it MUST be a ' ' then.
            #
            self._p_simple_string(" ")

        # all done
        #
        return result

    #######################################################################
    #
    def _p_fetch_atts(self) -> List[FetchAtt]:
        """We have either a single fetch attribute or a list of fetch
        attributes. We know which it will be, because it will be a
        parenthesized list so if the next character on our input stream is a
        "(" we do a list of.. otherwise we do a single element.

        In any case we return a list because that is how we are going to store
        this in our IMAPParse object so that the entity using this can just
        loop over the elements of the list and call each fetch object extractor
        on each message being fetched.

        We also grok fetch atts of: "ALL", "FULL", and "FAST" which are macros
        for: (FLAGS INTERNALDATE RFC822.SIZE ENVELOPE), (FLAGS INTERNALDATE
        RFC822.SIZE ENVELOPE BODY), and (FLAGS INTERNALDATE RFC822.SIZE)
        respectively. When we encounter these we will make a list of the
        specified fetch attributes.
        """

        # Is this a list? If so, parse our list of flags.
        #
        if self._p_simple_string("(", silent=True, swallow=False):
            return self._p_paren_list_of(self._p_fetch_att)
        else:
            # See if we have one of the three defined fetch att macros.
            # If we do we will just by hand create our list of fetch atts
            #
            macro = self._p_re(_fetch_att_macros_re, silent=True)
            if macro is not None:
                # We had a macro.. so depending on which one we construct a
                # list of fetch atts.
                #
                macro = macro.lower()
                match macro:
                    case "all":
                        return [
                            FetchAtt(FetchOp.FLAGS),
                            FetchAtt(FetchOp.INTERNALDATE),
                            FetchAtt(FetchOp.RFC822_SIZE),
                            FetchAtt(FetchOp.ENVELOPE),
                        ]
                    case "full":
                        return [
                            FetchAtt(FetchOp.FLAGS),
                            FetchAtt(FetchOp.INTERNALDATE),
                            FetchAtt(FetchOp.RFC822_SIZE),
                            FetchAtt(FetchOp.ENVELOPE),
                            FetchAtt(
                                FetchOp.BODYSTRUCTURE,
                                ext_data=False,
                                actual_command="BODY",
                            ),
                        ]
                    case "fast":
                        return [
                            FetchAtt(FetchOp.FLAGS),
                            FetchAtt(FetchOp.INTERNALDATE),
                            FetchAtt(FetchOp.RFC822_SIZE),
                        ]
                    case _:
                        raise BadSyntax(
                            value=f'"{macro}" is not a valid fetch attribute'
                        )
            else:
                # Otherwise we have what MUST be a single fetch attribute.
                return [self._p_fetch_att()]

    #######################################################################
    #
    def _p_fetch_att(self) -> FetchAtt:
        """fetch_att ::= "ENVELOPE" / "FLAGS" / "INTERNALDATE" /
                     "RFC822" [".HEADER" / ".SIZE" / ".TEXT"] /
                     "BODY" ["STRUCTURE"] / "UID" /
                     "BODY" [".PEEK"] section
                     ["<" number "." nz_number ">"]

           section ::= "[" [section_text / (nz_number *["." nz_number]
                       ["." (section_text / "MIME")])] "]"

           section_text ::= "HEADER" / "HEADER.FIELDS" [".NOT"]
                            SPACE header_list / "TEXT"

           header_fld_name ::= astring

           header_list ::= "(" 1#header_fld_name ")"

        rfc2060 says that the fetch-att is an atom. We could indeed pull off
        the entire atom and have it. We could even then pass this FetchAtt
        object to decipher in to its component bits.  However, we already have
        the precedent of parsing out the entire syntax here and handing more
        digestable pieces to other objects so we are going to continue to do
        that. The FetchAtt object, like the IMAPSearch object, is going to
        need at least a single parameter, which is the fetch att, and
        potentially some additional keyword arguments for the "BODY" fetch att
        that can have several followon bits of information.
        """

        fetch_att_tok = self._p_re(_fetch_att_atom_re)
        if fetch_att_tok is None or fetch_att_tok.lower() not in ParseFetchAtt:
            raise BadSyntax(
                "'%s' is not a valid FETCH argument" % fetch_att_tok
            )
        fetch_att_tok = fetch_att_tok.lower()

        # XXX NOTE, our turning things from shortcuts to their
        # underlying representation we need to store the actual
        # command sent so we can return the command sent to the
        # client!

        # If the fetch att is one of BODY or BODY.PEEK then it may have a
        # section and a 'partial.' Actually it MUST have one, unless
        # it is just "BODY" in which case the fetch_att is really
        # "BODYSTRUCTURE"
        #
        if fetch_att_tok not in ("body", "body.peek"):
            # a rfc822 is turned in to a body[] fetch.
            # a rfc822.header is turned in to a body.peek[header] fetch.
            # a rfc822.text is turned in to a body[text] fetch.
            #
            match fetch_att_tok:
                case ParseFetchAtt.RFC822:
                    return FetchAtt(
                        FetchOp.BODY, section=[], actual_command="RFC822"
                    )
                case ParseFetchAtt.RFC822_SIZE:
                    return FetchAtt(FetchOp.RFC822_SIZE)
                case ParseFetchAtt.RFC822_HEADER:
                    return FetchAtt(
                        FetchOp.BODY,
                        section=["header"],
                        peek=True,
                        actual_command="RFC822.HEADER",
                    )
                case ParseFetchAtt.RFC822_TEXT:
                    return FetchAtt(
                        FetchOp.BODY,
                        section=["text"],
                        actual_command="RFC822.TEXT",
                    )
                case _:
                    return FetchAtt(STR_TO_FETCH_OP[fetch_att_tok])

        # If we have `BODY` and the next character is NOT `[` then this is
        # `BODYSTRUCTURE`
        #
        if (
            fetch_att_tok == ParseFetchAtt.BODY
            and self._p_simple_string("[", silent=True, swallow=False) is None
        ):
            return FetchAtt(
                FetchOp.BODYSTRUCTURE, ext_data=False, actual_command="BODY"
            )

        # We treat `BODY.PEEK` the same as `BODY` with the `peek` attribute set
        # to `True` or `False`.
        #
        if fetch_att_tok == ParseFetchAtt.BODY_PEEK:
            peek = True
            fetch_att_tok = ParseFetchAtt.BODY
        else:
            peek = False

        # Otherwise we must have a section. We must parse what section they
        # want to care about. It will either be one of several text strings or
        # it will be a list of numbers (which may be followed by one of several
        # text strings), all separated by '.'
        #
        section = self._p_section()

        # If the next character is a '<' then we have a 'partial' to parse.
        # Otherwise there is no partial and we are done parsing.
        #
        if self._p_simple_string("<", silent=True, swallow=False) is None:
            return FetchAtt(
                STR_TO_FETCH_OP[fetch_att_tok], section=section, peek=peek
            )

        return FetchAtt(
            STR_TO_FETCH_OP[fetch_att_tok],
            section=section,
            partial=self._p_partial(),
            peek=peek,
        )

    #######################################################################
    #
    def _p_partial(self) -> Tuple[int, int]:
        """An attribute being fetched can have a 'partial' section that
        indicates. It is a '<' integer '.' integer '>'. We will return the
        tuple of integers
        """
        self._p_simple_string("<")
        r = self._p_re(_number_re)
        assert r
        start = int(r)
        self._p_simple_string(".")
        r = self._p_re(_number_re)
        assert r
        end = int(r)
        self._p_simple_string(">")
        return (start, end)

    #######################################################################
    #
    def _p_section(self):
        """Fetch the "section" part of a body.

        section         ::= "[" [section_text / (nz_number *["." nz_number]
                             ["." (section_text / "MIME")])] "]"

        section_text    ::= "HEADER" / "HEADER.FIELDS" [".NOT"]
                            SPACE header_list / "TEXT"

        header_list     ::= "(" 1#header_fld_name ")"
        header_fld_name ::= astring

        (a header list is, in other words, a list of one more or astrings)
        """
        self._p_simple_string("[")
        # The section we start as a list of elements. Each element in the
        # list indicates what sub-section of its preceeding section it
        # refers to.
        #
        # This means that a section is either one of the known text strings
        # or a series of numbers, separated by '.' followed by one of the
        # known text strings OR the string 'MIME'.
        #
        # So, see if we have a list of numbers separated by '.'
        #
        sect_list: List[Union[int, str]] = []
        try:
            while True:
                sect_list.append(int(self._p_re(_number_re)))
                self._p_simple_string(".")
        except NoMatch:
            pass

        # At this point if the next character is ']' then we are at the
        # end of our subsection list.
        #
        if self._p_simple_string("]", silent=True) is not None:
            return sect_list

        # Now we either have one of our known strings. If sect_list is not
        # empty we may also have the string 'MIME'
        #
        section = None
        section_texts = [
            "header.fields.not",
            "header.fields",
            "header",
            "text",
        ]
        if len(sect_list) > 0:
            section_texts.append("mime")
        for st in section_texts:
            section = self._p_simple_string(st, silent=True)
            if section is not None:
                break
        if section is None:
            raise BadSyntax(
                value="%s: expected a valid section "
                "identifier, one of: %s" % (self.input[:10], str(section_texts))
            )

        # If the section is one of 'header.fields.not' or 'header.fields'
        # then we have more parsing to do. We expect a ' ' and then a
        # paren list of astrings which is the 'header' list.
        #
        if section in ("header.fields.not", "header.fields"):
            self._p_simple_string(" ")
            header_list = self._p_paren_list_of(self._p_astring)
            if len(header_list) == 0:
                raise BadSyntax(
                    value="section '%s' must be followed by a "
                    "parenthesized list of one or more "
                    "headers." % section
                )
            sect_list.append((section, header_list))
        else:
            sect_list.append(section)

        # and finally we must be followed by ']'
        self._p_simple_string("]")
        return sect_list

    #######################################################################
    #
    def _p_search_key(self):
        """search_key ::= "ALL" / "ANSWERED" / "BCC" SPACE astring /
                          "BEFORE" SPACE date / "BODY" SPACE astring /
                          "CC" SPACE astring / "DELETED" / "FLAGGED" /
                          "FROM" SPACE astring /
                          "KEYWORD" SPACE flag_keyword / "NEW" / "OLD" /
                          "ON" SPACE date / "RECENT" / "SEEN" /
                          "SINCE" SPACE date / "SUBJECT" SPACE astring /
                          "TEXT" SPACE astring / "TO" SPACE astring /
                          "UNANSWERED" / "UNDELETED" / "UNFLAGGED" /
                          "UNKEYWORD" SPACE flag_keyword / "UNSEEN" /
                          ;; Above this line were in [IMAP2]
                          "DRAFT" /
                          "HEADER" SPACE header_fld_name SPACE astring /
                          "LARGER" SPACE number / "NOT" SPACE search_key /
                          "OR" SPACE search_key SPACE search_key /
                          "SENTBEFORE" SPACE date / "SENTON" SPACE date /
                          "SENTSINCE" SPACE date / "SMALLER" SPACE number /
                          "UID" SPACE set / "UNDRAFT" / set /
                          "(" 1#search_key ")"
        So, yeah, this will parse a single search_key.. which may contain a
        list of search keys.

        Based on the search_key text we will generate an IMAPSearch object
        which we will return to our caller.

        We will use the same mechanism to parse search_key as we do for basic
        commands - we parse out an atom, we see if there is a method that is
        named after the search command and if there is we invoke that command
        to finish parsing this search.
        """

        # First off.. each search key is prefixed by an atom, or a paren. We
        # try to parse it as a single paren first because this is a cheap
        # operation. If that succeeds, we hand it off to be parsed as a list
        # of search_keys.
        #
        # Otherwise we parse it as an atom - if this fails we finally try to
        # parse it as a 'set'.
        #
        if self._p_simple_string("(", silent=True, swallow=False):
            # Okay, a possible list of search keys. If this list has
            # only one element then just return that element. Otherwise
            # return an 'and' (of the list of elements.)
            #
            search_key = self._p_paren_list_of(self._p_search_key)
            if len(search_key) == 1:
                return search_key[0]
            return IMAPSearch("and", search_key=search_key)

        # Not a list.. it is either a seach key atom or a message set.
        #
        search_tok = self._p_re(_search_atom_re, silent=True)
        if search_tok:
            # Okay. It looks like a search token.. but is it one of the search
            # tokens we understand?
            #
            search_tok = search_tok.lower()
            if not hasattr(self, "_p_srchkey_%s" % search_tok):
                raise UnknownSearchKey(
                    value='Unknown search key "%s"' % search_tok
                )
            # Yup. it was a known search key atom. Let our routine
            # specifically for parsing this search key to the rest of the
            # work.
            #
            return getattr(self, "_p_srchkey_%s" % search_tok)()
        else:
            # See if it is a message set.
            msg_set = self._p_msg_set()
            return IMAPSearch("message_set", msg_set=msg_set)

        # Huh.. we have no idea what this is supposed to be.
        #
        raise UnknownSearchKey

    #######################################################################
    #
    def is_seq_num(self, val):
        """sequence_num ::= nz_number / "*"

        This function will return the sequence number passed in as val if it
        is one, or return None if it is not. It will convert the string to an
        int using int() if it is an integer. If will raise a BadSyntax
        exception if we get an int that is < 1.
        """
        if val.isdigit():
            num = int(val)
            if num < 0:  # 0 is a valid uid...
                raise SyntaxError(
                    "message sequence numbers "
                    "must be greater then 0: %d" % num
                )
            return num

        # We allow '*' as a message sequence number.
        if val == "*":
            return val

        # Otherwise return None.
        return None

    #######################################################################
    #
    def _p_srchkey_all(self):
        return IMAPSearch("all")

    #######################################################################
    #
    def _p_srchkey_answered(self):
        return IMAPSearch("keyword", keyword=r"\Answered")

    #######################################################################
    #
    def _p_srchkey_bcc(self):
        self._p_simple_string(" ")
        return IMAPSearch(
            "header", header="bcc", string=self._p_astring().lower()
        )

    #######################################################################
    #
    def _p_srchkey_before(self):
        self._p_simple_string(" ")
        return IMAPSearch("before", date=self._p_date())

    #######################################################################
    #
    def _p_srchkey_body(self):
        self._p_simple_string(" ")
        return IMAPSearch("body", string=self._p_astring().lower())

    #######################################################################
    #
    def _p_srchkey_cc(self):
        self._p_simple_string(" ")
        return IMAPSearch(
            "header", header="cc", string=self._p_astring().lower()
        )

    #######################################################################
    #
    def _p_srchkey_deleted(self):
        return IMAPSearch("keyword", keyword=r"\Deleted")

    #######################################################################
    #
    def _p_srchkey_draft(self):
        return IMAPSearch("keyword", keyword=r"\Draft")

    #######################################################################
    #
    def _p_srchkey_flagged(self):
        return IMAPSearch("keyword", keyword=r"\Flagged")

    #######################################################################
    #
    def _p_srchkey_from(self):
        self._p_simple_string(" ")
        return IMAPSearch(
            "header", header="from", string=self._p_astring().lower()
        )

    #######################################################################
    #
    def _p_srchkey_header(self):
        self._p_simple_string(" ")
        header_fld_name = self._p_astring().lower()
        self._p_simple_string(" ")
        return IMAPSearch(
            "header", header=header_fld_name, string=self._p_astring().lower()
        )

    #######################################################################
    #
    def _p_srchkey_keyword(self):
        self._p_simple_string(" ")
        return IMAPSearch("keyword", keyword=self._p_re(_atom_re))

    #######################################################################
    #
    def _p_srchkey_larger(self):
        self._p_simple_string(" ")
        return IMAPSearch("larger", n=int(self._p_re(_number_re)))

    #######################################################################
    #
    def _p_srchkey_new(self):
        return IMAPSearch(
            "and",
            search_key=[self._p_srchkey_recent(), self._p_srchkey_unseen()],
        )

    #######################################################################
    #
    def _p_srchkey_not(self):
        self._p_simple_string(" ")
        return IMAPSearch("not", search_key=self._p_search_key())

    #######################################################################
    #
    def _p_srchkey_old(self):
        return IMAPSearch("not", search_key=self._p_srchkey_recent())

    #######################################################################
    #
    def _p_srchkey_on(self):
        self._p_simple_string(" ")
        return IMAPSearch("on", date=self._p_date())

    #######################################################################
    #
    def _p_srchkey_or(self):
        self._p_simple_string(" ")
        search_key1 = self._p_search_key()
        self._p_simple_string(" ")
        search_key2 = self._p_search_key()
        return IMAPSearch("or", search_key=(search_key1, search_key2))

    #######################################################################
    #
    def _p_srchkey_recent(self):
        return IMAPSearch("keyword", keyword=r"\Recent")

    #######################################################################
    #
    def _p_srchkey_seen(self):
        return IMAPSearch("keyword", keyword=r"\Seen")

    #######################################################################
    #
    def _p_srchkey_sentbefore(self):
        self._p_simple_string(" ")
        return IMAPSearch("sentbefore", date=self._p_date())

    #######################################################################
    #
    def _p_srchkey_senton(self):
        self._p_simple_string(" ")
        return IMAPSearch("senton", date=self._p_date())

    #######################################################################
    #
    def _p_srchkey_sentsince(self):
        self._p_simple_string(" ")
        return IMAPSearch("sentsince", date=self._p_date())

    #######################################################################
    #
    def _p_srchkey_since(self):
        self._p_simple_string(" ")
        return IMAPSearch("since", date=self._p_date())

    #######################################################################
    #
    def _p_srchkey_smaller(self):
        self._p_simple_string(" ")
        return IMAPSearch("smaller", n=int(self._p_re(_number_re)))

    #######################################################################
    #
    def _p_srchkey_subject(self):
        self._p_simple_string(" ")
        return IMAPSearch(
            "header", header="subject", string=self._p_astring().lower()
        )

    #######################################################################
    #
    def _p_srchkey_text(self):
        self._p_simple_string(" ")
        return IMAPSearch("text", string=self._p_astring().lower())

    #######################################################################
    #
    def _p_srchkey_to(self):
        self._p_simple_string(" ")
        return IMAPSearch(
            "header", header="to", string=self._p_astring().lower()
        )

    #######################################################################
    #
    def _p_srchkey_uid(self):
        self._p_simple_string(" ")
        return IMAPSearch("uid", msg_set=self._p_msg_set())

    #######################################################################
    #
    def _p_srchkey_unanswered(self):
        return IMAPSearch("not", search_key=self._p_srchkey_answered())

    #######################################################################
    #
    def _p_srchkey_undeleted(self):
        return IMAPSearch("not", search_key=self._p_srchkey_deleted())

    #######################################################################
    #
    def _p_srchkey_unflagged(self):
        return IMAPSearch("not", search_key=self._p_srchkey_flagged())

    #######################################################################
    #
    def _p_srchkey_unkeyword(self):
        return IMAPSearch("not", search_key=self._p_srchkey_keyword())

    #######################################################################
    #
    def _p_srchkey_unseen(self):
        return IMAPSearch("not", search_key=self._p_srchkey_seen())

    #######################################################################
    #
    def _p_msg_set(self) -> List:
        """sequence_num ::= nz_number / "*"

        * is the largest number in use.  For message sequence numbers, it is
        the number of messages in the mailbox.  For unique identifiers, it is
        the unique identifier of the last message in the mailbox.

        set  ::= sequence_num / (sequence_num ":" sequence_num) / (set "," set)

        Identifies a set of messages.  For message sequence numbers, these are
        consecutive numbers from 1 to the number of messages in the mailbox
        Comma delimits individual numbers, colon delimits between two numbers
        inclusive. Example: 2,4:7,9,12:* is 2,4,5,6,7,9,12,13,14,15 for a
        mailbox with 15 messages.

        Our cursor in the input string should be at what we expect is a
        message set. We do not know if it actually is so we need to verify
        that.

        We will parse the input and construct a result which is returned to
        our caller. The result will be a list (even if there is only one
        element). The list will be a list of integers, "*", and tuples. Tuples
        will reprsent the "sequence_num : sequence_num" construct. The
        integers MUST be greater then zero.
        """

        # Pull what should be a message off of our input string.
        #
        msg_set = self._p_re(
            _msg_set_re,
            syntax_error="missing or " "invalid message sequence set",
        )
        assert msg_set

        # Now just because we got something does not mean it is a message
        # set. However, we know that it will be comma separated. Between the
        # commas will either be an integer, a '*' or a "foo:bar" where foo &
        # bar are either an intger or '*'
        #
        seqs = msg_set.split(",")
        result = []
        for seq_num in seqs:
            # If it is a nz positive integer or '*', then just append it to
            # our result as an integer.
            #
            sn = self.is_seq_num(seq_num)
            if sn is not None:
                result.append(sn)
                continue

            # Otherwise this element MUST be a 'seq num : seq num'
            # combination.
            #
            search = _msg_set_pair_re.search(seq_num)
            if search:
                sn_start = self.is_seq_num(search.group(1))
                sn_end = self.is_seq_num(search.group(2))
                if sn_start is not None and sn_end is not None:
                    result.append((sn_start, sn_end))
                    continue

            # Otherwise this is a bad sequence number..
            #
            raise BadSyntax(
                value='"%s" is not a valid message ' "sequence number" % seq_num
            )
        return result

    #######################################################################
    #
    def _p_date(self) -> date:
        """date      ::= date_text / <"> date_text <">
           date_text ::= date_day "-" date_month "-" date_year
           date_year ::= 4digit
           date_month ::= "Jan" / "Feb" / "Mar" / "Apr" / "May" / "Jun" /
                          "Jul" / "Aug" / "Sep" / "Oct" / "Nov" / "Dec"
           date_day   ::= 1*2digit -- Day of month

        We parse the date and return a datetime object.
        """
        date_exp = self._p_re(_date_re)
        assert date_exp
        match = _date_re.match(date_exp)
        assert match
        return date(
            year=int(match.group("year")),
            month=_month[match.group("month").lower()],
            day=int(match.group("day")),
        )

    #######################################################################
    #
    def _p_date_time(self):
        """date_time ::= <"> date_day_fixed "-" date_month "-" date_year
                         SPACE time SPACE zone <">

        We have a regular expression to match the entire date time string.
        We will match it again so that we can use symbolic group names for each
        part of the string.

        The return is a datetime object."""

        date_time = self._p_re(
            _date_time_re,
            syntax_error="expected a " "rfc822 formated date-time",
        )
        assert date_time

        # We need to strip off the "" surrounding the date-time string.
        #
        return parsedate(date_time[1:-1])

    #######################################################################
    #
    def _p_flag(self):
        r"""flag ::= "\Answered" / "\Flagged" / "\Deleted" /
                 "\Seen" / "\Draft" / flag_keyword / flag_extension

        flag_extension  ::= "\" atom
                 ;; Future expansion.  Client implementations
                 ;; MUST accept flag_extension flags.  Server
                 ;; implementations MUST NOT generate
                 ;; flag_extension flags except as defined by
                 ;; future standard or standards-track
                 ;; revisions of this specification.

        flag_keyword    ::= atom

        What that above is saying is that a flag is an atom or a "\"
        followed by an atom. Which flags are valid is context dependent and
        we do not know that when parsing."""

        # If the first character is a '\' then swallow it (and set it as the
        # first character of our result.
        #
        flag = ""
        if self._p_simple_string("\\", silent=True) is not None:
            flag = "\\"

        # And what follows is always an atom.
        #
        flag += self._p_re(_atom_re)

        return flag

    #######################################################################
    #
    def _p_status_att(self) -> str:
        """status_att ::= "MESSAGES" / "RECENT" / "UIDNEXT" / "UIDVALIDITY" /
        "UNSEEN"
        """
        stats_atts = ["messages", "recent", "uidnext", "uidvalidity", "unseen"]
        for status in StatusAtt:
            stat = self._p_simple_string(status, silent=True)
            if stat is not None:
                return status
        raise BadSyntax(
            value="expected a status attribute: %s" % str(stats_atts)
        )

    #######################################################################
    #
    def _p_list_mailbox(self):
        """list_mailbox   ::= 1*(ATOM_CHAR / list_wildcards) / string
        list_wildcards ::= '%' / '*'

        In other words, it is the same as the 'atom' r.e., except it may
        also include the characters '%' and '*'.. or it is a string.
        """
        list_mailbox = self._p_re(_list_atom_re, silent=True)
        if list_mailbox is None:
            list_mailbox = self._p_string()
        return list_mailbox

    #######################################################################
    #
    def _p_mailbox(self) -> str:
        """mailbox ::= 'INBOX' / astring

        INBOX is case-insensitive.  All case variants of INBOX (e.g. 'iNbOx')
        MUST be interpreted as INBOX not as an astring.  Refer to section 5.1
        for further semantic details of mailbox names.
        """
        # We must match the case insensitive string 'mailbox' first because
        # our other mailbox names are case sensitive.
        #
        mbox_name = self._p_simple_string("inbox", silent=True)
        if mbox_name is None:
            mbox_name = self._p_astring()
        if mbox_name != "":
            return os.path.normpath(mbox_name)
        else:
            return mbox_name

    #######################################################################
    #
    def _p_astring(self) -> str:
        """an 'astring' is an 'atom' or a 'string'"""
        try:
            return self._p_re(_atom_re)
        except NoMatch:
            return self._p_string()

    #######################################################################
    #
    def _p_string_nstring_pairs(self):
        """we expect a STRING " " (NIL | STRING) pair."""
        key = self._p_string()
        self._p_simple_string(
            " ",
            syntax_error="expected a space between "
            "strings in a parenthesized key/value list",
        )
        # What is next may be 'nil' which turns in to None in python
        # or a string.
        #
        if self._p_simple_string("nil", silent=True):
            return (key, None)
        return (key, self._p_string())

    #######################################################################
    #
    def _p_string(self):
        """A string is either a 'quoted string' or a 'literal string'"""
        try:
            return self._p_re(_quoted_re)[1:-1]
        except NoMatch:
            literal_length = int(self._p_re(_lit_ref_re, group=1))

            # Huh. We have a literal string. This means the client sent us
            # the length of the actual string. The reader that called us
            # passed us the entire client message. This means they went and
            # asked the client for all the data for all literal strings. So
            # the value of this literal string is already on our input
            # string. The only thing we need to check for is to make sure
            # that the input string is at least as long as the literal string
            # is supposed to be.
            if literal_length > len(self.input):
                raise BadLiteral(
                    value="Remaining input %d characters "
                    "long, expected at least %d"
                    % (len(self.input), literal_length)
                )
            str = self.input[:literal_length]
            self.input = self.input[literal_length:]
            return str

    #######################################################################
    #
    def _p_re(
        self,
        regexp: re.Pattern,
        silent: bool = False,
        swallow: bool = True,
        group: int = 0,
        syntax_error: Optional[str] = None,
    ) -> Optional[str]:
        """This will attempt to match (ie: at the beginning of the string)
        the given regular expression with our current input string. If it
        matches it will return what matched. If 'silent' is False, and it did
        NOT match, then it will raise the NoMatch exception. If 'swallow' is
        True, then it will chop off the matched characters from the beginning
        of our input string.

        If 'group' is specified (an integer!) it will be passed to the match
        object's group() method letting the caller pick what part of the
        match they wish returned to them. Of course the r.e. used must have
        appropiate matching group's specified.

        NOTE: If the match fails then we do NOT swallow any input even if
              swallow = True
        """
        match = regexp.match(self.input)
        if match is None:
            if silent:
                return None
            else:
                if syntax_error:
                    raise NoMatch(value=syntax_error)
                else:
                    raise NoMatch(
                        value="No match for r.e. '%s'" % regexp.pattern
                    )
        if swallow:
            self.input = self.input[match.end() :]
        return match.group(group)

    #######################################################################
    #
    def _p_simple_string(
        self,
        string,
        silent=False,
        swallow=True,
        case_matters=False,
        syntax_error=None,
    ):
        """Like p_re(), this is used to parse a bit of input. However it just
        a well defined string so there is no waste time invoking a regular
        expression.

        If 'case_matters' is True then the string comparsion is exact (case
        sensitive.) Otherwise the match is case insensitive.

        If 'case_matters' is False the returned string is forced to lower case.

        If we do not match, then input is not swallowed even if swallow = True.
        """
        if len(self.input) < len(string):
            match = None
        else:
            if case_matters:
                if self.input[: len(string)] == string:
                    match = string
                else:
                    match = None
            else:
                if self.input[: len(string)].lower() == string.lower():
                    match = string.lower()
                else:
                    match = None

        if match is None:
            if silent:
                return None
            else:
                if syntax_error:
                    raise NoMatch(value=syntax_error)
                else:
                    raise NoMatch(
                        value="No match for simple string '%s', input started with: '%s'"
                        % (string, self.input[:10])
                    )
        if swallow:
            self.input = self.input[len(string) :]
        return match

    #
    # Done token parsing routines.
    #
    #######################################################################
    #######################################################################


####################################################################
#
def conflicting_cmd(
    imap_cmd: IMAPClientCommand, tasks: List[IMAPClientCommand]
) -> bool:
    r"""
    Indicates whether the IMAP Command is one that would conflict with any
    of the other IMAP Commands currently executing on a mailbox.

    Certain commands listed in `CONFLICTING_COMMANDS` will conflict with all
    other commands.

    FETCH BODY (ie: not BODY.PEEK) commands will conflict with

    the mailbox while executing.

    This condition is because these commands will modify the mailbox in
    some way and possibly generate notifications.

    If the command is one of CONFLICTING_COMMANDS or if the command is
    a FETCH and one of the fetch attributes is "BODY" (note that
    "BODY.PEEK" is okay because unlike "BODY" it does not set the `\Seen`
    flag on the message.)

    Good IMAP Clients tend to use BODY.PEEK and reserve clearing the
    `\Seen` flag for when the user of the IMAP Client actuall reads the
    message.
    """
    # If there are no other IMAP commands running, then this command is
    # non-conflciting (nothing to conflict with!)
    #
    if not tasks:
        return False

    if imap_cmd.command in CONFLICTING_COMMANDS:
        return True

    if imap_cmd.command == IMAPCommand.FETCH:
        for fetch_att in imap_cmd.fetch_atts:
            if fetch_att.attribute == FetchOp.BODY and not fetch_att.peek:
                return True
    return False


####################################################################
#
def parse_cmd_from_msg(msg: Union[bytes, str]) -> IMAPClientCommand:
    """
    Create an IMAPClientCommand from the message, and parse it. Return the
    command to the caller.
    """
    if isinstance(msg, bytes):
        msg = str(msg, "latin-1")
    cmd = IMAPClientCommand(msg)
    cmd.parse()
    return cmd
