#!/usr/bin/env python
#
# File: $Id$
#
"""
The AS IMAP Daemon. This is intended to be run as root. It provides an
IMAP service that is typically backed by MH mail folders.
"""

# system imports
#

# Application imports
#
from asimap import __version__
import options
import logging

#############################################################################
#
def main():
    """

    """
    
    (options.options, args) = options.parser.parse_args()
    logging.setup_logging(options)

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
