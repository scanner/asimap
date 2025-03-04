"""
The canonical method for creating the text representation of MHMessages for
sending to IMAP clients.

This way all parts of asimapd use the same logic for email text generation and
size.
"""

# system imports
#
import logging
from copy import deepcopy
from email.generator import BytesGenerator, Generator
from email.message import Message
from email.policy import HTTP, SMTP, EmailPolicy, Policy
from io import BytesIO, StringIO
from typing import BinaryIO, List, Optional, TextIO, Tuple

logger = logging.getLogger("asimap.generator")

SMTP_LONG_LINES = SMTP.clone(max_line_length=None)


########################################################################
########################################################################
#
class ASGenerator(BytesGenerator):
    """
    Base class for our bytes generator and bytes header generator.
    """

    ####################################################################
    #
    def __init__(self, outfp: BinaryIO, *args, **kwargs):
        self._mangle_from_: bool
        self.policy: EmailPolicy

        # We want to use SMTP policy because we want RFC822 compliant emails
        # for what we send to the IMAP Client.. but we want to let other
        # options in if specified when this class is instantiated.
        #
        kwargs["policy"] = SMTP if "policy" not in kwargs else kwargs["policy"]

        super().__init__(outfp, *args, **kwargs)

    ####################################################################
    #
    # XXX If we fail to encode the string as ascii with surrogateescape we need
    #     to probably tweak this routine. Right now this is an exact copy of
    #     the BytesGenerator.write() method.
    #
    #     If this works for all of our messages then we can get rid of this
    #     method and use the super class's method.
    #
    def write(self, s):
        self._fp.write(s.encode("ascii", "surrogateescape"))


########################################################################
########################################################################
#
class ASBytesGenerator(ASGenerator):
    """
    Like the BytesGenerator except that it is intended to produce 8bit
    binary output. So no re-encoding to a string is done.
    """

    ####################################################################
    #
    def __init__(
        self, outfp: BinaryIO, *args, render_headers: bool = False, **kwargs
    ):
        super().__init__(outfp, *args, **kwargs)
        self._render_headers = render_headers

    ####################################################################
    #
    def _write_headers(self, msg):
        """
        This method exists so we can determine whether or not to write the
        headers based on the instance variable `_headers`.
        """
        if self._render_headers:
            super()._write_headers(msg)  # type: ignore

    ####################################################################
    #
    def clone(self, fp, render_headers=True):
        """
        When a message is being flattened the Generator is cloned for each
        sub-part. We want all sub-parts to have their headers generated, thus
        the default is `True` for these clones.

        This way with the default being `False` for the top level generator
        means that we have the option of producing a message body without its
        initial headers (but all sub-parts get their headers.)
        """
        return self.__class__(
            fp,
            self._mangle_from_,
            None,
            render_headers=render_headers,
            policy=self.policy,
        )


