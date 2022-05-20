#!/usr/bin/env python
#
# File: $Id$
#
"""
A utility script that does the work of going through every MH mbox and
indexing it. This will add/update the uids on every message. It will
update the mbox's entry in the asimap db as well.

This is a script to run when setting up an asimap configuration for a
user to pre-index their entire MH folder.

It MUST NOT be run when asimapd is running because they will fight
over the same data structures and files.

The purpose is to pre-index those very large folders and/or test that
we are indexing things correctly when the system is otherwise
quiescent.

Assumptions this program makes:

o The user this program is being run as is the one that has the MH
  folder you want to index.
o The MH folder is stored in ~/Mail
"""

import asyncore
import logging
import logging.handlers
import optparse

# system imports
#
import os
import os.path
import pwd
import sys
import time

# XXX NOTE: We munge the python path so that we use the asimap directory we
#     are running out of as the source of its modules!
asimap_module_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)
sys.path.insert(0, asimap_module_dir)
# Application imports
#
import asimap
import asimap.user_server


##################################################################
##################################################################
#
class TempOptions(object):
    """
    An options object that contains the attributes that the user
    server and folder objects expect to do their job.

    This basically just stubs out the attributes in the 'options'
    object with ones appropriate for running the user_server in
    standalone mode.
    """

    ##################################################################
    #
    def __init__(self):
        """
        set up our attributes for standalone mode operation.
        """
        self.debug = True
        self.standalone_mode = True
        self.errorstack_key = None

        # XXX We should make the maildir being used be passed in as an argument
        #
        self.maildir = "/Volumes/extra/tmp/testmaildir"
        return


#############################################################################
#
def main():
    """
    Main entry points.
    Sets up our logging system.
    Sets up enough of the user server environment to index all folders
    find_all_folders, update all attributes necessary, and exit.

    Logging is directed to stderr.
    """
    options = TempOptions()
    level = logging.DEBUG
    log = logging.getLogger("asimap")
    log.setLevel(level)
    h = logging.StreamHandler()
    h.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(created)s %(process)d "
        "%(levelname)s %(name)s %(message)s"
    )
    h.setFormatter(formatter)
    log.addHandler(h)
    log.info("Changing to maildir directory '%s'" % options.maildir)
    os.chdir(options.maildir)

    log.info("Instantiating asimap.user_server.IMAPUserServer object")
    server = asimap.user_server.IMAPUserServer(options, options.maildir)

    server.find_all_folders()
    server.check_all_folders(force=True)

    log.info("Finishing all indexing.")
    asyncore.close_all()

    # Close our handle to the sqlite3 database and our MH mailbox.
    #
    server.db.commit()
    server.db.close()
    server.mailbox.close()

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
