"""
Test our sized based LRU message cache
"""

# Project imports
#
from ..message_cache import MessageCache


####################################################################
#
def test_message_cache(email_factory):
    """
    Simple message caching test.
    """
    msg_cache = MessageCache()
    # XXX gets this test module up for now.. add more tests later.
    assert msg_cache