########################################################################
########################################################################
#
class ASHeaderGenerator(ASGenerator):
    """
    A generator that prints out only headers. If 'skip' is true,
    then headers in the list 'headers' are NOT included in the
    output.

    If skip is False then only headers in the list 'headers' are
    included in the output.

    The default of headers = [] and skip = True will cause all
    headers to be printed.

    NOTE: Headers are compared in a case insensitive fashion so
    'bCc' and 'bCC' and 'bcc' are all the same.
    """

    ####################################################################
    #
    def __init__(
        self,
        outfp: BinaryIO,
        *args,
        headers: Optional[Tuple[str, ...]] = None,
        skip: bool = True,
        **kwargs,
    ):
        self._NL: str
        self._fp: BinaryIO
        headers = [] if headers is None else headers

        super().__init__(outfp, *args, **kwargs)

        self._headers = [x.lower() for x in headers]
        self._skip = skip

    ####################################################################
    #
    def clone(self, fp):
        return self.__class__(
            fp,
            self._mangle_from_,
            None,
            headers=self._headers,
            skip=self._skip,
            policy=self.policy,
        )

    ####################################################################
    #
    def _write(self, msg):
        """
        Just like the original _write in the Generator class except
        that we do is write the headers.

        Write the headers.  First we see if the message object wants to
        handle that itself.  If not, we'll do it generically.
        """
        # We can't write the headers yet because of the following scenario:
        # say a multipart message includes the boundary string somewhere in
        # its body.  We'd have to calculate the new boundary /before/ we write
        # the headers so that we can write the correct Content-Type:
        # parameter.
        #
        # The way we do this, so as to make the _handle_*() methods simpler,
        # is to cache any subpart writes into a buffer.  Then we write the
        # headers and the buffer contents.  That way, subpart handlers can
        # Do The Right Thing, and can still modify the Content-Type: header if
        # necessary.
        oldfp = self._fp
        try:
            self._munge_cte = None
            self._fp = self._new_buffer()
            self._dispatch(msg)
        finally:
            self._fp = oldfp
            munge_cte = self._munge_cte
            del self._munge_cte
        # If we munged the cte, copy the message again and re-fix the CTE.
        if munge_cte:
            msg = deepcopy(msg)
            # Preserve the header order if the CTE header already exists.
            if msg.get("content-transfer-encoding") is None:
                msg["Content-Transfer-Encoding"] = munge_cte[0]
            else:
                msg.replace_header("content-transfer-encoding", munge_cte[0])
            msg.replace_header("content-type", munge_cte[1])
        # Write the headers.  Do not check the message to see if it has its own
        # method for writing headers.
        #
        self._write_headers(msg)

        # And since we are only writing headers do not write the rest of the
        # mssage.
        #
        # self._fp.write(sfp.getvalue())

    ####################################################################
    #
    def _write_headers(self, msg):
        """
        Like the original Generator's `_write_headers`, except we may be
        asked to only send certain headers or skip certain headers.

        NOTE: from RFC3501:
           Subsetting does not exclude the [RFC-2822] delimiting blank line
           between the header and the body; the blank line is included in all
           header fetches, except in the case of a message which has no body
           and no blank line.

        So we always include an extra `linesep` at the end of the headers we
        are returning.  We expect all messages to have a body so we are not
        going to worry about the case where there is no body and no blank line.
        """
        # This is almost the same as the string version, except for handling
        # strings with 8bit bytes.
        for h, v in msg.raw_items():
            # Determine if we are supposed to skip this header or not.
            #
            hdr = h.lower()
            if self._skip and hdr in self._headers:
                continue
            if not self._skip and hdr not in self._headers:
                continue

            self._fp.write(self.policy.fold_binary(h, v))
        # A blank line always separates headers from body
        # and this is always included in our response to IMAP clients.
        #
        self.write(self._NL)


############################################################################
#
class TextGenerator(Generator):
    def __init__(self, outfp: TextIO, *args, headers: bool = False, **kwargs):
        """
        This is a special purpose message generator.

        We need a generator that can be used to represent the 'TEXT'
        fetch attribute. When used on a multipart message it does not
        render the headers of the message, but renders the headers of
        every sub-part.

        When used on a message that is not a multipart it just renders
        the body.

        We do this by having the 'clone()' method basically reverse
        whether or not we should print the headers, and the _write()
        method looks at that instance variable to decide if it should
        print the headers or not.

        outfp is the output file-like object for writing the message to.  It
        must have a write() method.

        """
        self._mangle_from_: bool
        self.policy: Policy

        # We want to use SMTP policy because we want RFC822 compliant emails
        # for what we send to the IMAP Client.. but we want to let other
        # options in if specified when this class is instantiated.
        #
        kwargs["policy"] = SMTP if "policy" not in kwargs else kwargs["policy"]

        super().__init__(outfp, *args, **kwargs)
        self._headers = headers

    ####################################################################
    #
    def _write_headers(self, msg):
        """
        This method exists so we can determine whether or not to write the
        headers based on the instance variable `_headers`.
        """
        if self._headers:
            super()._write_headers(msg)  # type: ignore

    ####################################################################
    #
    def clone(self, fp, headers=True):
        """
        When a message is being flattened the Generator is cloned for each
        sub-part. We want all sub-parts to have their headers generated, thus
        the default is `True` for these clones.

        This way with the default being `False` for the top level generator
        means that we have the option of producing a message body without its
        initial headers (but all sub-parts get their headers.)
        """
        return self.__class__(
            fp, self._mangle_from_, None, headers=headers, policy=self.policy
        )


