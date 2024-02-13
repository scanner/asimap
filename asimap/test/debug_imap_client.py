#!/usr/bin/env python
#
# File: $Id$
#
"""
A quick little script to test an imap server by connecting to it
on localhost and logging in.

This is a playground for testing the imap commands we want to actually
use against a test server.
"""

import imaplib
import mailbox

# system imports
#
import os
import os.path
import tarfile


####################################################################
#
def cleanup_test_mode_dir(test_mode_dir):
    """
    Set up the 'test_mode' mail directory. Clean out any messages it
    may have from previous runs
    """

    # Make the test mode directory and inbox & Archive subdirectories
    # if they do not already exist. Delete any messages in the
    # mailboxes if they exist.
    #
    mh = mailbox.MH(test_mode_dir, create=True)

    folders = mh.list_folders()
    for f in ("inbox", "Archive", "Junk"):
        if f not in folders:
            mh_f = mh.add_folder(f)
        else:
            mh_f = mh.get_folder(f)

        # Delete any messages in these folders
        #
        mh_f.lock()
        try:
            for msg_key in list(mh_f.keys()):
                mh_f.discard(msg_key)
        finally:
            mh_f.unlock()

        # See if we have a zipfile of messages to seed the now empty
        # folder with.
        #
        init_state_msgs_file = os.path.join(test_mode_dir, f + ".tar.gz")
        if os.path.exists(init_state_msgs_file):
            # We do not care about the names of the files in this zip
            # file. Each file we insert in to this mh folder.
            #
            print(
                "Extracting initial messages from {}".format(
                    init_state_msgs_file
                )
            )
            mh_f.lock()
            try:
                with tarfile.open(init_state_msgs_file, "r:gz") as tf:
                    for member in tf.getmembers():
                        if member.isfile():
                            print(
                                "    Adding message {}, size: {}".format(
                                    member.name, member.size
                                )
                            )
                            mh_f.add(tf.extractfile(member).read())
            finally:
                mh_f.unlock()


####################################################################
#
def do_baked_appends(test_mode_dir, imap, mbox_name):
    """
    Look for a tar file in the test mode directory for the given
    mailbox that contains messages we want to send to the IMAP server
    via APPEND.

    Keyword Arguments:
    test_mode_dir -- the path name for the test mode directory
    imap -- imaplib.IMAP4 object
    mbox_name -- name of the mailbox to append the messages to
    """
    tfile = os.path.join(
        test_mode_dir, "append_fodder-{}.tar.gz".format(mbox_name)
    )
    if not os.path.exists(tfile):
        return

    with tarfile.open(tfile, "r:gz") as tf:
        for member in tf.getmembers():
            if member.isfile():
                print(
                    "    Appending tf member {}, size: {}".format(
                        member.name, member.size
                    )
                )
                content = tf.extractfile(member).read()
                imap.append(mbox_name, None, None, content)


####################################################################
#
def dump_all_messages(imap):
    """
    Search and dump all the messages in the currently selected mailbox
    Keyword Arguments:
    imap -- imaplib.IMAP4 object
    """
    typ, data = imap.search(None, "ALL")
    if data[0]:
        print("  Messages in mailbox: {}".format(data[0]))
        for num in data[0].split():
            t, d = imap.fetch(num, "(RFC822.header)")
            print("    Message {} header info: {}".format(num, d[0][0]))
            # typ, data = imap.fetch(num, '(RFC822)')
            # print 'Message {}, length: {}'.format(num, len(d[0][1]))


#############################################################################
#
def main():
    # Look for the credentials in a well known file in several
    # locations relative to the location of this file.
    #
    # XXX Should make a command line option to set the mail dir
    #     directory we exepct to use.
    #
    username = None
    password = None
    for path in ("test_mode", "../test_mode"):
        creds_file = os.path.join(
            os.path.dirname(__file__), path, "test_mode_creds.txt"
        )
        print("Looking for creds file {}".format(creds_file))
        if os.path.exists(creds_file):
            print("Using credentials file {}".format(creds_file))
            username, password = open(creds_file).read().strip().split(":")
            test_mode_dir = os.path.dirname(creds_file)
            break

    if username is None or password is None:
        raise RuntimeError("Unable to find test mode credentials")

    # Look for the address file in the same directory as the creds file
    #
    addr_file = os.path.join(test_mode_dir, "test_mode_addr.txt")
    addr, port = open(addr_file).read().strip().split(":")
    port = int(port)

    print("Cleaning and setting up test-mode maildir")
    cleanup_test_mode_dir(test_mode_dir)

    imap = imaplib.IMAP4(addr, port)
    imap.login(username, password)

    for mbox_name in ("INBOX", "Archive", "Junk"):
        print("Selected '{}'".format(mbox_name))

        imap.select(mbox_name)
        do_baked_appends(test_mode_dir, imap, mbox_name)
        dump_all_messages(imap)

    imap.close()
    imap.logout()


############################################################################
############################################################################
#
# Here is where it all starts
#
if __name__ == "__main__":
    main()
#
############################################################################
############################################################################
