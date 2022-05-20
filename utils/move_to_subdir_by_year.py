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

import email.utils
import mailbox
import optparse
import os

# system imports
#
import sys
import time
from datetime import datetime


############################################################################
#
def setup_option_parser():
    """
    This function uses the python OptionParser module to define an option
    parser for parsing the command line options for this script. This does not
    actually parse the command line options. It returns the parser object that
    can be used for parsing them.
    """
    parser = optparse.OptionParser(usage="%prog [options]", version="1.0")

    parser.set_defaults(dry_run=False, keep=1000)
    parser.add_option(
        "--dry_run",
        action="store_true",
        dest="dry_run",
        help="Do a dry run - ie: do not create any folders, "
        "do not move any messages",
    )
    parser.add_option(
        "--keep",
        action="store",
        dest="keep",
        type="int",
        help="How many messages to keep in the folder and "
        "not move to subfolders",
    )
    return parser


#############################################################################
#
def main():
    """ """
    parser = setup_option_parser()
    (options, args) = parser.parse_args()
    if len(args) != 1:
        print("need to supply a MH folder to operation on")
        return

    source_folder = args[0]

    # Trim off any trailing "/" because basename will trim it not how
    # we want if the path ends in a "/"
    #
    if source_folder[-1] == "/":
        source_folder = source_folder[:-1]

    mbox = mailbox.MH(source_folder)
    mbox.lock()

    print("Collecting timestamps for all messages..")
    try:
        msg_array = []
        msgs = list(mbox.keys())

        if len(msgs) < options.keep:
            print(
                "Less than %s messages in folder. Nothing to do."
                % options.keep
            )
            return

        # Find the dates of all the messages and sort them so we know
        # which ones to move in to which sub-folders.
        #
        for i, msg_key in enumerate(msgs):

            if i % 200 == 0:
                print("%d out of %d" % (i, len(msgs) - i))

            msg = mbox[msg_key]

            tt = None
            try:
                if "delivery-date" in msg:
                    tt = email.utils.parsedate_tz(msg["delivery-date"])
                    date = email.utils.mktime_tz(tt)
            except (ValueError, TypeError):
                print("Yow. Message %d's 'delivery-date'(%s) resulted in ")
                "a: %s" % (msg_key, msg["delivery-date"], str(e))
                tt = None

            try:
                if tt is None and "date" in msg:
                    tt = email.utils.parsedate_tz(msg["date"])
                    date = email.utils.mktime_tz(tt)
            except (ValueError, TypeError) as e:
                print(
                    "Yow. Message %d's 'date'(%s) resulted in a: %s"
                    % (msg_key, msg["date"], str(e))
                )
                tt = None
            except OverflowError as e:
                print(
                    "Yow. Message %d's 'date'(%s) resulted in a: %s"
                    % (msg_key, msg["date"], str(e))
                )
                tt = None

            if tt is None:
                date = os.path.getmtime(
                    os.path.join(source_folder, str(msg_key))
                )

            msg_array.append((date, msg_key))

        msg_array.sort(key=lambda x: x[0])

        print("Total number of messages: %d" % len(msg_array))
        print(
            "Spanning from %s, to %s"
            % (time.ctime(msg_array[0][0]), time.ctime(msg_array[-1][0]))
        )

        msg_array = msg_array[: -options.keep]
        print("Goign to move %d messages" % len(msg_array))

        subfolder = None
        subfolder_year = None

        if options.dry_run:
            print("Doing a dry run! So nothing is actually being done..")

        cur_year = 0
        for date, msg_key in msg_array:
            msg = mbox[msg_key]
            tt = time.gmtime(date)
            year = tt.tm_year

            if cur_year != year:
                cur_year = year
                folder_name = "%s_%04d" % (
                    os.path.basename(source_folder),
                    year,
                )
                folder_path = os.path.join(source_folder, folder_name)
                print("making folder: %s" % folder_path)
                if not options.dry_run:
                    subfolder = mailbox.MH(
                        os.path.join(folder_path), create=True
                    )

            if not options.dry_run:
                mtime = os.path.getmtime(
                    os.path.join(source_folder, str(msg_key))
                )
                new_msg_key = subfolder.add(msg)
                os.utime(
                    os.path.join(folder_path, str(new_msg_key)), (mtime, mtime)
                )
                mbox.unlock()
                mbox.remove(msg_key)
                mbox.lock()

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
