"""
Fetch.. the part that gets various bits and pieces of messages.
"""

from email import message_from_bytes, message_from_string

# System imports
#
from email.generator import Generator
from email.policy import SMTP, default
from io import StringIO

# 3rd party imports
#
import pytest

# Project imports
#
from ..generator import msg_as_string, msg_headers_as_string
from .conftest import PROBLEMATIC_EMAIL_MSG_KEYS, STATIC_EMAIL_MSG_KEYS


####################################################################
#
def test_simple_email_text_generator_no_headers(email_factory):
    for _ in range(5):
        msg = email_factory()
        msg_text = msg_as_string(msg, headers=False)

        # An email message has a bunch of lines as a header and then a two line
        # break. After those two lines is the message body. We use this to
        # compare an RFC822 message from the default generator with our
        # sub-class that can skip headers. NOTE: rfc822 emails have `\r\n` as
        # their line ends.
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
@pytest.mark.parametrize("msg_key", STATIC_EMAIL_MSG_KEYS)
def test_static_email_text_generator_no_headers(
    msg_key, static_email_factory_bytes
):

    msg = message_from_bytes(
        static_email_factory_bytes(msg_key), policy=default
    )
    msg_text = msg_as_string(msg, headers=False)

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
@pytest.mark.parametrize("msg_key", STATIC_EMAIL_MSG_KEYS)
def test_static_email_text_generator_headers(
    msg_key, static_email_factory_bytes
):
    """
    A message with headers is the same as the default generator with
    policy=SMTP.
    """
    msg = message_from_bytes(
        static_email_factory_bytes(msg_key), policy=default
    )
    msg_text = msg_as_string(msg, headers=True)

    fp = StringIO()
    g = Generator(fp, mangle_from_=False, policy=SMTP)
    g.flatten(msg)
    rfc822_text = fp.getvalue()

    assert msg_text == rfc822_text


####################################################################
#
@pytest.mark.parametrize("msg_key", STATIC_EMAIL_MSG_KEYS)
def test_static_email_header_generator_all_headers(
    msg_key, static_email_factory_bytes
):

    msg = message_from_bytes(
        static_email_factory_bytes(msg_key), policy=default
    )
    headers = msg_headers_as_string(msg)

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


####################################################################
#
def test_header_generator_some_headers(lots_of_headers_email):
    """
    Test selective getting of headers.
    """
    msg = message_from_string(lots_of_headers_email, policy=default)

    headers = msg_headers_as_string(
        msg, ("to", "from", "SuBjEct", "Date"), skip=False
    )

    assert (
        headers
        == 'From: jang.abcdef@xyzlinu <jang.abcdef@xyzlinux12345678.it>\r\nTo: "jang12@linux12.org.new" <jang12@linux12.org.new>\r\nSubject: R: R: R: I: FR-selca LA selcaE\r\nDate: Wed, 15 Nov 2017 14:16:14 +0000\r\n\r\n'
    )