############################################################################
#
class HeaderGenerator(Generator):
    """
    A generator that prints out only headers. If 'skip' is true,
    then headers in the list 'headers' are NOT included in the
    output.

    If skip is False then only headers in the list 'headers' are
    included in the output.

    The default of headers = [] and skip = True will cause all
    headers to be printed.

    NOTE: Headers are compared in a case insensitive fashion so
    'bCc' and 'bCC' and 'bcc' are all the same.
    """

    ####################################################################
    #
    def __init__(
        self,
        outfp: TextIO,
        *args,
        headers: Optional[List[str]] = None,
        skip: bool = True,
        **kwargs,
    ):
        self._mangle_from_: bool
        self.policy: Policy
        self._NL: str
        headers = [] if headers is None else headers

        # We want to use SMTP policy because we want RFC822 compliant emails
        # for what we send to the IMAP Client.. but we want to let other
        # options in if specified when this class is instantiated.
        #
        kwargs["policy"] = SMTP if "policy" not in kwargs else kwargs["policy"]

        Generator.__init__(self, outfp, *args, **kwargs)

        self._headers = [x.lower() for x in headers]
        self._skip = skip

    ####################################################################
    #
    def clone(self, fp):
        return self.__class__(
            fp,
            self._mangle_from_,
            None,
            headers=self._headers,
            skip=self._skip,
            policy=self.policy,
        )

    ####################################################################
    #
    def _write(self, msg):
        """
        Just like the original _write in the Generator class except
        that we do is write the headers.

        Write the headers.  First we see if the message object wants to
        handle that itself.  If not, we'll do it generically.
        """
        # We have to fake write the rest of the message because we need to know
        # the boundary for multipart messages and we might not be able to know
        # that until those parts have been rendered.
        #
        # We can't write the headers yet because of the following scenario:
        # say a multipart message includes the boundary string somewhere in
        # its body.  We'd have to calculate the new boundary /before/ we write
        # the headers so that we can write the correct Content-Type:
        # parameter.
        #
        # The way we do this, so as to make the _handle_*() methods simpler,
        # is to cache any subpart writes into a buffer.  The we write the
        # headers and the buffer contents.  That way, subpart handlers can
        # Do The Right Thing, and can still modify the Content-Type: header if
        # necessary.
        #
        # NOTE: The new buffer (`sfp`) is going to be discarded since we are
        #       ONLY writing the headers.
        oldfp = self._fp
        try:
            self._munge_cte = None
            self._fp = self._new_buffer()
            self._dispatch(msg)
        finally:
            self._fp = oldfp
            munge_cte = self._munge_cte
            del self._munge_cte
        # If we munged the cte, copy the message again and re-fix the CTE.
        if munge_cte:
            msg = deepcopy(msg)
            # Preserve the header order if the CTE header already exists.
            if msg.get("content-transfer-encoding") is None:
                msg["Content-Transfer-Encoding"] = munge_cte[0]
            else:
                msg.replace_header("content-transfer-encoding", munge_cte[0])
            msg.replace_header("content-type", munge_cte[1])

        # In the original `Generator` it supported messages that have their own
        # `_write_headers` method, but we are going to ignore that. I do not
        # think any of the Message subclasses we will deal with will have their
        # own `_write_headers` method and we need to use ours so we can do the
        # HEADER.FIELDS inclusion/exclusion rules.
        #
        self._write_headers(msg)

    ####################################################################
    #
    def _write_headers(self, msg):
        """
        Like the original Generator's `_write_headers`, except we may be
        asked to only send certain headers or skip certain headers.

        NOTE: from RFC3501:
           Subsetting does not exclude the [RFC-2822] delimiting blank line
           between the header and the body; the blank line is included in all
           header fetches, except in the case of a message which has no body
           and no blank line.

        So we always include an extra `linesep` at the end of the headers we
        are returning.  We expect all messages to have a body so we are not
        going to worry about the case where there is no body and no blank line.
        """
        for h, v in msg.raw_items():
            # Determine if we are supposed to skip this header or not.
            #
            hdr = h.lower()
            if self._skip and hdr in self._headers:
                continue
            if not self._skip and hdr not in self._headers:
                continue
            self.write(self.policy.fold(h, v))
        # A blank line always separates headers from body
        # and this is always included in our response to IMAP clients.
        #
        self.write(self._NL)


####################################################################
#
def _msg_as_string(msg: Message, headers: bool = True):
    """
    Instead of having to create the StringIO, TextGenerator, call flatten
    and return the contents fo the StringIO we wrap all those in this
    convenience function.
    """
    # We try two different policies. Both the SMTP and SMTPUTF8 policies fail
    # to generate text if a header, say the subject, is split across more than
    # one line (folded) and is encoded as a UTF8 string.
    #
    # Not folding that line avoids the problem. Since these text
    # representations are for presenting to an IMAP client and not sending over
    # the wire this is okay.
    #
    try:
        failed = False
        fp = StringIO()
        g = TextGenerator(fp, mangle_from_=False, headers=headers)
        g.flatten(msg)
    except UnicodeEncodeError:
        failed = True

    if failed:
        fp = StringIO()
        g = TextGenerator(
            fp, mangle_from_=False, headers=headers, policy=SMTP_LONG_LINES
        )
        g.flatten(msg)

    msg_str = fp.getvalue()
    msg_str = msg_str if msg_str.endswith("\r\n") else msg_str + "\r\n"
    return msg_str


