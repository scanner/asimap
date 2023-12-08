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
from email.generator import Generator
from email.policy import SMTP
from enum import StrEnum
from io import StringIO
from typing import List, Optional, Tuple, Union

# asimap imports
#
from .constants import seq_to_flag
from .exceptions import Bad
from .generator import HeaderGenerator, TextGenerator

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
        section: Optional[List[Union[int, str]]] = None,
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
            case FetchOp.BODY | FetchOp.BODYSTRUCTURE | FetchOp.ENVELOPE | FetchOp.RFC822_SIZE:
                msg = await self.ctx.msg()
                match self.attribute:
                    case FetchOp.BODY:
                        result = self.body(msg, self.section)
                    case FetchOp.BODYSTRUCTURE:
                        result = self.bodystructure(msg)
                    case FetchOp.ENVELOPE:
                        result = self.envelope(msg)
                    case FetchOp.RFC822_SIZE:
                        fp = StringIO()
                        g = Generator(fp, mangle_from_=False, policy=SMTP)
                        g.flatten(self.ctx.msg)
                        result = len(fp.getvalue())
            case FetchOp.FLAGS:
                flags = " ".join([seq_to_flag(x) for x in self.ctx.sequences])
                result = f"({flags})"
            case FetchOp.INTERNALDATE:
                int_date = await self.ctx.internal_date()
                internal_date = int_date.strftime("%d-%b-%Y %H:%m:%S %z")
                result = f'"{internal_date}"'
            case FetchOp.UID:
                result = self.ctx.uid
            case _:
                raise NotImplementedError

        return f"{str(self)} {result}"

    #######################################################################
    #
    def body(self, msg, section):
        """
        Fetch the appropriate part of the message and return it to the
        user.
        """

        msg_text = None
        g = None
        if not section:
            # They want the entire message.
            #
            # This really only ever is the case as our first invocation of the
            # body() method. All further recursive calls will always have at
            # least one element in the section list when they are called.
            #
            fp = StringIO()
            g = Generator(fp, mangle_from_=False, policy=SMTP)
            g.flatten(self.ctx.msg)
            msg_text = fp.getvalue()
        else:
            if len(section) == 1:
                fp = StringIO()
                if isinstance(section[0], int):
                    # The want a sub-part. Get that sub-part. Watch
                    # out for messages that are not multipart. You can
                    # get a sub-part of these as long as it is only
                    # sub-part #1.
                    #
                    g = TextGenerator(fp, headers=False)

                    if section[0] == 1 and not msg.is_multipart():
                        # The logic is simpler to read this way.. if
                        # they want section 1 and this message is NOT
                        # a multipart message, then do the same as
                        # 'TEXT' ie: We will fall through to the end of this
                        # function where we will take the generator
                        # already created above and use it.
                        #
                        pass
                    else:
                        # Otherwise, get the sub-part they are after
                        # as the message to pass to the generator.
                        #
                        if section[0] != 1 and not msg.is_multipart():
                            raise BadSection(
                                "Trying to retrieve section %d "
                                "and this message is not "
                                "multipart" % section[0]
                            )
                        try:
                            msg = msg.get_payload(section[0] - 1)
                        except IndexError:
                            raise BadSection(
                                "Section %d does not exist in "
                                "this message sub-part" % section[0]
                            )
                elif (
                    isinstance(section[0], str) and section[0].upper() == "TEXT"
                ):
                    g = TextGenerator(fp, headers=False)
                elif isinstance(section[0], (list, tuple)):
                    if section[0][0].upper() == "HEADER.FIELDS":
                        g = HeaderGenerator(
                            fp, headers=section[0][1], skip=False
                        )
                    elif section[0][0].upper() == "HEADER.FIELDS.NOT":
                        g = HeaderGenerator(
                            fp, headers=section[0][1], skip=True
                        )
                    else:
                        raise BadSection(
                            "Section value must be either "
                            "HEADER.FIELDS or HEADER.FIELDS.NOT, "
                            "not: %s" % section[0][0]
                        )
                else:
                    g = HeaderGenerator(fp)
                    if (
                        isinstance(section[0], str)
                        and section[0].upper() == "MIME"
                    ):
                        # XXX just use the generator as it is for MIME.. I know
                        # this is not quite right in that it will accept more
                        # then it should, but it will otherwise work.
                        #
                        pass
                    elif (
                        isinstance(section[0], str)
                        and section[0].upper() == "HEADER"
                    ):
                        # if the content type is message/rfc822 then to
                        # get the headers we need to use the first
                        # sub-part of this message.
                        #
                        if (
                            msg.is_multipart()
                            and msg.get_content_type() == "message/rfc822"
                        ):
                            msg = msg.get_payload(0)
                    else:
                        self.log.warn(
                            "body: Unexpected section[0] value: %s"
                            % repr(section)
                        )
                        raise BadSection(
                            "%s: Unexpected section value: %s"
                            % (str(self), repr(section[0]))
                        )
            elif isinstance(section[0], int):
                # We have an integer sub-section. This means that we
                # need to pull apart the message (it MUST be a
                # multi-part message) and pass to a recursive
                # invocation of this function the sub-part and the
                # section list (with the top element of the section
                # list removed.)
                #
                if not msg.is_multipart():
                    raise BadSection(
                        "Message does not contain subsection %d "
                        "(section list: %s" % section[0],
                        section,
                    )
                try:
                    msg = msg.get_payload(section[0] - 1)
                except TypeError:
                    raise BadSection(
                        "Message does not contain subsection %d "
                        "(section list: %s)" % section[0],
                        section,
                    )
                return self.body(msg, section[1:])

            # We should have a generator that will give us the text
            # that we want. If we do not then we were not able to find
            # an appropriate section to parse.
            #
            if g is None:
                raise BadSection(
                    "Section selector '%s' can not be parsed" % section
                )
            g.flatten(msg)
            msg_text = fp.getvalue()

        # We have our message text we need to return to our caller.
        # truncate if it we also have a 'partial' defined.
        #
        # Convert \n in to \r\n
        #
        msg_text = email.utils.fix_eols(msg_text)
        if self.partial:
            msg_text = msg_text[self.partial[0] : self.partial[1]]

        # We return all of these body sections as a length prefixed
        # IMAP string. Also run the message text through the wringer
        # converter it through unicode via utf8 back in to bytes
        # (since we are sending messages over the network we want
        # stuff a bytes as much as possible.)
        #
        # msg_text = to_bytes(msg_text.decode("utf8", "replace")) XXX py2 way
        msg_text = msg_text.encode(encoding="UTF-8")
        return "{%d}\r\n%s" % (len(msg_text), msg_text)

    #######################################################################
    #
    def envelope(self, msg):
        """
        Get the envelope structure of the message as a list, in a defined
        order.

        Any fields that we can not determine the value of are NIL.

        XXX This does not need to be an instance method..

        """
        result = []

        from_field = ""
        for field in (
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
        ):
            # 'reply-to' and 'sender' are copied from the 'from' field
            # if they are not explicitly defined.
            #
            if field in ("sender", "reply-to") and field not in msg:
                result.append(from_field)
                continue

            # If a field is not in the message it is nil.
            #
            if field not in msg:
                result.append("NIL")
                continue

            # The from, sender, reply-to, to, cc, and bcc fields are
            # parenthesized lists of address structures.
            #
            if field in ("from", "sender", "reply-to", "to", "cc", "bcc"):
                addrs = email.Utils.getaddresses([msg[field]])
                if len(addrs) == 0:
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
                        one_addr.append('"%s"' % name)

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
                            one_addr.append('"%s"' % mbox_name)
                            one_addr.append('"%s"' % host_name)
                        else:
                            one_addr.append('"%s"' % paddr)
                            one_addr.append('"NIL"')
                    else:
                        one_addr.append("NIL")
                    addr_list.append("(%s)" % " ".join(one_addr))
                result.append("(%s)" % "".join(addr_list))

                # We stash the from field because we may need it for sender
                # and reply-to
                #
                if field == "from":
                    from_field = result[-1]
            else:
                result.append('"%s"' % msg[field])
        return "(%s)" % " ".join(result)

    ##################################################################
    #
    def body_languages(self, msg):
        """
        Find the language related headers in the message and return a
        string suitable for the 'body language' element of a
        bodystructure reply.

        Arguments:
        - `msg`: the message we are looking in..
        """
        # We want all of the header that end in "-language"
        #
        langs = []
        for hdr, value in list(msg.items()):
            if hdr[-9:].lower() != "-language":
                continue
            if "," in value:
                langs.extend(value.split(","))
            elif ";" in value:
                langs.extend(value.split(";"))
            else:
                langs.append(value)

        if len(langs) == 0:
            return "NIL"
        elif len(langs) == 1:
            return '"%s"' % langs[0]
        else:
            return "(%s)" % " ".join([str(x).strip() for x in langs])

    ##################################################################
    #
    def body_location(self, msg):
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
    def body_parameters(self, msg):
        """

         body parameter parenthesized list
            A parenthesized list of attribute/value pairs [e.g., (`foo`
            `bar` `baz` `rag`) where `bar` is the value of `foo` and
            `rag` is the value of `baz`] as defined in [MIME-IMB].

        Arguments:
        - `msg`:
        """
        if msg.get_params() is None:
            return "NIL"

        msg_params = []
        for k, v in msg.get_params():
            if v == "":
                continue
            msg_params.append('"%s"' % k.upper())
            msg_params.append('"%s"' % v)
        if len(msg_params) > 0:
            return "(%s)" % " ".join(msg_params)
        else:
            return "NIL"

    #######################################################################
    #
    def body_disposition(self, msg):
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
        # If we have a content-disposition
        #
        if "content-disposition" in msg:
            # XXX We are hard coding what we assume will be in
            # XXX content-disposition. This is doomed to eventual failure.
            #
            cd = msg.get_params(header="content-disposition")

            # if the content disposition does not have parameters then just
            # put 'NIL' for the parameter list, otherwise we have a list of
            # key/value pairs for the disposition list.
            #
            if len(cd) == 1:
                return '("%s" NIL)' % cd[0][0].upper()
            else:
                cdpl = []
                for k, v in cd[1:]:
                    cdpl.extend(['"%s" "%s"' % (k.upper(), v)])
                return '("%s" (%s))' % (cd[0][0].upper(), " ".join(cdpl))
        else:
            return "NIL"

    #######################################################################
    #
    def bodystructure(self, msg):
        """
        XXX NOTE: WE need to not send extension data if self.ext_data is False.
        """

        if msg.is_multipart():
            # If the message is a multipart message then the bodystructure
            # for this multipart is a parenthesized list.
            #
            # The list begins with a parenthesized list that is the
            # bodystructure for each sub-part.
            #
            # Then we have as an imap string the sub-type of the
            # multipart message (ie: 'mixed' 'digest', 'parallel',
            # 'alternative').
            #
            # Following the subtype is the extension data.
            #
            # The extension data appears in the following order:
            #
            # o body parameters as a parenthesized list. The body parameters
            #   are key value pairs (usually things like the multi-part
            #   boundary separator string.)
            #
            # o body disposition: NIL or A parenthesized list, consisting of
            #   a disposition type string followed by a parenthesized list of
            #   disposition attribute/value pairs.  The disposition type and
            #   attribute names will be defined in a future standards-track
            #   revision to [DISPOSITION]. NOTE: This means we always return
            #   NIL as disposition types have not been defined.
            #
            # o body language: A string or parenthesized list giving the body
            #   language value as defined in [LANGUAGE-TAGS].
            #   NOTE: This is also NIL as these are not defined either.
            #
            sub_parts = []
            for sub_part in msg.get_payload():
                sub_parts.append(self.bodystructure(sub_part))

            # If we are NOT supposed to return extension data (ie:
            # doing a 'body' not a 'bodystructure' then we have
            # everything we need to return a result.
            #
            if not self.ext_data:
                return '(%s "%s")' % (
                    "".join(sub_parts),
                    msg.get_content_subtype(),
                )
            #                                      msg.get_content_subtype().upper())

            # Otherwise this is a real 'bodystructure' fetch and we need to
            # return the extension data as well.
            #
            # The extension data of a multipart body part are in the
            # following order
            #
            # body parameter parenthesized list
            #    A parenthesized list of attribute/value pairs [e.g., ("foo"
            #    "bar" "baz" "rag") where "bar" is the value of "foo", and
            #    "rag" is the value of "baz"] as defined in [MIME-IMB].
            #
            # body disposition
            #    A parenthesized list, consisting of a disposition type
            #    string, followed by a parenthesized list of disposition
            #    attribute/value pairs as defined in [DISPOSITION].
            #
            # body language
            #    A string or parenthesized list giving the body language
            #    value as defined in [LANGUAGE-TAGS].
            #
            # body location
            #    A string list giving the body content URI as defined in
            #    [LOCATION].
            #
            return '(%s "%s" %s %s %s %s)' % (
                "".join(sub_parts),
                # msg.get_content_subtype().upper(),
                msg.get_content_subtype(),
                self.body_parameters(msg),
                self.body_disposition(msg),
                self.body_languages(msg),
                self.body_location(msg),
            )

        # Otherwise, we figure out the bodystructure of this message
        # part and return that as a string in parentheses.
        #
        # Consult the FETCH response section of rfc2060 for the gory
        # details of how and why the list is formatted this way.
        #
        result = []

        # Body type and sub-type
        #
        # result.append('"%s"' % msg.get_content_maintype().upper())
        # result.append('"%s"' % msg.get_content_subtype().upper())
        result.append('"%s"' % msg.get_content_maintype())
        result.append('"%s"' % msg.get_content_subtype())

        # Parenthesized list of the message parameters as a list of
        # key followed by value.
        #
        result.append(self.body_parameters(msg))

        # The body id (what is this? none of our message from a test
        # IMAP server ever set this.
        #
        result.append("NIL")

        # Body description.
        # diito as body id.. noone seems to use this.
        #
        result.append("NIL")

        # The body encoding, from the 'content transfer-encoding' header.
        #
        if "content-transfer-encoding" in msg:
            result.append('"%s"' % msg["content-transfer-encoding"].upper())
        else:
            result.append('"7bit"')
            # result.append('"7BIT"')

        # Body size.. length of the payload string.
        #
        # NOTE: The message payload as returned by the email module has '\n's
        # instead of '\n\r' for newlines. This is probably just coming from
        # the file system directly, but I believe that we need to convert
        # this to \r\n and this needs to figure in our calculation of the
        # number of octets in the message.
        #
        payload = msg.get_payload()
        result.append(str(len(email.utils.fix_eols(payload))))
        # result.append(str(len(payload)))

        # Now come the variable fields depending on the maintype/subtype
        # of this message.
        #
        if msg.get_content_type() == "message/rfc822":
            # envelope structure
            # body structure
            # size in text lines of encapsulated message
            result.append(self.envelope(msg))
            result.append(self.bodystructure(payload))
            result.append(str(len(payload.splitlines())))
        elif msg.get_content_maintype() == "text":
            # size in text lines of the body.
            #
            result.append(str(len(payload.splitlines())))

        # If we are not supposed to return extension data then we are done here
        #
        if not self.ext_data:
            return "(%s)" % " ".join(result)

        # Now we have the message body extension data.
        #
        # The MD5 of the payload
        # XXX Dovecot does not supply this so we will skip this too.
        # result.append('"%s"' % hashlib.md5(payload).hexdigest())
        #
        result.append("NIL")

        # body disposition
        #    A parenthesized list, consisting of a disposition type
        #    string, followed by a parenthesized list of disposition
        #    attribute/value pairs as defined in [DISPOSITION].
        #
        result.append(self.body_disposition(msg))

        # And body language..
        #
        result.append(self.body_languages(msg))

        # and body location..
        #
        result.append(self.body_location(msg))

        # Convert our result in to a parentheses list.
        #
        return "(%s)" % " ".join(result)
