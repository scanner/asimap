#!/usr/bin/env python
#
# Copyright (C) 2015 Eric "Scanner" Luce
#
#
"""This module provides a main routine that will run all of the test suites we
have defined so far for mhimap.

As such this python script can be run as a program directly and it will run
through all of our defined test suites.
"""

import unittest
import mhimap.test.imapparse_test
import mhimap.test.imapsearch_test
import mhimap.test.imapfetch_test
import mhimap.test.functional_test

###########
#
# If we are invoked as a standalone program, just run the test suite defined
# in this module.
#
if __name__ == "__main__":
    imapparse_suite = mhimap.test.imapparse_test.suite()
    imapsearch_suite = mhimap.test.imapsearch_test.suite()
    imapfetch_suite = mhimap.test.imapfetch_test.suite()
    functional_suite = mhimap.test.functional_test.suite()
    all_tests = unittest.TestSuite((imapparse_suite, imapsearch_suite,
                                    imapfetch_suite, functional_suite))
    unittest.TextTestRunner().run(all_tests)
#
#
###########