####################################################################
#
def msg_as_string(msg: Message, headers: bool = True) -> str:
    return _msg_as_string(msg, headers=headers)


####################################################################
#
def _msg_as_bytes(msg: Message, render_headers: bool = True) -> bytes:
    # We try two different policies. Both the SMTP and SMTPUTF8 policies fail
    # to generate text if a header, say the subject, is split across more than
    # one line (folded) and is encoded as a UTF8 string. I believe this is a
    # bug in the header registry class in that it seems to break handling
    # refold's of UTF8 encoded header values.
    #
    # So, force it to not fold by saying header lines have no max length. Since
    # these text representations are for presenting to an IMAP client and not
    # sending over the wire this is okay.
    #
    try:
        failed = False
        fp = BytesIO()
        g = ASBytesGenerator(
            fp, mangle_from_=False, render_headers=render_headers
        )
        g.flatten(msg)
    except UnicodeEncodeError:
        failed = True

    if failed:
        failed = False
        fp = BytesIO()
        g = ASBytesGenerator(
            fp, mangle_from_=False, render_headers=render_headers, policy=HTTP
        )
        g.flatten(msg)

    msg_bytes = fp.getvalue()
    msg_bytes = (
        msg_bytes if msg_bytes.endswith(b"\r\n") else msg_bytes + b"\r\n"
    )
    return msg_bytes


####################################################################
#
def msg_as_bytes(msg: Message, render_headers: bool = True) -> bytes:
    return _msg_as_bytes(msg, render_headers=render_headers)


####################################################################
#
def get_msg_size(msg: Message, render_headers: bool = True) -> int:
    """
    We need to know the size of a message in octets in several different
    contexts. Our TextGenerator is what we use to flatten messages for sending
    to IMAP clients, so we want to also use it to be the canonical description
    of a message's size.
    """
    msg_bytes = msg_as_bytes(msg, render_headers=render_headers)
    return len(msg_bytes)


####################################################################
#
def msg_headers_as_bytes(
    msg: Message,
    headers: Optional[Tuple[str, ...]] = None,
    skip: bool = True,
) -> bytes:
    """
    Convenience method to generate just the headers for a message.

    If `headers` and `skip=False`is specified this is the list of headers to
    return.

    If `skip` is True (and `headers` must be provided) the headers returned
    are the ones that are NOT in the list of headers.
    """
    # We try two different policies. Both the SMTP and SMTPUTF8 policies fail
    # to generate text if a header, say the subject, is split across more than
    # one line (folded) and is encoded as a UTF8 string. I believe this is a
    # bug in the header registry class in that it seems to break handling
    # refold's of UTF8 encoded header values.
    #
    # So, force it to not fold by saying header lines have no max length. Since
    # these text representations are for presenting to an IMAP client and not
    # sending over the wire this is okay.
    #
    try:
        failed = False
        fp = BytesIO()
        g = ASHeaderGenerator(
            fp, mangle_from_=False, headers=headers, skip=skip
        )
        g.flatten(msg)
    except UnicodeEncodeError:
        failed = True

    if failed:
        failed = False
        fp = BytesIO()
        g = ASHeaderGenerator(
            fp, mangle_from_=False, headers=headers, skip=skip, policy=HTTP
        )
        g.flatten(msg)

    return fp.getvalue()


####################################################################
#
def msg_headers_as_string(
    msg: Message,
    headers: Optional[Tuple[str, ...]] = None,
    skip: bool = True,
) -> str:
    """
    Instead of having to create the StringIO, TextGenerator, call flatten
    and return the contents fo the StringIO we wrap all those in this
    convenience function.

    If `headers` and `skip=False`is specified this is the list of headers to
    return.

    If `skip` is True (and `headers` must be provided) the headers returned
    are the ones that are NOT in the list of headers.
    """
    # We try two different policies. Both the SMTP and SMTPUTF8 policies fail
    # to generate text if a header, say the subject, is split across more than
    # one line (folded) and is encoded as a UTF8 string.
    #
    # Not folding that line avoids the problem. Since these text
    # representations are for presenting to an IMAP client and not sending over
    # the wire this is okay.
    #
    hdrs = list(headers) if headers else None
    try:
        failed = False
        fp = StringIO()
        g = HeaderGenerator(fp, mangle_from_=False, headers=hdrs, skip=skip)
        g.flatten(msg)
    except UnicodeEncodeError:
        failed = True

    if failed:
        fp = StringIO()
        g = HeaderGenerator(
            fp,
            mangle_from_=False,
            headers=hdrs,
            skip=skip,
            policy=SMTP_LONG_LINES,
        )
        g.flatten(msg)

    return fp.getvalue()
