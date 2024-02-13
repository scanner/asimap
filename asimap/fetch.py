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
from email.message import EmailMessage
from enum import StrEnum
from typing import List, Optional, Set, Tuple, Union

# asimap imports
#
from .constants import seq_to_flag
from .exceptions import Bad
from .generator import msg_as_string, msg_headers_as_string

logger = logging.getLogger("asimap.fetch")


############################################################################
#
class BadSection(Bad):
    def __init__(self, value="bad 'section'"):
        self.value = value

    def __str__(self):
        return f"BadSection: {self.value}"


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

ENVELOPE_FIELDS = (
    "date",
    "subject",
    "from",
    "sender",
    "reply-to",
    "to",
    "cc",
    "bcc",
    "in-reply-to",
    "message-id",
)


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
            result.append(f"<{self.partial[0]}.{self.partial[1]}>")
        return "".join(result)

    ##################################################################
    #
    def __str__(self):
        return self.dbg(show_peek=False)

    ##################################################################
    #
    def dbg(self, show_peek=False):
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
                result += f"<{self.partial[0]}.{self.partial[1]}>"
        return result

    #######################################################################
    #
    async def fetch(self, ctx) -> str:
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
        result: Union[str, int]
        match self.attribute:
            case (
                FetchOp.BODY
                | FetchOp.BODYSTRUCTURE
                | FetchOp.ENVELOPE
                | FetchOp.RFC822_SIZE
            ):
                msg = await self.ctx.email_message()
                match self.attribute:
                    case FetchOp.BODY:
                        result = self.body(msg, self.section)
                    case FetchOp.BODYSTRUCTURE:
                        result = self.bodystructure(msg)
                    case FetchOp.ENVELOPE:
                        result = self.envelope(msg)
                    case FetchOp.RFC822_SIZE:
                        result = await ctx.msg_size()
            case FetchOp.FLAGS:
                flags = " ".join([seq_to_flag(x) for x in self.ctx.sequences])
                result = f"({flags})"
            case FetchOp.INTERNALDATE:
                int_date = await self.ctx.internal_date()
                internal_date = email.utils.format_datetime(int_date)
                result = f'"{internal_date}"'
            case FetchOp.UID:
                result = await self.ctx.uid()
            case _:
                raise NotImplementedError

        return f"{str(self)} {result}"

    ####################################################################
    #
    def _single_section(
        self, msg: EmailMessage, section: Union[int | str]
    ) -> str:
        """
        Flatten message text from single top level section.
        """
        if isinstance(section, int):
            if section == 1 and not msg.is_multipart():
                # If they want section 1 and this message is NOT multipart then
                # this is equivalent to `BODY[TEXT]`
                #
                return msg_as_string(msg, headers=False)

            # Otherwise, get the sub-part of the message that they are after
            #
            if section != 1 and not msg.is_multipart():
                raise BadSection(
                    f"Trying to retrieve section {section} and this message "
                    "is not multipart"
                )

            try:
                # NOTE: IMAP sections are 1 based. Sections in the message are
                #       0-based.
                #
                return msg_as_string(
                    msg.get_payload(section - 1), headers=False
                )
            except IndexError:
                raise BadSection(
                    f"Section {section} does not exist in this message sub-part"
                )
        elif isinstance(section, str) and section.upper() == "TEXT":
            return msg_as_string(msg, headers=False)
        elif isinstance(section, (list, tuple)):
            if section[0].upper() == "HEADER.FIELDS":
                return msg_headers_as_string(
                    msg, headers=section[1], skip=False
                )
            elif section[0].upper() == "HEADER.FIELDS.NOT":
                return msg_headers_as_string(msg, headers=section[1], skip=True)
            else:
                raise BadSection(
                    "Section value must be either HEADER.FIELDS or "
                    f"HEADER.FIELDS.NOT, not: '{section[0]}'"
                )
        # ELSE: section is a str.
        #
        if not isinstance(section, str):
            self.log.warn(f"body: Unexpected section value: '{section}'")
            raise BadSection(f"{self}: Unexpected section value: '{section}'")

        match section.upper():
            case "MIME":
                # XXX just use the generator as it is for MIME.. I know this is
                #     not quite right in that it will accept more then it
                #     should, but it will otherwise work.
                #
                return msg_headers_as_string(msg)
            case "HEADER":
                # if the content type is message/rfc822 then to
                # get the headers we need to use the first
                # sub-part of this message.
                #
                if (
                    msg.is_multipart()
                    and msg.get_content_type() == "message/rfc822"
                ):
                    return msg_headers_as_string(msg.get_payload(0))
                else:
                    return msg_headers_as_string(msg)
            case _:
                self.log.warn(f"body: Unexpected section value: '{section}'")
                raise BadSection(
                    f"{self}: Unexpected section value: '{section}'"
                )

    ####################################################################
    #
    def _body(
        self, msg: EmailMessage, section: Union[None, List[int | str]]
    ) -> str:
        if not section:
            return msg_as_string(msg)

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
                return self._body(msg.get_payload(section[0] - 1), section[1:])
            except TypeError:
                raise BadSection(
                    f"Message does not contain subsection {section[0]} "
                    f"(section list: {section})"
                )
        return msg_as_string(msg)

    ####################################################################
    #
    def body(self, msg: EmailMessage, section: Union[None, List[int | str]]):
        """
        Fetch the appropriate section of the message, flatten into a string
        and return it to the user.
        """
        msg_text = self._body(msg, section)

        # We need to always terminate with crlf.
        #
        msg_text = msg_text if msg_text.endswith("\r\n") else msg_text + "\r\n"

        # If this is a partial only return the bits asked for.
        #
        if self.partial:
            msg_text = msg_text[self.partial[0] : self.partial[1]]

        # Return literal length encoded string.
        #
        return f"{{{len(msg_text)}}}\r\n{msg_text}"

    #######################################################################
    #
    def envelope(self, msg: EmailMessage) -> str:
        """
        Get the envelope structure of the message as a list, in a defined
        order.

        Any fields that we can not determine the value of are NIL.
        """
        result = []

        from_field = ""
        for field in ENVELOPE_FIELDS:
            if field in ("date", "subject", "in-reply-to", "message-id"):
                if field not in msg:
                    result.append("NIL")
                    continue
            # 'reply-to' and 'sender' are copied from the 'from' field
            # if they are not explicitly defined.
            #
            if field in ("sender", "reply-to") and field not in msg:
                result.append(from_field)
                continue

            # If a field is not in the message it is nil.
            #
            if field not in msg:
                if field == "from":
                    # Messages without a From field are bad, but we need to
                    # supply all fields for the ENVELOPE response.
                    #
                    from_field = "NIL"
                result.append("NIL")
                continue

            # The from, sender, reply-to, to, cc, and bcc fields are
            # parenthesized lists of address structures.
            #
            if field in ("from", "sender", "reply-to", "to", "cc", "bcc"):
                field_data: List[str] = msg.get_all(field, [])
                if field_data:
                    addrs = email.utils.getaddresses(field_data)
                else:
                    result.append("NIL")
                    continue

                addr_list = []

                # Parse each address in to an address structure: An address
                # structure is a parenthesized list that describes an
                # electronic mail address.  The fields of an address structure
                # are in the following order: personal name, [SMTP]
                # at-domain-list (source route), mailbox name, and host name.
                #
                for name, paddr in addrs:
                    one_addr = []
                    if name == "":
                        one_addr.append("NIL")
                    else:
                        one_addr.append(f'"{name}"')

                    # This is the '[SMTP] at-domain-list (source route)' which
                    # for now we do not bother with (not in any of my messages
                    # so I am just going to punt on it.)
                    #
                    one_addr.append("NIL")

                    # Next: mailbox name, and host name. Handle the case like
                    # 'MAILER-DAEMON' on the local host where there is no host
                    # name.
                    #
                    if paddr != "":
                        if "@" in paddr:
                            mbox_name, host_name = paddr.split("@")
                            one_addr.append(f'"{mbox_name}"')
                            one_addr.append(f'"{host_name}"')
                        else:
                            one_addr.append(f'"{paddr}"')
                            one_addr.append("NIL")
                    else:
                        one_addr.append("NIL")
                    addr_list.append(f"({' '.join(one_addr)})")
                result.append(f"({''.join(addr_list)})")

                # We stash the from field because we may need it for sender
                # and reply-to
                #
                if field == "from":
                    from_field = result[-1]
            else:
                result.append(f'"{msg[field]}"')
        return f"({' '.join(result)})"

    ##################################################################
    #
    def body_languages(self, msg: EmailMessage) -> str:
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
            return "NIL"
        elif len(langs) == 1:
            return list(langs)[0]
        else:
            return f"({' '.join([x for x in sorted(list(langs))])})"

    ##################################################################
    #
    def body_location(self, msg: EmailMessage) -> str:
        """
        Suss if the message part has a 'content-location' header or not
        and return it if it does (or NIL if it does not.)

        Arguments:
        - `msg`: the message (message part) we are looking at
        """
        if "content-location" in msg:
            return '"%s"' % msg["content-location"]
        return "NIL"

    ##################################################################
    #
    def body_parameters(self, msg: EmailMessage) -> str:
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
            return "NIL"

        results = []
        for k, v in params.items():
            results.append(f'"{k.upper()}" "{v}"')

        return f"({' '.join(results)})"

    ####################################################################
    #
    def extension_data(self, msg: EmailMessage) -> List[str]:
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
        results.append(cl)
        return results

    #######################################################################
    #
    def body_disposition(self, msg: EmailMessage) -> str:
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
            return "NIL"

        params = msg["Content-Disposition"].params
        if not params:
            return f'("{cd}" NIL)'

        result = []
        for param, value in params.items():
            result.append(f'"{param.upper()}" "{value}"')

        return f'("{cd.upper()}" ({" ".join(result)}))'

    #######################################################################
    #
    def bodystructure(self, msg: EmailMessage) -> str:
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
            "BASE64" 4554 73) "MIXED")

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
            sub_parts = []
            for sub_part in msg.get_payload():
                sub_parts.append(self.bodystructure(sub_part))

            # If we are NOT supposed to return extension data (ie:
            # doing a 'body' not a 'bodystructure' then we have
            # everything we need to return a result.
            #
            subtype = msg.get_content_subtype().upper()
            if not self.ext_data:
                return f'({"".join(sub_parts)} "{subtype}")'

            # Get the extension data and add it to our response.
            #
            ext_data = self.extension_data(msg)
            body_params = self.body_parameters(msg)
            ext_data.insert(0, body_params)

            return f'({"".join(sub_parts)} "{subtype}" {" ".join(ext_data)})'

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
        result.append(f'"{maintype.upper()}"')
        result.append(f'"{subtype.upper()}"')

        result.append(self.body_parameters(msg))

        body_id = f'"{msg["Content-ID"]}"' if "Content-ID" in msg else "NIL"
        result.append(body_id)

        body_desc = (
            f'"{msg["Content-Description"]}"'
            if "Content-Description" in msg
            else "NIL"
        )
        result.append(body_desc)

        cte = (
            msg["Content-Transfer-Encoding"]
            if "Content-Transfer-Encoding" in msg
            else "7BIT"
        )
        result.append(f'"{cte}"')

        # Body size
        payload = msg_as_string(msg, headers=False)
        result.append(str(len(payload)))
        num_lines = payload.count("\n")

        # Now come the variable fields depending on the maintype/subtype
        # of this message.
        #
        if maintype == "message" and subtype == "rfc822":
            # - envelope structure
            # - body structure
            # - size in text lines of encapsulated message
            encapsulated_msg = msg.get_payload()[0]
            result.append(self.envelope(encapsulated_msg))
            result.append(self.bodystructure(encapsulated_msg))
            encapsulated_msg_text = msg_as_string(encapsulated_msg)
            result.append(str(encapsulated_msg_text.count("\n")))
        elif msg.get_content_maintype() == "text":
            result.append(str(num_lines))

        # If we are not supposed to return extension data then we are done here
        #
        if not self.ext_data:
            return f"({' '.join(result)})"

        # Now we have the message body extension data. NOTE: This does NOT
        # return the `NIL` for MD5.
        #
        extension_data = self.extension_data(msg)
        extension_data.insert(0, "NIL")

        # If there is no extension data then do not bother to include it.
        #
        # if any(x != "NIL" for x in extension_data):
        #     for x in extension_data:
        #         result.append(x)
        #
        # Extension data should be optional but some IMAP clients seem to
        # require it. it does no harm to include it as far as I can tell.
        #
        for x in extension_data:
            result.append(x)
        return f"({' '.join(result)})"
