#!/usr/bin/env python
#
# File: $Id$
#
"""
Run through a bunch of emails in files, loading them with the
python email module
"""

import json
import mailbox

# system imports
#
import sys
from pathlib import Path

# 3rd party imports
#
from codetiming import Timer


#############################################################################
#
def main():
    """
    Takes two arguments. First is the directory that is the MH maildir.
    The second is the name of an mh folder in that mailbox.

    It will go through and read all of the messages in that folder into memory.

    It will not read messages in sub-folders of the folder.
    """
    mhdir = Path(sys.argv[1])
    folder_name = sys.argv[2]

    mh_mbox = mailbox.MH(mhdir, create=False)
    folder = mh_mbox.get_folder(folder_name)


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
