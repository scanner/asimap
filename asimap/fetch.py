"""
Objects and functions to fetch elements of a message.  This is the
module that does the heavy lifting of sending actual content of email
back to the IMAP Client. The FetchAtt class contains the query of what
the IMAP Client has asked for as well as the ability to process that
query and generate the body of the `FETCH` response IMAP message.
"""

# system imports
#
import email.utils
import logging
from email.header import Header
from email.message import EmailMessage, Message
from enum import StrEnum
from typing import List, Optional, Set, Tuple, TypeAlias, Union

# asimap imports
#
from .constants import seq_to_flag
from .exceptions import Bad

# from .generator import msg_as_string, msg_headers_as_string
from .generator import msg_as_bytes, msg_headers_as_bytes

logger = logging.getLogger("asimap.fetch")

# A section in a message that can be fetched.
# XXX `None` indicates the entire message? Or should `Optional` be removed?
#
MsgSectionType: TypeAlias = Optional[
    List[Union[int, str, List[str | List[str]]]]
]


############################################################################
#
class BadSection(Bad):
    def __init__(self, value="bad 'section'"):
        self.value = value

    def __str__(self):
        return f"BadSection: {self.value}"


####################################################################
#
def header_or_nil(msg: Message, field: str) -> bytes:
    """
    a shortcut for encoding a heaader if it exists in a message otherwise
    it returns "NIL"
    """
    return encode_header(msg[field]) if field in msg else b"NIL"


############################################################################
#
def encode_header(hdr: str) -> bytes:
    """
    Attempts to encode a header as bytes. It will first attempt to simply
    encode the header using latin-1. If that fails then we will try to encode
    it as utf-8 using the MIME encoding specified in RFC2047:

        `=?charset?encoding?encoded_text?=`

    If we are unable to encode it using UTF-8, we will encoded as latin-1, but
    falling back to "?" for all characters that can not be encoded.
    """
    try:
        result = hdr.encode("latin-1")
    except UnicodeEncodeError:
        pass

    try:
        # maxlinelen=0 means do not wrap
        #
        result = Header(hdr).encode(maxlinelen=0).encode("latin-1")
    except UnicodeEncodeError:
        result = hdr.encode("latin-1", errors="replace")
    return b'"' + result + b'"'


########################################################################
#
def encode_addrs(msg: Message, field: str) -> bytes:
    """
    Encode all the email addresses in a given field in the message as an
    address structure (as bytes)

    The fields of an address are in the following order: personal name, [SMTP]
    at-domain-list (source route), mailbox name, and host name.

    The includes proper UTF-8 encodeding for the personal name if it can not
    encoded as latin-1.
    """
    field_data: List[str] = msg.get_all(field, [])
    if not field_data:
        return b"NIL"

    result: List[bytes] = []
    addrs = email.utils.getaddresses(field_data, strict=False)

    for real_name, email_address in addrs:
        addr: List[bytes] = []

        # the real name is not set, it turns into nil.
        #
        name = encode_header(real_name) if real_name else b"NIL"
        addr.append(name)
        addr.append(b"NIL")  # We do not bother with the source route.

        # mailbox and hostname MUST be latin-1 encodable is my understanding
        #
        if "@" in email_address:
            mailbox, host = email_address.split("@")
            addr.append(encode_header(mailbox))
            addr.append(encode_header(host))
        else:
            addr.append(encode_header(email_address))
            addr.append(b"NIL")

        result.append(b"(" + b" ".join(addr) + b")")

    return b"(" + b" ".join(result) + b")"


########################################################################
########################################################################
#
# Note that the order is important. We need to match the longest strings with
# the common prefix first to insure that we fully match the proper keyword (ie:
# if we look for 'rfc822' first we will incorrectly not identify a
# 'rfc822.text')
#
class FetchOp(StrEnum):
    ENVELOPE = "envelope"
    FLAGS = "flags"
    INTERNALDATE = "internaldate"
    RFC822_HEADER = "rfc822.header"
    RFC822_SIZE = "rfc822.size"
    RFC822_TEXT = "rfc822.text"
    UID = "uid"
    BODYSTRUCTURE = "bodystructure"
    BODY = "body"