####################################################################
#
def test_header_generator_skip_headers(lots_of_headers_email):
    """
    Test selective getting of headers.
    """
    msg = message_from_string(lots_of_headers_email, policy=default)

    # Going to skip most of the headers!
    to_skip = [
        "X-Assp-ID",
        "X-Assp-Session",
        "X-Assp-Version",
        "X-Assp-Delay",
        "X-Assp-Message-Score",
        "X-Assp-IP-Score",
        "X-Assp-Message-Score",
        "X-Original-Authentication-Results",
        "X-Assp-Message-Score",
        "X-Assp-IP-Score",
        "X-Assp-Message-Score",
        "X-Assp-Message-Score",
        "X-Assp-DKIM",
        "X-MS-Has-Attach",
        "X-MS-TNEF-Correlator",
        "x-originating-ip",
        "x-ms-publictraffictype",
        "x-microsoft-exchange-diagnostics",
        "x-ms-exchange-antispam-srfa-diagnostics"
        "x-ms-office365-filtering-correlation-id",
        "x-microsoft-antispam",
        "x-ms-traffictypediagnostic",
        "x-microsoft-antispam-prvs",
        "x-exchange-antispam-report-test",
        "x-exchange-antispam-report-cfa-test",
        "x-forefront-prvs",
        "x-forefront-antispam-report",
        "received-spf",
        "spamdiagnosticoutput",
        "spamdiagnosticmetadata",
        "X-Priority",
        "X-MSMail-Priority",
        "X-Mailer" "X-MimeOLE",
        "X-Antivirus",
        "X-Antivirus-Status",
        "X-UIDL",
        "X-Antivirus",
        "X-Antivirus-Status",
        "x-ms-EXCHANGE-ANTISPAM-srfa-diagnostics"
        "x-ms-office365-FILTERING-correlation-id",
        "X-MIMEOLE",
    ]

    expected = """Return-Path: <jang.abcdef@xyzlinux12345678.it>\r
Delivered-To: jang12@linux12.org.new\r
Received: (qmail 21619 invoked from network); 15 Nov 2017 14:16:18 -0000\r
Received: from unknown (HELO EUR01-HE1-obe.outbound.protection.outlook.com)\r
 (80.68.177.35)  by  with SMTP; 15 Nov 2017 14:16:18 -0000\r
Received: from mail-he1eur01on0133.outbound.protection.outlook.com\r
\t([104.47.0.133] helo=EUR01-HE1-obe.outbound.protection.outlook.com) by\r
\tmyassp01.mynet.it with SMTP (2.5.5); 15 Nov 2017 15:16:20 +0100\r
DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed;\r
 d=CMMSRL.onmicrosoft.com; s=selector1-cmmlaser-it;\r
 h=From:Date:Subject:Message-ID:Content-Type:MIME-Version;\r
 bh=JmZzBMD0RLaOTuqX/VlM86EEKHsfeOF0B0kBWE4fKBY=; =?utf-8?q?b=3Dh65Qop22nh21?=\r
 =?utf-8?q?H30A/T/T47dDaCkb70hySSaJfJCzh+0E2A41BTqlUT7Y3c80Kf6zc5Totg4Kmuub2?=\r
 =?utf-8?q?P8r/Fj30rIiQP5EXW+/caFvHtXEQjZXeuWYRfBweASqK5/1ClHkY3SBgnw3dEuAhl?=\r
 =?utf-8?q?IDzid6M/5YxuJqzn6d/mKvmjV2Ju0=3D?=\r
Received: from AM4PR01MB1444.eurprd01.prod.exchangelabs.com (10.164.76.26) by\r
 AM4PR01MB1442.eurprd01.prod.exchangelabs.com (10.164.76.24) with Microsoft\r
 SMTP Server (version=TLS1_2,\r
 cipher=TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA384_P256) id 15.20.218.12; Wed, 15\r
 Nov 2017 14:16:14 +0000\r
Received: from AM4PR01MB1444.eurprd01.prod.exchangelabs.com\r
 ([fe80::7830:c66f:eaa8:e3dd]) by AM4PR01MB1444.eurprd01.prod.exchangelabs.com\r
 ([fe80::7830:c66f:eaa8:e3dd%14]) with mapi id 15.20.0218.015; Wed, 15 Nov\r
 2017 14:16:14 +0000\r
From: jang.abcdef@xyzlinu <jang.abcdef@xyzlinux12345678.it>\r
To: "jang12@linux12.org.new" <jang12@linux12.org.new>\r
Subject: R: R: R: I: FR-selca LA selcaE\r
Thread-Topic: R: R: I: FR-selca LA selcaE\r
Thread-Index: =?utf-8?q?AdNST+6DXK4xfZYaRzuyUbaIacENgAHGVF+AAACaRUAAAhGDmgAA?=\r
 =?utf-8?q?St6QACm+BjkA/3MzkAAAPw7yAAA7j6A=3D?=\r
Date: Wed, 15 Nov 2017 14:16:14 +0000\r
Message-ID: <AM4PR01MB1444B3F21AE7DA9C8128C28FF7290@AM4PR01MB1444.eurprd01.prod.exchangelabs.com>\r
References: =?utf-8?q?=3CAM4PR01MB1444920F2AF5B6F4856FEA13F7290=40AM4PR01MB1?=\r
 =?utf-8?q?444=2Eeurprd01=2Eprod=2Eexchangelabs=2Ecom=3E_=3C5185e377-81c5-43?=\r
 =?utf-8?q?61-91ba-11d42f4c5cc9=40AM5EUR02FT056=2Eeop-EUR02=2Eprod=2Eprotect?=\r
 =?utf-8?q?ion=2Eoutlook=2Ecom=3E?=\r
In-Reply-To: =?utf-8?q?=3C5185e377-81c5-4361-91ba-11d42f4c5cc9=40AM5EUR02FT0?=\r
 =?utf-8?q?56=2Eeop-EUR02=2Eprod=2Eprotection=2Eoutlook=2Ecom=3E?=\r
Accept-Language: it-IT, en-US\r
Content-Language: it-IT\r
authentication-results: spf=none (sender IP is )\r
 smtp.mailfrom=jang.selca.tubi@linux.selca;\r
x-ms-exchange-antispam-srfa-diagnostics: SSOS;\r
x-ms-office365-filtering-correlation-id: b6800147-d5b4-494e-46ff-08d52c336e1f\r
MIME-Version: 1.0\r
Content-Type: multipart/alternative;\r
\tboundary="----=_NextPart_000_0031_01D36222.8A648550"\r
X-Mailer: Microsoft Outlook Express 6.00.2900.5931\r\n\r\n"""

    headers = msg_headers_as_string(msg, tuple(to_skip), skip=True)
    assert headers == expected


####################################################################
#
@pytest.mark.parametrize("msg_key", PROBLEMATIC_EMAIL_MSG_KEYS)
def test_generator_problematic_email(msg_key, problematic_email_factory_bytes):
    """
    Not all emails can be flattened out of the box without some jiggery
    pokery.  Such as messages that say they are 7-bit us-ascii but are actually
    8-bit latin-1.
    """
    msg = message_from_bytes(
        problematic_email_factory_bytes(msg_key), policy=default
    )
    msg_text = msg_as_string(msg)
    assert msg_text
    msg_hdrs = msg_headers_as_string(msg)
    assert msg_hdrs
