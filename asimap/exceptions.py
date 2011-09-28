#!/usr/bin/env python
#
# File: $Id$
#
"""
Some exceptions need to be generally available to many modules so they are
kept in this module to avoid ciruclar dependencies.
"""

# system imports
#
#######################################################################
#
# We have some basic exceptions used during the processing of commands
# to determine how we respond in exceptional situations
#
class ProtocolException(Exception):
    def __init__(self, value = "protocol exception"):
        self.value = value
    def __str__(self):
        return self.value

class No(ProtocolException):
    def __init__(self, value = "no"):
        self.value = value
    def __str__(self):
        return self.value

class Bad(ProtocolException):
    def __init__(self, value = "bad"):
        self.value = value
    def __str__(self):
        return self.value

