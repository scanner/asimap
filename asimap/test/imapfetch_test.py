#!/usr/bin/env python
#
# Copyright (C) 2007 Eric "Scanner" Luce
#
# File: $Id: imapfetch_test.py 1453 2007-10-29 01:47:26Z scanner $
#
"""
This test module runs the IMAPFetch engine through some of its
paces.
"""

import unittest
import commands
import os.path
import email
import mhlib

from datetime import datetime

# mhimap imports
#
import mhimap.utils
from mhimap.IMAPFetch import FetchAtt
from mhimap.Mailbox import MessageEntry

from mhimap.test.utils import folder_setup

############################################################################   
#
class IMAPFetchTest(unittest.TestCase):

    SEEN_MSGS = [x for x in range(1,90)]
    ANSWERED_MSGS = [x for x in range(1,80)]
    FLAGGED_MSGS = [1,5,8,10,20]
    DELETED_MSGS = [1,2,3,4,5]
    DRAFT_MSGS = [7,10,13,21]
    RECENT_MSGS = [x for x in range(90,100)]

    ########################################################################
    #
    def setUp(self):
        folder_setup(self)

    ########################################################################
    #
    def genericFlagTest(self, msg_num, flags):
        fetch = FetchAtt(FetchAtt.OP_FLAGS)
        result,chg = fetch.fetch(self.messages[msg_num].msg,
                                 self.messages[msg_num])
        self.assertEqual(chg, False)
        self.assertEqual(result, "FLAGS (%s)" % flags)

    ########################################################################
    #
    def testFlags01(self):
        self.genericFlagTest(1, "\Seen \Answered \Flagged \Deleted")

    ########################################################################
    #
    def testFlags02(self):
        self.genericFlagTest(2, "\Seen \Answered \Deleted")

    ########################################################################
    #
    def testFlags03(self):
        self.genericFlagTest(6, "\Seen \Answered")

    ########################################################################
    #
    def testFlags04(self):
        self.genericFlagTest(91, "\Recent")

    ########################################################################
    #
    def testUid(self):
        fetch = FetchAtt(FetchAtt.OP_UID)
        for i in range(1,20):
            result,chg = fetch.fetch(self.messages[i].msg,
                                     self.messages[i])
            self.assertEqual(chg, False)
            self.assertEqual(result, "UID %s" % i)

    ########################################################################
    #
    def genericDateTest(self, msg_num, date_string):
        fetch = FetchAtt(FetchAtt.OP_INTERNALDATE)
        result,chg = fetch.fetch(self.messages[msg_num].msg,
                                 self.messages[msg_num])
        self.assertEqual(chg, False)
        self.assertEqual(result, 'INTERNALDATE "%s"' % date_string)

    ########################################################################
    #
    def testInternalDate01(self):
        self.genericDateTest(91, "Fri, 10 Oct 2003 17:45:31 -0000")

    ########################################################################
    #
    def testInternalDate02(self):
        self.genericDateTest(2, "Mon, 10 Mar 2003 22:39:17 -0000")

    ########################################################################
    #
    def genericEnvelopeTest(self, msg_num, envelope):
        fetch = FetchAtt(FetchAtt.OP_ENVELOPE)
        result,chg = fetch.fetch(self.messages[msg_num].msg,
                                 self.messages[msg_num])
        self.assertEqual(chg, False)
        self.assertEqual(result, 'ENVELOPE (%s)' % envelope)

    ########################################################################
    #
    def testEnvelope01(self):
        self.genericEnvelopeTest(9, '"Mon, 28 Apr 2003 12:48:10 -0700" "for SolThree" (("gruhn" NIL "gruhn" "hwb.com")) (("gruhn" NIL "gruhn" "hwb.com")) (("gruhn" NIL "gruhn" "hwb.com")) (("Scanner Luce" NIL "scanner" "apricot.com")) NIL NIL NIL "<005601c30dbf$1a96a220$0300a8c0@qwest.net>"')

    ########################################################################
    #
    def testEnvelope02(self):
        self.genericEnvelopeTest(14,'"Fri, 13 Jun 2003 23:47:20 -0700" "Sign I can\'t read" (("Brian Williams" NIL "brianwilliams01" "speakeasy.net")) (("Brian Williams" NIL "brianwilliams01" "speakeasy.net")) (("Brian Williams" NIL "brianwilliams01" "speakeasy.net")) ((NIL NIL "scanner" "apricot.com")) NIL NIL NIL "<001301c33240$cd8b4ca0$0200a8c0@viper>"')

    ########################################################################
    #
    def testEnvelope03(self):
        self.genericEnvelopeTest(20,'"Wed, 9 Jul 2003 12:50:02 -0700 (PDT)" "Re: email floundering " (("Curtis Soldano" NIL "curtissoldano" "yahoo.com")) (("Curtis Soldano" NIL "curtissoldano" "yahoo.com")) (("Curtis Soldano" NIL "curtissoldano" "yahoo.com")) ((NIL NIL "scanner" "apricot.com")) NIL NIL "<200307090200.h6920Qne021790@matsubue.apricot.com>" "<20030709195002.77025.qmail@web11607.mail.yahoo.com>"')

    ########################################################################
    #
    def testEnvelope04(self):
        self.genericEnvelopeTest(22,'"Thu, 10 Jul 2003 01:04:08 -0700" "Re: Email server question " (("Keith Rhee" NIL "foxpaws" "apricot.com")) (("Keith Rhee" NIL "foxpaws" "apricot.com")) (("Keith Rhee" NIL "foxpaws" "apricot.com")) (("Eric Luce" NIL "scanner" "apricot.com")) NIL NIL "<200307090729.h697TTne023533@matsubue.apricot.com>" "<BB326C08.143F%foxpaws@apricot.com>"')

    ########################################################################
    #
    def testEnvelope05(self):
        self.genericEnvelopeTest(23,'"Thu, 10 Jul 2003 16:50:49 -0500" "Re: Vienna" (("Ted Lemon" NIL "mellon" "fugue.com")) (("Ted Lemon" NIL "mellon" "fugue.com")) (("Ted Lemon" NIL "mellon" "fugue.com")) (("Carolyn Lee Luce" NIL "clee" "apricot.com")(NIL NIL "april.marine" "nominum.com")(NIL NIL "ted.lemon" "nominum.com")(NIL NIL "tale" "dd.org")) ((NIL NIL "scanner" "apricot.com")(NIL NIL "andrea" "crackpot.com")) NIL "<200307102105.h6AL51ne075477@matsubue.apricot.com>" "<200307101650.49849.mellon@fugue.com>"')

    ########################################################################
    #
    def testEnvelope06(self):
        self.genericEnvelopeTest(24,'"Fri, 11 Jul 2003 03:59:38 -0400" "Re: Vienna" (("David C Lawrence" NIL "tale" "dd.org")) (("David C Lawrence" NIL "tale" "dd.org")) (("David C Lawrence" NIL "tale" "dd.org")) (("Carolyn Lee Luce" NIL "clee" "apricot.com")) ((NIL NIL "april.marine" "nominum.com")(NIL NIL "ted.lemon" "nominum.com")(NIL NIL "scanner" "apricot.com")) NIL "<200307102105.h6AL51ne075477@matsubue.apricot.com>" "<16142.28266.828703.96025@gro.dd.org>"')

    ########################################################################
    #
    def testEnvelope07(self):
        self.genericEnvelopeTest(25,'"Fri, 11 Jul 2003 11:19:41 -0500" "Re: Vienna" (("Ted Lemon" NIL "mellon" "fugue.com")) (("Ted Lemon" NIL "mellon" "fugue.com")) (("Ted Lemon" NIL "mellon" "fugue.com")) (("David C Lawrence" NIL "tale" "dd.org")("Carolyn Lee Luce" NIL "clee" "apricot.com")) ((NIL NIL "april.marine" "nominum.com")(NIL NIL "ted.lemon" "nominum.com")(NIL NIL "scanner" "apricot.com")) NIL "<16142.28266.828703.96025@gro.dd.org>" "<200307111119.41438.mellon@fugue.com>"')

    ########################################################################
    #
    def testEnvelope08(self):
        self.genericEnvelopeTest(26,'"Fri, 11 Jul 2003 14:09:24 -0500" "Re: Vienna" (("Ted Lemon" NIL "mellon" "fugue.com")) (("Ted Lemon" NIL "mellon" "fugue.com")) (("Ted Lemon" NIL "mellon" "fugue.com")) ((NIL NIL "clee" "apricot.com")) (("David C Lawrence" NIL "tale" "dd.org")(NIL NIL "april.marine" "nominum.com")(NIL NIL "ted.lemon" "nominum.com")(NIL NIL "scanner" "apricot.com")) NIL "<200307111824.h6BIOone076960@matsubue.apricot.com>" "<200307111409.24758.mellon@fugue.com>"')

    ########################################################################
    #
    def testEnvelope09(self):
        self.genericEnvelopeTest(61,'"Thu, 4 Sep 2003 19:52:20 -0700 (PDT)" "Re: Tori and Karl on PBS!! :)" (("Chris Nara" NIL "cnara1966" "yahoo.com")) (("Chris Nara" NIL "cnara1966" "yahoo.com")) (("Chris Nara" NIL "cnara1966" "yahoo.com")) (("Luce, Karl" NIL "KLuce" "lear.com")("Almasian Steve (E-mail)" NIL "Steve.Almasian" "intier.com")("Andranik Mesrobian (E-mail)" NIL "andym1" "ix.netcom.com")("Bardallis Pete (Centroidsys) (E-mail)" NIL "pbardallis" "centroidsys.com")("Brian Fechner (E-mail)" NIL "BF31" "daimlerchrysler.com")("Brian Fechner (E-mail 2)" NIL "daskid0" "yahoo.com")("Brown David (E-mail)" NIL "dmbrown" "icubed.com")("Brown David (Work) (E-mail)" NIL "dmb" "fourthriver.com")("Brown Mike (E-mail)" NIL "BeatleSau" "aol.com")("Carolyn Lee Luce (E-mail)" NIL "clee" "apricot.com")("Chrissie (E-mail)" NIL "pelerugrat" "aol.com")("Corrao Mark (E-mail)" NIL "mark.corrao" "valeo.com")("Coury Larry (E-mail)" NIL "lcoury" "fishneave.com")("Daniel Nussbaum (E-mail)" NIL "dann" "alum.mit.edu")("Dean Pierce (E-mail)" NIL "dean_pierce" "together.org")("Diane Snoeyink (E-mail)" NIL "dsnoeyin" "ford.com")("Dianna Sabo (E-mail)" NIL "asmartkitty" "aol.com")("Eileen Concannon (E-mail)" NIL "eileen" "alum.mit.edu")("Eng, Jim (E-mail)" NIL "jimeng" "ameritech.net")("Eric Luce (E-mail)" NIL "scanner" "apricot.com")("Gene Halbrooks (E-mail)" NIL "ehalbroo" "cognex.com")("Diane Gonzales (E-mail)" NIL "gonzales_b" "yahoo.com")("Halder, Tuhin (E-mail 2)" NIL "tuhin" "umich.edu")("Ilana Katz (E-mail)" NIL "ikatz" "monitor.com")("Jackie Schonholtz (E-mail)" NIL "jackie" "lvdi.net")("Jia Liu (E-mail)" NIL "jia.liu" "wl.com")("Kermit Diehl (E-mail)" NIL "kfrogdiehl" "jorsm.com")("Kris Luce (E-mail)" NIL "kris" "luce-co.com")("Laurie Ellis (E-mail)" NIL "Laurieellis77" "yahoo.com")("Lola Luce (E-mail)" NIL "isismist" "msn.com")("Mark Tessmer (E-mail)" NIL "mtessmer" "ford.com")("Mike Sortor (E-mail)" NIL "mike.sortor" "us.bosch.com")("Mike Sullivan (E-mail)" NIL "msulli16" "ford.com")("Amie Miller (E-mail 2)" NIL "amiller" "hospice-swf.org")("Nick Miller (E-mail)" NIL "valmonte69" "aol.com")("Reza Khorshidi (E-mail)" NIL "rezak" "alum.mit.edu")("Roman Jach (E-mail 2)" NIL "jachs" "cablespeed.com")("Tara Marie Bardallis (E-mail)" NIL "Tara.Bardallis" "oracle.com")) (("Luce Karl (ANS) (E-mail)" NIL "kluce" "alum.mit.edu")) NIL "<3831EA55AE7BD5119F750008C7866D9F08CABDE0@drbnemail2.dearborn01a.lear.com>" "<20030905025220.33710.qmail@web41407.mail.yahoo.com>"')

    ########################################################################
    #
    def genericBodyStructureTest(self, msg_num, bs):
        fetch = FetchAtt(FetchAtt.OP_BODYSTRUCTURE)
        result,chg = fetch.fetch(self.messages[msg_num].msg,
                                 self.messages[msg_num])
        self.assertEqual(chg, False)
        self.assertEqual(result, 'BODYSTRUCTURE (%s)' % bs)

    ########################################################################
    #
    def testBodyStructure01(self):
        self.genericBodyStructureTest(9,'("TEXT" "PLAIN" ("CHARSET" "iso-8859-1") NIL NIL "7BIT" 15 1 NIL NIL NIL NIL)("TEXT" "PLAIN" ("NAME" "graves.txt") NIL NIL "QUOTED-PRINTABLE" 602 9 NIL ("ATTACHMENT" ("FILENAME" "graves.txt")) NIL NIL) "MIXED" ("BOUNDARY" "----=_NextPart_000_0053_01C30D84.6CC686E0") NIL NIL NIL')

    ########################################################################
    #
    def testBodyStructure02(self):
        self.genericBodyStructureTest(14,'("TEXT" "PLAIN" ("CHARSET" "iso-8859-1") NIL NIL "7BIT" 116 7 NIL NIL NIL NIL)("IMAGE" "JPEG" ("NAME" "P1010043.JPG") NIL NIL "BASE64" 89784 NIL ("ATTACHMENT" ("FILENAME" "P1010043.JPG")) NIL NIL) "MIXED" ("BOUNDARY" "----=_NextPart_000_0010_01C33206.2104C860") NIL NIL NIL')

    ########################################################################
    #
    def testBodyStructure03(self):
        self.genericBodyStructureTest(15,'("TEXT" "PLAIN" ("CHARSET" "us-ascii") NIL NIL "7BIT" 4366 107 NIL NIL NIL NIL)("TEXT" "HTML" ("CHARSET" "us-ascii") NIL NIL "7BIT" 11632 243 NIL NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "------------7C252360672B03A0BAD013A5") NIL NIL NIL')

    ########################################################################
    #
    def testBodyStructure04(self):
        self.genericBodyStructureTest(23,'"TEXT" "PLAIN" ("CHARSET" "iso-8859-1") NIL NIL "7BIT" 1133 20 NIL ("INLINE" NIL) NIL NIL')

    ########################################################################
    #
    def testBodyStructure05(self):
        self.genericBodyStructureTest(50,'("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 137 6 NIL NIL NIL NIL)("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 795 17 NIL NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "----=_NextPart_000_0026_01C360F8.606036E0") NIL NIL NIL')

    ########################################################################
    #
    def testBodyStructure06(self):
        self.genericBodyStructureTest(60,'("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2511 54 NIL NIL NIL NIL)("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 5269 137 NIL NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "__________MIMEboundary__________") NIL NIL NIL')

    ########################################################################
    #
    def testBodyStructure07(self):
        self.genericBodyStructureTest(61,'("TEXT" "PLAIN" ("CHARSET" "us-ascii") NIL NIL "7BIT" 3037 78 NIL NIL NIL NIL)("TEXT" "HTML" ("CHARSET" "us-ascii") NIL NIL "7BIT" 3464 10 NIL NIL NIL NIL) "ALTERNATIVE" ("BOUNDARY" "0-147294474-1062730340=:32871") NIL NIL NIL')

    ########################################################################
    #
    def genericRfc822Size(self, msg_num, bs):
        fetch = FetchAtt(FetchAtt.OP_RFC822_SIZE)
        result,chg = fetch.fetch(self.messages[msg_num].msg,
                                 self.messages[msg_num])
        self.assertEqual(chg, False)
        self.assertEqual(result, 'RFC822.SIZE %s' % bs)

    ########################################################################
    #
    def testRfc822Size01(self):
        self.genericRfc822Size(50, '2565')

    ########################################################################
    #
    def testRfc822Size02(self):
        self.genericRfc822Size(51, '1848')

    ########################################################################
    #
    def testRfc822Size03(self):
        self.genericRfc822Size(52, '2705')

    ########################################################################
    #
    def testRfc822Size04(self):
        self.genericRfc822Size(53, '2340')
    
    ########################################################################
    #
    def testRfc822Size05(self):
        self.genericRfc822Size(54, '3710')
    
    ########################################################################
    #
    def testRfc822Size06(self):
        self.genericRfc822Size(55, '39145')

    ########################################################################
    #
    def genericBodyTest01(self, msg_num, bs):
        fetch = FetchAtt(FetchAtt.OP_BODYSTRUCTURE, ext_data = False,
                         actual_command = "BODY")
        result,chg = fetch.fetch(self.messages[msg_num].msg,
                                 self.messages[msg_num])
        self.assertEqual(chg, False)
        self.assertEqual(result, 'BODY (%s)' % bs)

    ########################################################################
    #
    def testBody01(self):
        self.genericBodyTest01(9,'("TEXT" "PLAIN" ("CHARSET" "iso-8859-1") NIL NIL "7BIT" 15 1)("TEXT" "PLAIN" ("NAME" "graves.txt") NIL NIL "QUOTED-PRINTABLE" 602 9) "MIXED"')

    ########################################################################
    #
    def testBody02(self):
        self.genericBodyTest01(14,'("TEXT" "PLAIN" ("CHARSET" "iso-8859-1") NIL NIL "7BIT" 116 7)("IMAGE" "JPEG" ("NAME" "P1010043.JPG") NIL NIL "BASE64" 89784) "MIXED"')

    ########################################################################
    #
    def testBody03(self):
        self.genericBodyTest01(15,'("TEXT" "PLAIN" ("CHARSET" "us-ascii") NIL NIL "7BIT" 4366 107)("TEXT" "HTML" ("CHARSET" "us-ascii") NIL NIL "7BIT" 11632 243) "ALTERNATIVE"')

    ########################################################################
    #
    def testBody04(self):
        self.genericBodyTest01(23,'"TEXT" "PLAIN" ("CHARSET" "iso-8859-1") NIL NIL "7BIT" 1133 20')

    ########################################################################
    #
    def testBody05(self):
        self.genericBodyTest01(50,'("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 137 6)("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 795 17) "ALTERNATIVE"')

    ########################################################################
    #
    def testBody06(self):
        self.genericBodyTest01(60,'("TEXT" "PLAIN" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 2511 54)("TEXT" "HTML" ("CHARSET" "US-ASCII") NIL NIL "7BIT" 5269 137) "ALTERNATIVE"')

    ########################################################################
    #
    def testBody07(self):
        self.genericBodyTest01(61,'("TEXT" "PLAIN" ("CHARSET" "us-ascii") NIL NIL "7BIT" 3037 78)("TEXT" "HTML" ("CHARSET" "us-ascii") NIL NIL "7BIT" 3464 10) "ALTERNATIVE"')
    
    ########################################################################
    #
    def genericBodyTest02(self, msg_num, bs, section = [], partial = None,
                          peek = False):
        fetch = FetchAtt(FetchAtt.OP_BODY, section = section,
                         partial = partial, peek = peek)

        if '\Seen' in self.messages[msg_num].flags:
            seen = True
        else:
            seen = False

        result,chg = fetch.fetch(self.messages[msg_num].msg,
                                 self.messages[msg_num])
        if not peek and not seen:
            self.assertEqual(chg, True)
        else:
            self.assertEqual(chg, False)
        self.assertEqual(result, bs)

    ########################################################################
    #
    def testBody201(self):
        self.genericBodyTest02(1,'BODY[] {2074}\r\nReturn-Path: russluce@adelphia.net\r\nDelivery-Date: Sat, 04 Jan 2003 18:37:22 -0800\r\nReceived: from mta4.adelphia.net (mta4.mail.adelphia.net [64.8.50.184] (may be\r\n\tforged))\r\n\tby kamidake.apricot.com (8.11.6/8.11.6) with ESMTP id h052bE933604\r\n\tfor <scanner@apricot.com>; Sat, 4 Jan 2003 18:37:14 -0800 (PST)\r\n\t(envelope-from russluce@adelphia.net)\r\nReceived: from boss ([24.53.119.89]) by mta4.adelphia.net\r\n\t(InterMail vM.5.01.05.25 201-253-122-126-125-20021216) with SMTP\r\n\tid <20030105023218.OPW23099.mta4.adelphia.net@boss>\r\n\tfor <scanner@apricot.com>; Sat, 4 Jan 2003 18:32:18 -0800\r\nFrom: "Russell Luce" <russluce@adelphia.net>\r\nTo: <scanner@apricot.com>\r\nSubject: Net working\r\nDate: Sat, 4 Jan 2003 21:37:05 -0500\r\nMessage-ID: <DCEKIHLDAODKHJCKDBAAMEFHCBAA.russluce@adelphia.net>\r\nMIME-Version: 1.0\r\nContent-Type: text/plain;\r\n\tcharset="iso-8859-1"\r\nContent-Transfer-Encoding: 7bit\r\nX-Priority: 3 (Normal)\r\nX-MSMail-Priority: Normal\r\nX-Mailer: Microsoft Outlook IMO, Build 9.0.2416 (9.0.2910.0)\r\nImportance: Normal\r\nX-MimeOLE: Produced By Microsoft MimeOLE V6.00.2600.0000\r\nX-SpamBouncer: 1.5 (12/13/02)\r\nX-SBPass: No Freemail Filtering\r\nX-SBClass: OK\r\nX-Bogosity: No, tests=bogofilter, spamicity=0.336578, version=0.9.1.2\r\nX-MHImapServer-UID: 0000000012.0000000002\r\n\r\nEric,\r\n\r\nYour dad is getting his hardware together for interfacing to the outside\r\nworld via DSL.\r\n\r\nMy hardware is basically all set: Dual PII Xeon processors (1 meg cache\r\neach) 1 Gig of ECC memory, 2 18 Gig scsi drives (configured in a 1 raid,\r\nMirroring config),Mylex controller\r\nand two Nics.\r\n\r\nThe main problem now is I would like to run linux as the server software but\r\nI have very little to no experience with it.  What suggestions do you have\r\nin this area?  Like do I use a simple software firewall.  I guess I am open\r\nto suggestions, because I can use Windows\r\n2k server software or Linux.  I\'m going to need a firewall, that will work\r\nwith this software. all the computers that tie to the internal network run\r\nwindows XP, 2K, 98SE.  Which would be the better way to go?\r\n\r\nDad\r\n\r\n',
                               section = [],
                               partial = None,
                               peek = False)

    ########################################################################
    #
    def testBody202(self):
        self.genericBodyTest02(9,'BODY[] {2000}\r\nReturn-Path: gruhn@hwb.com\r\nDelivery-Date: Mon, 28 Apr 2003 12:51:04 -0700\r\nReceived: from arallc (office.rararchitects.com [63.230.202.169])\r\n\tby kamidake.apricot.com (8.12.8/8.12.8) with SMTP id h3SJp0uG036327\r\n\tfor <scanner@apricot.com>; Mon, 28 Apr 2003 12:51:01 -0700 (PDT)\r\n\t(envelope-from gruhn@hwb.com)\r\nMessage-ID: <005601c30dbf$1a96a220$0300a8c0@qwest.net>\r\nFrom: "gruhn" <gruhn@hwb.com>\r\nTo: "Scanner Luce" <scanner@apricot.com>\r\nSubject: for SolThree\r\nDate: Mon, 28 Apr 2003 12:48:10 -0700\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed;\r\n\tboundary="----=_NextPart_000_0053_01C30D84.6CC686E0"\r\nX-Priority: 3\r\nX-MSMail-Priority: Normal\r\nX-Mailer: Microsoft Outlook Express 6.00.2600.0000\r\nX-MimeOLE: Produced By Microsoft MimeOLE V6.00.2600.0000\r\nX-SpamBouncer: 1.5 (12/13/02)\r\nX-SBPass: No Freemail Filtering\r\nX-SBClass: OK\r\nX-Bogosity: No, tests=bogofilter, spamicity=0.292588, version=0.11.2\r\nX-MHImapServer-UID: 0000000012.0000000010\r\n\r\nThis is a multi-part message in MIME format.\r\n\r\n------=_NextPart_000_0053_01C30D84.6CC686E0\r\nContent-Type: text/plain;\r\n\tcharset="iso-8859-1"\r\nContent-Transfer-Encoding: 7bit\r\n\r\nsee attached.\r\n\r\n------=_NextPart_000_0053_01C30D84.6CC686E0\r\nContent-Type: text/plain;\r\n\tname="graves.txt"\r\nContent-Transfer-Encoding: quoted-printable\r\nContent-Disposition: attachment;\r\n\tfilename="graves.txt"\r\n\r\nWe were driving around the back roads of Ireland one day. Maybe out in =\r\nConnemara. Maybe not. Out in the middle of nowhere we passed a small =\r\ngraveyard. I got the car to stop and back up because I thought I had =\r\nseen something which everybody else found just a bit intriguing as well. =\r\nOr maybe they were just being indulgent. I thought most every grave =\r\nstone in the yard had a dollar sign ($) carved in the top of it. That =\r\nseemed a little odd to me. Turns out that I was sort of right, but in =\r\nthe end, completely wrong. It was the letters I H S super imposed on top =\r\nof one another.\r\n------=_NextPart_000_0053_01C30D84.6CC686E0--\r\n\r\n',
                               section = [],
                               partial = None,
                               peek = False)

    ########################################################################
    #
    def testBody203(self):
        self.genericBodyTest02(1,"BODY[1] {797}\r\nEric,\r\n\r\nYour dad is getting his hardware together for interfacing to the outside\r\nworld via DSL.\r\n\r\nMy hardware is basically all set: Dual PII Xeon processors (1 meg cache\r\neach) 1 Gig of ECC memory, 2 18 Gig scsi drives (configured in a 1 raid,\r\nMirroring config),Mylex controller\r\nand two Nics.\r\n\r\nThe main problem now is I would like to run linux as the server software but\r\nI have very little to no experience with it.  What suggestions do you have\r\nin this area?  Like do I use a simple software firewall.  I guess I am open\r\nto suggestions, because I can use Windows\r\n2k server software or Linux.  I'm going to need a firewall, that will work\r\nwith this software. all the computers that tie to the internal network run\r\nwindows XP, 2K, 98SE.  Which would be the better way to go?\r\n\r\nDad\r\n\r\n",
                               section = [1],
                               partial = None,
                               peek = False)

    ########################################################################
    #
    def testBody204(self):
        self.genericBodyTest02(1,"BODY[TEXT] {797}\r\nEric,\r\n\r\nYour dad is getting his hardware together for interfacing to the outside\r\nworld via DSL.\r\n\r\nMy hardware is basically all set: Dual PII Xeon processors (1 meg cache\r\neach) 1 Gig of ECC memory, 2 18 Gig scsi drives (configured in a 1 raid,\r\nMirroring config),Mylex controller\r\nand two Nics.\r\n\r\nThe main problem now is I would like to run linux as the server software but\r\nI have very little to no experience with it.  What suggestions do you have\r\nin this area?  Like do I use a simple software firewall.  I guess I am open\r\nto suggestions, because I can use Windows\r\n2k server software or Linux.  I'm going to need a firewall, that will work\r\nwith this software. all the computers that tie to the internal network run\r\nwindows XP, 2K, 98SE.  Which would be the better way to go?\r\n\r\nDad\r\n\r\n",
                               section = ['TEXT'],
                               partial = None,
                               peek = False)

    ########################################################################
    #
    def testBody205(self):
        self.genericBodyTest02(9,'BODY[1.MIME] {85}\r\nContent-Type: text/plain;\r\n\tcharset="iso-8859-1"\r\nContent-Transfer-Encoding: 7bit\r\n\r\n',
                               section = [1, 'MIME'],
                               partial = None,
                               peek = False)

    ########################################################################
    #
    def testBody206(self):
        self.genericBodyTest02(9,'BODY[2.MIME] {152}\r\nContent-Type: text/plain;\r\n\tname="graves.txt"\r\nContent-Transfer-Encoding: quoted-printable\r\nContent-Disposition: attachment;\r\n\tfilename="graves.txt"\r\n\r\n',
                               section = [2, 'MIME'],
                               partial = None,
                               peek = False)

    ########################################################################
    #
    def testBody207(self):
        self.genericBodyTest02(9,'BODY[1] {15}\r\nsee attached.\r\n',
                               section = [1],
                               partial = None,
                               peek = False)

    ########################################################################
    #
    def testBody208(self):
        self.genericBodyTest02(9,'BODY[2] {602}\r\nWe were driving around the back roads of Ireland one day. Maybe out in =\r\nConnemara. Maybe not. Out in the middle of nowhere we passed a small =\r\ngraveyard. I got the car to stop and back up because I thought I had =\r\nseen something which everybody else found just a bit intriguing as well. =\r\nOr maybe they were just being indulgent. I thought most every grave =\r\nstone in the yard had a dollar sign ($) carved in the top of it. That =\r\nseemed a little odd to me. Turns out that I was sort of right, but in =\r\nthe end, completely wrong. It was the letters I H S super imposed on top =\r\nof one another.',
                               section = [2],
                               partial = None,
                               peek = False)

    ########################################################################
    #
    def testBody209(self):
        self.genericBodyTest02(9,'BODY[HEADER] {955}\r\nReturn-Path: gruhn@hwb.com\r\nDelivery-Date: Mon, 28 Apr 2003 12:51:04 -0700\r\nReceived: from arallc (office.rararchitects.com [63.230.202.169])\r\n\tby kamidake.apricot.com (8.12.8/8.12.8) with SMTP id h3SJp0uG036327\r\n\tfor <scanner@apricot.com>; Mon, 28 Apr 2003 12:51:01 -0700 (PDT)\r\n\t(envelope-from gruhn@hwb.com)\r\nMessage-ID: <005601c30dbf$1a96a220$0300a8c0@qwest.net>\r\nFrom: "gruhn" <gruhn@hwb.com>\r\nTo: "Scanner Luce" <scanner@apricot.com>\r\nSubject: for SolThree\r\nDate: Mon, 28 Apr 2003 12:48:10 -0700\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed;\r\n\tboundary="----=_NextPart_000_0053_01C30D84.6CC686E0"\r\nX-Priority: 3\r\nX-MSMail-Priority: Normal\r\nX-Mailer: Microsoft Outlook Express 6.00.2600.0000\r\nX-MimeOLE: Produced By Microsoft MimeOLE V6.00.2600.0000\r\nX-SpamBouncer: 1.5 (12/13/02)\r\nX-SBPass: No Freemail Filtering\r\nX-SBClass: OK\r\nX-Bogosity: No, tests=bogofilter, spamicity=0.292588, version=0.11.2\r\nX-MHImapServer-UID: 0000000012.0000000010\r\n\r\n',
                               section = ['HEADER'],
                               partial = None,
                               peek = False)

    ########################################################################
    #
    def testBody210(self):
        self.genericBodyTest02(9,'BODY[TEXT] {1045}\r\nThis is a multi-part message in MIME format.\r\n\r\n------=_NextPart_000_0053_01C30D84.6CC686E0\r\nContent-Type: text/plain;\r\n\tcharset="iso-8859-1"\r\nContent-Transfer-Encoding: 7bit\r\n\r\nsee attached.\r\n\r\n------=_NextPart_000_0053_01C30D84.6CC686E0\r\nContent-Type: text/plain;\r\n\tname="graves.txt"\r\nContent-Transfer-Encoding: quoted-printable\r\nContent-Disposition: attachment;\r\n\tfilename="graves.txt"\r\n\r\nWe were driving around the back roads of Ireland one day. Maybe out in =\r\nConnemara. Maybe not. Out in the middle of nowhere we passed a small =\r\ngraveyard. I got the car to stop and back up because I thought I had =\r\nseen something which everybody else found just a bit intriguing as well. =\r\nOr maybe they were just being indulgent. I thought most every grave =\r\nstone in the yard had a dollar sign ($) carved in the top of it. That =\r\nseemed a little odd to me. Turns out that I was sort of right, but in =\r\nthe end, completely wrong. It was the letters I H S super imposed on top =\r\nof one another.\r\n------=_NextPart_000_0053_01C30D84.6CC686E0--\r\n\r\n',
                               section = ['TEXT'],
                               partial = None,
                               peek = False)

    ########################################################################
    #
    def testBody211(self):
        self.genericBodyTest02(9,'BODY[[\'HEADER.FIELDS\', [\'FROM\', \'TO\']]] {75}\r\nFrom: "gruhn" <gruhn@hwb.com>\r\nTo: "Scanner Luce" <scanner@apricot.com>\r\n\r\n',
                               section = [['HEADER.FIELDS', ['FROM', 'TO']]],
                               partial = None,
                               peek = False)

    ########################################################################
    #
    def testBody212(self):
        self.genericBodyTest02(9,'BODY[[\'HEADER.FIELDS.NOT\', [\'FROM\', \'TO\']]] {882}\r\nReturn-Path: gruhn@hwb.com\r\nDelivery-Date: Mon, 28 Apr 2003 12:51:04 -0700\r\nReceived: from arallc (office.rararchitects.com [63.230.202.169])\r\n\tby kamidake.apricot.com (8.12.8/8.12.8) with SMTP id h3SJp0uG036327\r\n\tfor <scanner@apricot.com>; Mon, 28 Apr 2003 12:51:01 -0700 (PDT)\r\n\t(envelope-from gruhn@hwb.com)\r\nMessage-ID: <005601c30dbf$1a96a220$0300a8c0@qwest.net>\r\nSubject: for SolThree\r\nDate: Mon, 28 Apr 2003 12:48:10 -0700\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed;\r\n\tboundary="----=_NextPart_000_0053_01C30D84.6CC686E0"\r\nX-Priority: 3\r\nX-MSMail-Priority: Normal\r\nX-Mailer: Microsoft Outlook Express 6.00.2600.0000\r\nX-MimeOLE: Produced By Microsoft MimeOLE V6.00.2600.0000\r\nX-SpamBouncer: 1.5 (12/13/02)\r\nX-SBPass: No Freemail Filtering\r\nX-SBClass: OK\r\nX-Bogosity: No, tests=bogofilter, spamicity=0.292588, version=0.11.2\r\nX-MHImapServer-UID: 0000000012.0000000010\r\n\r\n',
                               section = [['HEADER.FIELDS.NOT', ['FROM',
                                                                 'TO']]],
                               partial = None,
                               peek = False)


