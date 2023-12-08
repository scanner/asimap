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
def test_text_generator_no_headers(email_factory, static_email_factory):
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

    for orig_msg_text in static_email_factory:
        msg = MHMessage(orig_msg_text)
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
def test_text_generator_headers(email_factory, static_email_factory):
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

    for orig_msg_text in static_email_factory:
        msg = MHMessage(orig_msg_text)
        msg_text = msg_as_string(msg, headers=True)
        fp = StringIO()
        g = Generator(fp, mangle_from_=False, policy=SMTP)
        g.flatten(msg)
        rfc822_text = fp.getvalue()

        assert msg_text == rfc822_text


####################################################################
#
def test_header_generator_all_headers(email_factory, static_email_factory):
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

    for orig_msg_text in static_email_factory:
        msg = MHMessage(orig_msg_text)
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
    msg = MHMessage(lots_of_headers_email)

    headers = msg_headers_as_string(
        msg, ["to", "from", "SuBjEct", "Date"], skip=False
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
    msg = MHMessage(lots_of_headers_email)

    # Goign to skip most of the headers!
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

    expected = [
        "Return-Path: <jang.abcdef@xyzlinux12345678.it>",
        "Delivered-To: jang12@linux12.org.new",
        "Received: (qmail 21619 invoked from network); 15 Nov 2017 14:16:18 -0000",
        "Received: from unknown (HELO EUR01-HE1-obe.outbound.protection.outlook.com)",
        " (80.68.177.35)  by  with SMTP; 15 Nov 2017 14:16:18 -0000",
        "Received: from mail-he1eur01on0133.outbound.protection.outlook.com",
        "\t([104.47.0.133] helo=EUR01-HE1-obe.outbound.protection.outlook.com) by",
        "\tmyassp01.mynet.it with SMTP (2.5.5); 15 Nov 2017 15:16:20 +0100",
        "DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed;",
        " d=CMMSRL.onmicrosoft.com; s=selector1-cmmlaser-it;",
        " h=From:Date:Subject:Message-ID:Content-Type:MIME-Version;",
        " bh=JmZzBMD0RLaOTuqX/VlM86EEKHsfeOF0B0kBWE4fKBY=; =?utf-8?q?b=3Dh65Qop22nh21?=",
        " =?utf-8?q?H30A/T/T47dDaCkb70hySSaJfJCzh+0E2A41BTqlUT7Y3c80Kf6zc5Totg4Kmuub2?=",
        " =?utf-8?q?P8r/Fj30rIiQP5EXW+/caFvHtXEQjZXeuWYRfBweASqK5/1ClHkY3SBgnw3dEuAhl?=",
        " =?utf-8?q?IDzid6M/5YxuJqzn6d/mKvmjV2Ju0=3D?=",
        "Received: from AM4PR01MB1444.eurprd01.prod.exchangelabs.com (10.164.76.26) by",
        " AM4PR01MB1442.eurprd01.prod.exchangelabs.com (10.164.76.24) with Microsoft",
        " SMTP Server (version=TLS1_2,",
        " cipher=TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA384_P256) id 15.20.218.12; Wed, 15",
        " Nov 2017 14:16:14 +0000",
        "Received: from AM4PR01MB1444.eurprd01.prod.exchangelabs.com",
        " ([fe80::7830:c66f:eaa8:e3dd]) by AM4PR01MB1444.eurprd01.prod.exchangelabs.com",
        " ([fe80::7830:c66f:eaa8:e3dd%14]) with mapi id 15.20.0218.015; Wed, 15 Nov",
        " 2017 14:16:14 +0000",
        "From: jang.abcdef@xyzlinu <jang.abcdef@xyzlinux12345678.it>",
        'To: "jang12@linux12.org.new" <jang12@linux12.org.new>',
        "Subject: R: R: R: I: FR-selca LA selcaE",
        "Thread-Topic: R: R: I: FR-selca LA selcaE",
        "Thread-Index: =?utf-8?q?AdNST+6DXK4xfZYaRzuyUbaIacENgAHGVF+AAACaRUAAAhGDmgAA?=",
        " =?utf-8?q?St6QACm+BjkA/3MzkAAAPw7yAAA7j6A=3D?=",
        "Date: Wed, 15 Nov 2017 14:16:14 +0000",
        "Message-ID: <AM4PR01MB1444B3F21AE7DA9C8128C28FF7290@AM4PR01MB1444.eurprd01.prod.exchangelabs.com>",
        "References: =?utf-8?q?=3CAM4PR01MB1444920F2AF5B6F4856FEA13F7290=40AM4PR01MB1?=",
        " =?utf-8?q?444=2Eeurprd01=2Eprod=2Eexchangelabs=2Ecom=3E_=3C5185e377-81c5-43?=",
        " =?utf-8?q?61-91ba-11d42f4c5cc9=40AM5EUR02FT056=2Eeop-EUR02=2Eprod=2Eprotect?=",
        " =?utf-8?q?ion=2Eoutlook=2Ecom=3E?=",
        "In-Reply-To: =?utf-8?q?=3C5185e377-81c5-4361-91ba-11d42f4c5cc9=40AM5EUR02FT0?=",
        " =?utf-8?q?56=2Eeop-EUR02=2Eprod=2Eprotection=2Eoutlook=2Ecom=3E?=",
        "Accept-Language: it-IT, en-US",
        "Content-Language: it-IT",
        "authentication-results: spf=none (sender IP is )",
        " smtp.mailfrom=jang.selca.tubi@linux.selca;",
        "x-ms-exchange-antispam-srfa-diagnostics: SSOS;",
        "x-ms-office365-filtering-correlation-id: b6800147-d5b4-494e-46ff-08d52c336e1f",
        "MIME-Version: 1.0",
        "Content-Type: multipart/alternative;",
        '\tboundary="----=_NextPart_000_0031_01D36222.8A648550"',
        "X-Mailer: Microsoft Outlook Express 6.00.2900.5931",
        "",
        "",
    ]

    headers = msg_headers_as_string(msg, to_skip, skip=True)

    assert headers == "\r\n".join(expected)
