#!/usr/bin/env python
#
# File: $Id$
#
"""
A little tool that given an MH folder will move messages in the folder
in to subfolders that are named '<parent folder name>_<year>' by the
timestamp of when the mail was received in its headers.

Except we will keep at least 1,000 in the folder
"""

# system imports
#
import sys
import mailbox

#############################################################################
#
def main():
    """

    """
    source_folder = sys.argv[1]
    mbox = mailbox.MH(source_folder)
    mbox.lock()

    try:
        msg_array = []
        msgs = mbox.keys()

        if len(msgs) < 1000:
            print "Less than 1000 messages in folder. Nothing to do."
            return

        # Find the dates of all the messages and sort them so we know
        # which ones to move in to which sub-folders.
        #
        for msg_key in msgs:
            msg = mbox[msg_key]
            if 'delivery-date' in msg:
                date = msg['delivery-date']
            elif 'date' in msg:
                date = msg['date']
            else:
                date = os.path.getmtime(os.path.join(source_folder,
                                                     str(msg_key)))
            msg_array.append((date,msg_key))

        msg_array.sort(lambda x,y: x[0] < y[0])

        msg_array = msg_array[-1000:]
        subfolder = None
        subfolder_year = None
        for date, msg_key in msg_array:
            msg = mbox[msg_key]
            year_of_msg = year_of_date(date)
            if subfolder is None:
                subfolder_year = year_of_msg
                subfolder = mailbox.MH(os.path.join(source_folder,
                                                    "%s-%04d" % (os.path.basename(source_folder), year)), create = True)
            if subfolder and subfolder_year != year_of_msg
                subfolder_year = year_of_msg
                subfolder = mailbox.MH(os.path.join(source_folder,
                                                    "%s-%04d" % (os.path.basename(source_folder), year)), create = True)
            subfolder.add(msg)
            mbox.remove(msg)

    finally:
        mbox.unlock()
    return

############################################################################
############################################################################
#
# Here is where it all starts
#
if __name__ == "__main__":
    main()
#
#
############################################################################
############################################################################