## a01 FETCH 50:55 FAST
## * 50 FETCH (FLAGS (\Seen) INTERNALDATE "13-Aug-2003 00:37:22 +0000" RFC822.SIZE 2524)
## * 51 FETCH (FLAGS (\Seen) INTERNALDATE "13-Aug-2003 01:39:59 +0000" RFC822.SIZE 1807)
## * 52 FETCH (FLAGS (\Seen) INTERNALDATE "15-Aug-2003 01:27:21 +0000" RFC822.SIZE 2660)
## * 53 FETCH (FLAGS (\Seen) INTERNALDATE "15-Aug-2003 04:10:41 +0000" RFC822.SIZE 2292)
## * 54 FETCH (FLAGS (\Seen) INTERNALDATE "18-Aug-2003 14:34:38 +0000" RFC822.SIZE 2375)
## * 55 FETCH (FLAGS (\Seen) INTERNALDATE "19-Aug-2003 19:33:10 +0000" RFC822.SIZE 3665)
## a01 OK FETCH completed

############################################################################   
#
def suite():

    # NOTE: Since this data is all used readonly we create these files
    # via their tar archives and let them sit around for the entire
    # test session.
    #
    # Make sure to remove any temp data files that may have been
    # lieing around.
    #
    commands.getoutput("rm -rf /tmp/mh-imap-test")

    # We re-create our tail mail directory from scratch each time
    # this is run.
    #
    commands.getoutput("tar -C /tmp/ -x -f test-data/test-data.tar")

    suite = unittest.makeSuite(IMAPFetchTest)
    return suite

