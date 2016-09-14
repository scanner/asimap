#
# File: $Id: utils.py 1355 2007-07-03 07:22:41Z scanner $
#

"""
Some common code used by more than one test case.
"""

import commands
import os.path
import email
import mhlib

from datetime import datetime

# mhimap imports
#
import mhimap.utils
from mhimap.Mailbox import MessageEntry

# Our tests including searching for messages with various flags set or
# not set. To that end we define the message numbers of messages that
# have various flags set. This way we can use this both to generate the
# test data and to make sure the results of certain searches worked
# properly. This also assumes that the messages with these numbers exist
# in our folder.
#
SEEN_MSGS = [x for x in range(1,90)]
ANSWERED_MSGS = [x for x in range(1,80)]
FLAGGED_MSGS = [1,5,8,10,20]
DELETED_MSGS = [1,2,3,4,5]
DRAFT_MSGS = [7,10,13,21]
RECENT_MSGS = [x for x in range(90,100)]


def folder_setup(test_case):
    test_case.mh = mhlib.MH(profile = '/tmp/mh-imap-test/mh_profile')
    test_case.folder = test_case.mh.openfolder('oldinbox')
    test_case.messages = { }

    msg_path_root = test_case.folder.getfullname()
    msgs = test_case.folder.listmessages()

    test_case.max_num = len(msgs)
    test_case.max_uid = len(msgs)

    for msg_num in msgs:

        # Set up our uid & message filename 
        uuid = "%010d.%010d" % (1, msg_num)
        filename = test_case.folder.getmessagefilename(msg_num)

        # Read the message in to a structure in memory.
        fp = open(filename, 'r')
        msg = email.Parser.Parser().parse(fp = fp)
        fp.close()

        # Figure out what flags we need to set.
        flags = []
        if msg_num in test_case.SEEN_MSGS:
            flags.append('\Seen')
        if msg_num in test_case.ANSWERED_MSGS:
            flags.append('\Answered')
        if msg_num in test_case.FLAGGED_MSGS:
            flags.append('\Flagged')
        if msg_num in test_case.DELETED_MSGS:
            flags.append('\Deleted')
        if msg_num in test_case.DRAFT_MSGS:
            flags.append('\Draft')
        if msg_num in test_case.RECENT_MSGS:
            flags.append('\Recent')

        # Determine the 'internal-date' for a message.
        if 'delivery-date' in msg:
            internal_date = mhimap.utils.parsedate(msg['delivery-date'])
        else:
            internal_date = \
                datetime.utcfromtimestamp(os.path.getmtime(msg_file_name))

        # Create our message entry object for this message.
        msg_entry = MessageEntry(uuid, flags = flags,
                                 internal_date = internal_date,
                                 msg_num = msg_num,
                                 msg_file = filename)

        # Tack the actual message object in to our message entry so that
        # we can pull it out easily later.
        msg_entry.msg = msg

        # And put this message in our dictionary of messages.
        #
        test_case.messages[msg_num] = msg_entry
    
