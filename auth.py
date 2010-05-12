#!/usr/bin/env python
#
# File: $Id: auth.py 1926 2009-04-02 17:00:17Z scanner $
#
"""
This module defines classes that are used by the main server to
authenticate users. You sub-class the BaseAuth class to support
different authentication systems. 

The goal is to pull the nitty gritty logic out of IMAPClient and
Server classes.
"""

# system imports
#