###########
#
# If we are invoked as a standalone program, just run the test suite defined
# in this module.
#
if __name__ == "__main__":
    suite = suite()
    unittest.main()
    
#
#
###########

# a01 FETCH 1 BODY[MIME]
# a01 BAD Unknown section text specifier


# a01 FETCH 1 ALL
# * 1 FETCH (FLAGS (\Seen) INTERNALDATE " 5-Jan-2003 02:37:22 +0000" RFC822.SIZE 2056 ENVELOPE ("Sat, 4 Jan 2003 21:37:05 -0500" "Net working" (("Russell Luce" NIL "russluce" "adelphia.net")) (("Russell Luce" NIL "russluce" "adelphia.net")) (("Russell Luce" NIL "russluce" "adelphia.net")) ((NIL NIL "scanner" "apricot.com")) NIL NIL NIL "<DCEKIHLDAODKHJCKDBAAMEFHCBAA.russluce@adelphia.net>"))
# a01 OK FETCH completed


# * 1 FETCH (BODY[1.TEXT] "")
# a01 OK FETCH completed
# a01 fetch 1 body[1]
# * 1 FETCH (BODY[1] {797}
# Eric,
# ...



# a01 FETCH 1 body[1.2]
# * 1 FETCH (BODY[1.2] "")
# a01 OK FETCH completed


