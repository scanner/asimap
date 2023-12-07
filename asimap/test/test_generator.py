"""
Fetch.. the part that gets various bits and pieces of messages.
"""
from email.generator import Generator
from email.policy import SMTP

# System imports
#
from io import StringIO
from mailbox import MHMessage

# Project imports
#
from ..generator import msg_as_string, msg_headers_as_string


####################################################################
#
def test_text_generator_no_headers(email_factory):
    msg = MHMessage(email_factory())
    msg_text = msg_as_string(msg, headers=False)

    # An email message has a bunch of lines as a header and then a two line
    # break. After those two lines is the message body. We use this to compare
    # an RFC822 message from the default generator with our sub-class that can
    # skip headers. NOTE: rfc822 emails have `\r\n` as their line ends.
    #
    fp = StringIO()
    g = Generator(fp, mangle_from_=False, policy=SMTP)
    g.flatten(msg)
    rfc822_text = fp.getvalue()

    # Look for the first occurence of "\r\n" in our rfc822_text. Split the
    # string on that point.
    #
    where = rfc822_text.index("\r\n\r\n") + 4
    body = rfc822_text[where:]

    assert msg_text == body


####################################################################
#
def test_text_generator_headers(email_factory):
    """
    A message with headers is the same as the default generator with
    policy=SMTP.
    """
    msg = MHMessage(email_factory())
    msg_text = msg_as_string(msg, headers=True)

    fp = StringIO()
    g = Generator(fp, mangle_from_=False, policy=SMTP)
    g.flatten(msg)
    rfc822_text = fp.getvalue()

    assert msg_text == rfc822_text


####################################################################
#
def test_header_generator_all_headers(email_factory):
    msg = MHMessage(email_factory())
    headers = msg_headers_as_string(msg)

    # An email message has a bunch of lines as a header and then a two line
    # break. After those two lines is the message body. We use this to compare
    # an RFC822 message from the default generator with our sub-class that can
    # skip headers. NOTE: rfc822 emails have `\r\n` as their line ends.
    #
    fp = StringIO()
    g = Generator(fp, mangle_from_=False, policy=SMTP)
    g.flatten(msg)
    rfc822_text = fp.getvalue()

    # Look for the first occurence of "\r\n" in our rfc822_text. Split the
    # string on that point.
    #
    where = rfc822_text.index("\r\n\r\n") + 4
    rfc822_headers = rfc822_text[:where]

    assert headers == rfc822_headers
