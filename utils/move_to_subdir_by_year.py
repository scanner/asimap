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
import email.utils
import os

############################################################################
#
def setup_option_parser():
    """
    This function uses the python OptionParser module to define an option
    parser for parsing the command line options for this script. This does not
    actually parse the command line options. It returns the parser object that
    can be used for parsing them.
    """
    parser = optparse.OptionParser(usage = "%prog [options]",
                                   version = asimap.__version__)

    parser.set_defaults(src_folder = None, dry_run = False)
    parser.add_option("--folder", action="store", type="string",
                      dest="src_folder", help = "The MH folder to operate on")
    parser.add_option("--dry_run", action="store_true", dest="dry_run",
                      help="Do a dry run - ie: do not create any folders, "
                      "do not move any messages")
    return parser

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
                tt = email.utils.parsedate_tz(msg['delivery-date'])
                date = email.utils.mktime_tz(tt)
                year = tt[0]
            elif 'date' in msg:
                tt = email.utils.parsedate_tz(msg['date'])
                date = email.utils.mktime_tz(tt)
                year = tt[0]
            else:
                date = os.path.getmtime(os.path.join(source_folder,
                                                     str(msg_key)))
                tt = time.gmtime(date)
                year = tt[0]

            msg_array.append((date,year,msg_key))

        msg_array.sort(lambda x,y: x[0] < y[0])

        msg_array = msg_array[-1000:]
        subfolder = None
        subfolder_year = None

        for date, year, msg_key in msg_array:
            msg = mbox[msg_key]
            if subfolder is None:
                subfolder_year = year
                folder_name = "%s-%04d" % \
                    (os.path.basename(source_folder), year)
                print "making folder: %s" % folder_name
                #subfolder = folder_name
                subfolder = mailbox.MH(os.path.join(source_folder,folder_name),
                                       create = True)
            if subfolder and subfolder_year != year:
                subfolder_year = year
                folder_name = "%s-%04d" % \
                    (os.path.basename(source_folder), year)
                print "making folder: %s" % folder_name
                # subfolder = folder_name
                subfolder = mailbox.MH(os.path.join(source_folder,folder_name),
                                       create = True)
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