# a01 FETCH 9 BODY
# * 9 FETCH (BODY (("TEXT" "PLAIN" ("CHARSET" "iso-8859-1") NIL NIL "7BIT" 15 1)("TEXT" "PLAIN" ("NAME" "graves.txt") NIL NIL "QUOTED-PRINTABLE" 602 8) "MIXED"))
# a01 OK FETCH completed



# a01 FETCH 9 BODY[]
# * 9 FETCH (BODY[] {1957}
# Return-Path: gruhn@hwb.com
# Delivery-Date: Mon, 28 Apr 2003 12:51:04 -0700
# Received: from arallc (office.rararchitects.com [63.230.202.169])
#         by kamidake.apricot.com (8.12.8/8.12.8) with SMTP id h3SJp0uG036327
#         for <scanner@apricot.com>; Mon, 28 Apr 2003 12:51:01 -0700 (PDT)
#         (envelope-from gruhn@hwb.com)
# Message-ID: <005601c30dbf$1a96a220$0300a8c0@qwest.net>
# From: "gruhn" <gruhn@hwb.com>
# To: "Scanner Luce" <scanner@apricot.com>
# Subject: for SolThree
# Date: Mon, 28 Apr 2003 12:48:10 -0700
# MIME-Version: 1.0
# Content-Type: multipart/mixed;
#         boundary="----=_NextPart_000_0053_01C30D84.6CC686E0"
# X-Priority: 3
# X-MSMail-Priority: Normal
# X-Mailer: Microsoft Outlook Express 6.00.2600.0000
# X-MimeOLE: Produced By Microsoft MimeOLE V6.00.2600.0000
# X-SpamBouncer: 1.5 (12/13/02)
# X-SBPass: No Freemail Filtering
# X-SBClass: OK
# X-Bogosity: No, tests=bogofilter, spamicity=0.292588, version=0.11.2