STR_TO_FETCH_OP = {op_enum.value: op_enum for op_enum in FetchOp}


############################################################################
############################################################################
#
class FetchAtt:
    """
    This object contains the parsed out elements of a fetch command that
    specify an attribute to pull out of a message.

    It is expected that this will be applied to a Message object
    instance to retrieve the parts of the message that this FetchAtt
    indicates.
    """

    #######################################################################
    #
    def __init__(
        self,
        attribute: FetchOp,
        section: Optional[List[Union[int, str, List[str | List[str]]]]] = None,
        partial: Optional[Tuple[int, int]] = None,
        peek: bool = False,
        ext_data: bool = True,
        actual_command: Optional[str] = None,
    ):
        """
        Fill in the details of our FetchAtt object based on what we parse
        from the given attribute/section/partial.
        """
        self.attribute = attribute
        self.section = section
        self.partial = partial
        self.peek = peek
        self.ext_data = ext_data
        self.actual_command = (
            actual_command if actual_command else self.attribute.value.upper()
        )
        self.log = logging.getLogger(
            "%s.%s.%s" % (__name__, self.__class__.__name__, actual_command)
        )

    #######################################################################
    #
    def __repr__(self):
        result = [f"FetchAtt({self.attribute.value}"]
        if self.section:
            result.append(f"[{self.section}]")
        if self.partial:
            result.append(f"<{self.partial[0]}>")
        return "".join(result)

    ##################################################################
    #
    def __str__(self) -> str:
        return self.dbg(show_peek=False)

    ####################################################################
    #
    def __bytes__(self) -> bytes:
        return str(self).encode("latin-1")

    ##################################################################
    #
    def dbg(self, show_peek=False) -> str:
        """
        Arguments:
        - `show_peek`: Show if this is a .PEEK or not. This is
          improtant because a FETCH reply does NOT include the peek
          information, but we want it for when we dump debug strings.
        """
        result = self.actual_command
        if result == "BODY":
            if show_peek and self.peek:
                result = "BODY.PEEK"
            if self.section is not None:
                # we need to be careful how we convert HEADER.FIELDS and
                # HEADER.FIELDS.NOT back in to a string.
                #
                sects = []
                for s in self.section:
                    # If this section is a list or a tuple then we have a
                    # 'header.<fields<.not>> (header_list)' section and we need
                    # convert that to a proper string for our FETCH response.
                    #
                    if isinstance(s, (list, tuple)):
                        sect = str(s[0]).upper()
                        paren = " ".join(x for x in s[1])
                        sects.append(f"{sect} ({paren})")
                    else:
                        sects.append(str(s).upper())
                result += f"[{'.'.join(sects)}]"
            if self.partial:
                result += f"<{self.partial[0]}>"
        return result

    #######################################################################
    #
    def fetch(self, ctx) -> bytes:
        r"""
        This method applies fetch criteria that this object represents
        to the message and message entry being passed in.

        It returns the part of the message wanted as a string ready to
        be sent right back to the client that asked for it.

        NOTE: In case you are wondering a FETCH of a message can cause
        it to gain a '\Seen' flag.
        """
        self.ctx = ctx

        # Based on the operation figure out what subroutine does the rest
        # of the work.
        #
        result: Union[bytes, int]
        match self.attribute:
            case (
                FetchOp.BODY
                | FetchOp.BODYSTRUCTURE
                | FetchOp.ENVELOPE
                | FetchOp.RFC822_SIZE
            ):
                msg = self.ctx.msg()
                try:
                    match self.attribute:
                        case FetchOp.BODY:
                            result = self.body(msg, self.section)
                        case FetchOp.BODYSTRUCTURE:
                            result = self.bodystructure(msg)
                        case FetchOp.ENVELOPE:
                            result = self.envelope(msg)
                        case FetchOp.RFC822_SIZE:
                            result = str(ctx.msg_size()).encode("latin-1")
                except UnicodeEncodeError as e:
                    logger.error(
                        (
                            "Unable to perform fetch %s, failed on message %s, "
                            "exception: %r",
                        ),
                        self.attribute,
                        self.ctx,
                        e,
                    )
                    raise
            case FetchOp.FLAGS:
                flags = " ".join([seq_to_flag(x) for x in self.ctx.sequences])
                result = f"({flags})".encode("latin-1")
            case FetchOp.INTERNALDATE:
                int_date = self.ctx.internal_date()
                internal_date = email.utils.format_datetime(int_date)
                result = f'"{internal_date}"'.encode("latin-1")
            case FetchOp.UID:
                result = str(self.ctx.uid()).encode("latin-1")
            case _:
                raise NotImplementedError

        return bytes(self) + b" " + result

    ####################################################################
    #
    def _single_section(
        self, msg: Message | EmailMessage, section: Union[int | str]
    ) -> bytes:
        """
        Flatten message text from single top level section.
        """
        match section:
            case int():
                # If they want section 1 and this message is NOT multipart
                # then this is equivalent to `BODY[TEXT]`
                #
                if not msg.is_multipart():
                    if section != 1:
                        raise BadSection(
                            f"Trying to retrieve section {section} and this "
                            "message is not multipart"
                        )
                    return msg_as_bytes(msg, render_headers=False)

                # Message is multipart. Retrieve the part that they want.
                #
                try:
                    # NOTE: IMAP sections are 1 based. Sections in the message
                    #       are 0-based.
                    #
                    return msg_as_bytes(
                        msg.get_payload(section - 1), render_headers=False
                    )
                except IndexError:
                    raise BadSection(
                        f"Section {section} does not exist in this message "
                        "sub-part"
                    )

            case list() | tuple():
                match section[0].upper():
                    case "HEADER.FIELDS":
                        headers = tuple(section[1])
                        return msg_headers_as_bytes(
                            msg, headers=headers, skip=False
                        )
                    case "HEADER.FIELDS.NOT":
                        headers = tuple(section[1])
                        return msg_headers_as_bytes(
                            msg, headers=headers, skip=True
                        )
                    case _:
                        raise BadSection(
                            "Section value must be either HEADER.FIELDS or "
                            f"HEADER.FIELDS.NOT, not: '{section[0]}'"
                        )

            case str():
                match section.upper():
                    case "TEXT":
                        return msg_as_bytes(msg, render_headers=False)
                    case "MIME":
                        # XXX just use the generator as it is for MIME.. I know
                        #     this is not quite right in that it will accept
                        #     more then it should, but it will otherwise work.
                        #
                        return msg_headers_as_bytes(msg)
                    case "HEADER":
                        # if the content type is message/rfc822 then to get the
                        # headers we need to use the first sub-part of this
                        # message.
                        #
                        if (
                            msg.is_multipart()
                            and msg.get_content_type() == "message/rfc822"
                        ):
                            return msg_headers_as_bytes(msg.get_payload(0))
                        return msg_headers_as_bytes(msg)
                    case _:
                        self.log.warn(
                            f"body: Unexpected section value: '{section}'"
                        )
                        raise BadSection(
                            f"{self}: Unexpected section value: '{section}'"
                        )

            case _:
                self.log.warn(f"body: Unexpected section value: '{section}'")
                raise BadSection(
                    f"{self}: Unexpected section value: '{section}'"
                )

    ####################################################################
    #
    def _body(
        self, msg: Message | EmailMessage, section: Union[None, List[int | str]]
    ) -> bytes:
        if not section:
            return msg_as_bytes(msg)

        if len(section) == 1:
            return self._single_section(msg, section[0])

        if isinstance(section[0], int):
            # We have an integer sub-section. This means that we
            # need to pull apart the message (it MUST be a
            # multi-part message) and pass to a recursive
            # invocation of this function the sub-part and the
            # section list (with the top element of the section
            # list removed.)
            #
            if not msg.is_multipart():
                raise BadSection(
                    f"Message does not contain subsection {section[0]} "
                    f"(section list: {section})"
                )
            try:
                bp = msg.get_payload(section[0] - 1)
                assert isinstance(bp, Message)
                return self._body(bp, section[1:])
            except (TypeError, IndexError):
                raise BadSection(
                    f"Message does not contain subsection {section[0]} "
                    f"(section list: {section})"
                )
        return msg_as_bytes(msg)

    ####################################################################
    #
    def body(
        self, msg: EmailMessage, section: Union[None, List[int | str]]
    ) -> bytes:
        """
        Fetch the appropriate section of the message, flatten into a string
        and return it to the user.
        """
        msg_text = self._body(msg, section)

        # We need to always terminate with crlf.
        #
        msg_text = (
            msg_text if msg_text.endswith(b"\r\n") else msg_text + b"\r\n"
        )

        # If this is a partial only return the bits asked for.
        #
        if self.partial:
            end = self.partial[0] + self.partial[1]
            msg_text = msg_text[self.partial[0] : end]

        # Return literal length encoded string.
        #
        return (f"{{{len(msg_text)}}}\r\n").encode("latin-1") + msg_text

    #######################################################################
    #
    def envelope(self, msg: Message) -> bytes:
        """
        Get the envelope structure of the message as a list, in a defined
        order.

        The fields of the envelope structure are in the following order:
        - date
        - subject
        - from
        - sender
        - reply-to
        - to
        - cc
        - bcc
        - in-reply-to
        - message-id

        The date, subject, in-reply-to, and message-id fields are
        strings(bytes).  The from, sender, reply-to, to, cc, and bcc fields are
        parenthesized lists of address structures.

        Any fields that we can not determine the value of are NIL.
        """
        date = header_or_nil(msg, "date")
        subject = header_or_nil(msg, "subject")

        # Messages without a From field are bad, but we need to supply all
        # fields for the ENVELOPE response (and we have seen messages without a
        # From field.)
        #
        frm = encode_addrs(msg, "from")
        sender = encode_addrs(msg, "sender") if "sender" in msg else frm
        reply_to = encode_addrs(msg, "reply-to") if "reply-to" in msg else frm
        to = encode_addrs(msg, "to")
        cc = encode_addrs(msg, "cc")
        bcc = encode_addrs(msg, "bcc")
        in_reply_to = header_or_nil(msg, "in-reply-to")
        msg_id = header_or_nil(msg, "message-id")

        return (
            b"("
            + b" ".join(
                (
                    date,
                    subject,
                    frm,
                    sender,
                    reply_to,
                    to,
                    cc,
                    bcc,
                    in_reply_to,
                    msg_id,
                )
            )
            + b")"
        )

    ##################################################################
    #
    def body_languages(self, msg: Message | EmailMessage) -> bytes:
        """
        Find the language related headers in the message and return a
        string suitable for the 'body language' element of a
        bodystructure reply.

        Arguments:
        - `msg`: the message we are looking in..
        """
        langs: Set[str] = set()
        values: List[str] = []
        for hdr in ("Accept-Language", "Content-Language"):
            values.extend(msg.get_all(hdr, failobj=[]))

        for value in values:
            if "," in value:
                for lng in value.split(","):
                    langs.add(f'"{lng.strip()}"')
            elif ";" in value:
                for lng in value.split(";"):
                    langs.add(f'"{lng.strip()}"')
            else:
                langs.add(f'"{value.strip()}"')

        if not langs:
            return b"NIL"
        elif len(langs) == 1:
            return (list(langs)[0]).encode("latin-1")
        else:
            return (f"({' '.join([x for x in sorted(list(langs))])})").encode(
                "latin-1"
            )

    ##################################################################
    #
    def body_location(self, msg: EmailMessage) -> bytes:
        """
        Suss if the message part has a 'content-location' header or not
        and return it if it does (or NIL if it does not.)

        Arguments:
        - `msg`: the message (message part) we are looking at
        """
        if "content-location" in msg:
            return (f'"{msg["content-location"]}"').encode("latin-1")
        return b"NIL"

    ##################################################################
    #
    def body_parameters(self, msg: EmailMessage) -> bytes:
        """
        The body parameters for a message as a parenthesized list.
        Basically this list is a set of key value pairs, all separate by
        spaces. So the parameter "CHARSET" with a value of "US-ASCII" and a
        "NAME" with a value of "cc.diff" looks like:

             ("CHARSET" "US-ASCII" "NAME" "cc.diff")

        """
        params = {}

        # Get the charset.. if the message has no charset, yet it is of type
        # text or message, set it to be us-ascii.
        #
        charset = msg.get_content_charset()
        maintype = msg.get_content_maintype()
        if charset is None and maintype in ("text", "message"):
            charset = "us-ascii"
        if charset:
            params["CHARSET"] = charset.upper()

        # Add any other params from the 'Content-Type' header if it exists.
        #
        if "Content-Type" in msg:
            for param in msg["Content-Type"].params.keys():
                if param.lower() == "charset":
                    continue
                params[param] = msg["Content-Type"].params[param]

        if not params:
            return b"NIL"

        results = []
        for k, v in params.items():
            results.append(f'"{k.upper()}" "{v}"')

        try:
            res = (f"({' '.join(results)})").encode("latin-1")
        except UnicodeEncodeError:
            res = (f"({' '.join(results)})").encode("utf-8")
        return res

    ####################################################################
    #
    def extension_data(self, msg: Message | EmailMessage) -> List[bytes]:
        """
        Keyword Arguments:
        msg: EmailMessages --
        """
        results = []
        results.append(self.body_disposition(msg))
        results.append(self.body_languages(msg))
        cl = (
            f'"{msg["Content-Location"]}"'
            if "Content-Location" in msg
            else "NIL"
        )
        results.append(cl.encode("latin-1"))
        return results

    #######################################################################
    #
    def body_disposition(self, msg: Message | EmailMessage) -> bytes:
        """
        Return the body-disposition properly formatted for returning as
        part of a BODYSTRUCTURE fetch.

         body disposition
            A parenthesized list, consisting of a disposition type
            string, followed by a parenthesized list of disposition
            attribute/value pairs as defined in [DISPOSITION].

        This is the 'content-disposition' of the message.

        Arguments:
        - `msg`: The message (or message sub-part) we are looking at
        """
        cd = msg.get_content_disposition()
        if cd is None:
            return b"NIL"

        params = msg["Content-Disposition"].params
        if not params:
            return (f'("{cd}" NIL)').encode("latin-1")

        result = []
        for param, value in params.items():
            result.append(f'"{param.upper()}" "{value}"')
        res = f'("{cd.upper()}" ({" ".join(result)}))'
        try:
            return res.encode("latin-1")
        except UnicodeEncodeError:
            return res.encode("utf-8")

    #######################################################################
    #
    def bodystructure(self, msg: Message) -> bytes:
        """
        The [MIME-IMB] body structure of the message.  This is computed by
        the server by parsing the [MIME-IMB] header fields in the [RFC-2822]
        header and [MIME-IMB] headers.

        A parenthesized list that describes the [MIME-IMB] body structure of a
        message.  This is computed by the server by parsing the [MIME-IMB]
        header fields, defaulting various fields as necessary.

        For example, a simple text message of 48 lines and 2279 octets can have
        a body structure of:

           ("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2279 48)

        Multiple parts are indicated by parenthesis nesting.  Instead of a body
        type as the first element of the parenthesized list, there is a
        sequence of one or more nested body structures.  The second element of
        the parenthesized list is the multipart subtype (mixed, digest,
        parallel, alternative, etc.).

        For example, a two part message consisting of a text and a
        BASE64-encoded text attachment can have a body structure of:

           (("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 1152 23)
            ("TEXT" "PLAIN" ("CHARSET" "US-ASCII" "NAME" "cc.diff")
            "<960723163407.20117h@cac.washington.edu>" "Compiler diff"
            "BASE64" 4554 73)
            "MIXED"
           )

        Extension data follows the multipart subtype.  Extension data is never
        returned with the BODY fetch, but can be returned with a BODYSTRUCTURE
        fetch.  Extension data, if present, MUST be in the defined order.

        See RFC3501 `BODYSTRUCTURE` response for the rest of the description.
        """

        content_type = msg.get_content_type()
        if msg.is_multipart() and content_type != "message/rfc822":
            # For multiparts we are going to generate the bodystructure for
            # each sub-part.
            #
            # We take all these body parts and make a parentheized list.
            # At the end we add on the mime multipart subtype
            #
            # And finally, if we can, we add on the multipart extension data.
            #
            sub_parts: List[bytes] = []
            for sub_part in msg.get_payload():
                assert isinstance(sub_part, Message)
                sub_parts.append(self.bodystructure(sub_part))

            # If we are NOT supposed to return extension data (ie:
            # doing a 'body' not a 'bodystructure' then we have
            # everything we need to return a result.
            #
            subtype = (msg.get_content_subtype().upper()).encode("latin-1")
            if not self.ext_data:
                res = b"(" + b"".join(sub_parts) + b'"' + subtype + b'")'
                return res

            # Get the extension data and add it to our response.
            #
            ext_data = self.extension_data(msg)
            body_params = self.body_parameters(msg)
            ext_data.insert(0, body_params)

            res = (
                b"("
                + b"".join(sub_parts)
                + b' "'
                + subtype
                + b'" '
                + b" ".join(ext_data)
                + b")"
            )
            return res

        # This is a non-multipart msg (NOTE: This may very well be one of the
        # sub-parts of the original message).
        #
        # As such the response is made up of the following elements that will
        # be returned as a space separated, parenthesized list:
        #
        # The basic fields are:
        # - body type
        # - body subtype
        # - body parameter parenthesized list
        # - body id - Content-ID header field value (NIL if none)
        # - body description - Content-Description header field (NIL if none)
        # - body encoding - content transfer encoding
        # - body size - size of the body in octets
        #
        # After the basic fields:
        #   If this is a `message/rfc822`:
        #     - envelope structure
        #     - body structure
        #     - size of the encapsulated message in text lines
        #   If bodytype is 'text':
        #     - size of the body in text lines
        #
        # After the above fields comes the "extension data"
        # - body md5
        # - body disposition: A parenthesized list with the same content
        #   and function as the body disposition for a multipart body part.
        # - body langauge: A string or parenthesized list giving the body
        #   language value as defined in [LANGUAGE-TAGS].
        # - body location - A string giving the body content URI as defined
        #   in [LOCATION].
        #
        result = []

        # Body type and sub-type
        #
        maintype = msg.get_content_maintype()
        subtype = msg.get_content_subtype()
        result.append((f'"{maintype.upper()}"').encode("latin-1"))
        result.append((f'"{subtype.upper()}"').encode("latin-1"))

        result.append(self.body_parameters(msg))

        body_id = f'"{msg["Content-ID"]}"' if "Content-ID" in msg else "NIL"
        result.append(body_id.encode("latin-1"))

        body_desc = (
            f'"{msg["Content-Description"]}"'
            if "Content-Description" in msg
            else "NIL"
        )
        result.append(body_desc.encode("latin-1"))

        cte = (
            msg["Content-Transfer-Encoding"]
            if "Content-Transfer-Encoding" in msg
            else "7BIT"
        )
        result.append((f'"{cte}"').encode("latin-1"))

        # Body size
        payload = msg_as_bytes(msg, render_headers=False)
        result.append(str(len(payload)).encode("latin-1"))
        num_lines = payload.count(b"\n")

        # Now come the variable fields depending on the maintype/subtype
        # of this message.
        #
        if maintype == "message" and subtype == "rfc822":
            # - envelope structure
            # - body structure
            # - size in text lines of encapsulated message
            encapsulated_msg = msg.get_payload()[0]
            assert isinstance(encapsulated_msg, Message)
            result.append(self.envelope(encapsulated_msg))
            result.append(self.bodystructure(encapsulated_msg))
            encapsulated_msg_text = msg_as_bytes(encapsulated_msg)
            result.append(
                str(encapsulated_msg_text.count(b"\n")).encode("latin-1")
            )
        elif msg.get_content_maintype() == "text":
            result.append(str(num_lines).encode("latin-1"))

        # If we are not supposed to return extension data then we are done here
        #
        if not self.ext_data:
            return b"(" + b" ".join(result) + b")"

        # Now we have the message body extension data. NOTE: This does NOT
        # return the `NIL` for MD5.
        #
        extension_data = self.extension_data(msg)
        extension_data.insert(0, b"NIL")

        # Extension data should be optional but some IMAP clients seem to
        # require it. it does no harm to include it as far as I can tell.
        #
        for x in extension_data:
            result.append(x)

        return b"(" + b" ".join(result) + b")"
