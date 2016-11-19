"""
Apricot Systematic IMAP server
"""

__version__ = "1.0.1rc3"
__authors__ = ["Scanner Luce <scanner@apricot.com>"]

# import logging

# ##################################################################
# ##################################################################
# #
# class NullHandler(logging.Handler):
#     """
#     A null logging handler that does nothing.. this is so that if a
#     logging handler is not defined by our caller we log nothing.
#     """
#     def emit(self, record):
#         pass

# # A the null handler to the top level name space for our library. Then
# # all submodules will use that if no handler is set by our caller
# #
# # NOTE: This code gets run when this module is imported.
# #
# h = NullHandler()
# logging.getLogger("asimap").addHandler(h)