# This is a multi-part message in MIME format.

# ------=_NextPart_000_0053_01C30D84.6CC686E0
# Content-Type: text/plain;
#         charset="iso-8859-1"
# Content-Transfer-Encoding: 7bit

# see attached.

# ------=_NextPart_000_0053_01C30D84.6CC686E0
# Content-Type: text/plain;
#         name="graves.txt"
# Content-Transfer-Encoding: quoted-printable
# Content-Disposition: attachment;
#         filename="graves.txt"

# We were driving around the back roads of Ireland one day. Maybe out in =
# Connemara. Maybe not. Out in the middle of nowhere we passed a small =
# graveyard. I got the car to stop and back up because I thought I had =
# seen something which everybody else found just a bit intriguing as well. =
# Or maybe they were just being indulgent. I thought most every grave =
# stone in the yard had a dollar sign ($) carved in the top of it. That =
# seemed a little odd to me. Turns out that I was sort of right, but in =
# the end, completely wrong. It was the letters I H S super imposed on top =
# of one another.
# ------=_NextPart_000_0053_01C30D84.6CC686E0--

# )
# * 9 FETCH (FLAGS (\Seen))
# a01 OK FETCH completed



# a01 fetch 9 body[text]
# * 9 FETCH (BODY[TEXT] {1045}
# This is a multi-part message in MIME format.

# ------=_NextPart_000_0053_01C30D84.6CC686E0
# Content-Type: text/plain;
#         charset="iso-8859-1"
# Content-Transfer-Encoding: 7bit

# see attached.

# ------=_NextPart_000_0053_01C30D84.6CC686E0
# Content-Type: text/plain;
#         name="graves.txt"
# Content-Transfer-Encoding: quoted-printable
# Content-Disposition: attachment;
#         filename="graves.txt"

# We were driving around the back roads of Ireland one day. Maybe out in =
# Connemara. Maybe not. Out in the middle of nowhere we passed a small =
# graveyard. I got the car to stop and back up because I thought I had =
# seen something which everybody else found just a bit intriguing as well. =
# Or maybe they were just being indulgent. I thought most every grave =
# stone in the yard had a dollar sign ($) carved in the top of it. That =
# seemed a little odd to me. Turns out that I was sort of right, but in =
# the end, completely wrong. It was the letters I H S super imposed on top =
# of one another.
# ------=_NextPart_000_0053_01C30D84.6CC686E0--

# )
# a01 OK FETCH completed

