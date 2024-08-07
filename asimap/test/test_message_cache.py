"""
Test our sized based LRU message cache
"""

# Project imports
#
from ..message_cache import MessageCache


####################################################################
#
def test_message_cache_add(faker, email_factory):
    """
    Simple message caching test.
    """
    NUM_MSGS_PER_FOLDER = 100
    NUM_FOLDERS = 10
    msg_cache = MessageCache()
    # Make sure that the message cache __str__ method works
    #
    assert str(msg_cache)
    folders = {}
    for folder in (faker.word() for _ in range(NUM_FOLDERS)):
        folders[folder] = [email_factory() for _ in range(NUM_MSGS_PER_FOLDER)]
        for msg_key, msg in enumerate(folders[folder]):
            msg_cache.add(folder, msg_key, msg)

    # Make sure that the message cache __str__ method works
    #
    assert str(msg_cache)

    num_msgs = msg_cache.num_msgs
    cur_size = msg_cache.cur_size

    assert num_msgs == NUM_FOLDERS * NUM_MSGS_PER_FOLDER
    assert cur_size > 0

    for folder in folders:
        for msg_key, msg in enumerate(folders[folder]):
            cached_msg = msg_cache.get(folder, msg_key)
            assert msg == cached_msg

            msg_cache.remove(folder, msg_key)
            assert msg_cache.get(folder, msg_key) is None
            assert msg_cache.cur_size < cur_size
            cur_size = msg_cache.cur_size
            num_msgs -= 1
            assert msg_cache.num_msgs == num_msgs
    assert msg_cache.cur_size == 0
    # Make sure that the message cache __str__ method works
    #
    assert str(msg_cache)


####################################################################
#
def test_message_cache_expiry(faker, email_factory):
    """
    Keyword Arguments:
    faker         --
    email_factory --
    """
    NUM_MSGS_PER_FOLDER = 100
    NUM_FOLDERS = 10
    MAX_SIZE = 1_600_000
    msg_cache = MessageCache(max_size=MAX_SIZE)

    folders = {}
    computed_size = 0
    number_msgs = 0

    # Create and add enough messages to the message cache to force it to expire
    # some of the messages.
    #
    for folder in (faker.word() for _ in range(NUM_FOLDERS)):
        folders[folder] = [email_factory() for _ in range(NUM_MSGS_PER_FOLDER)]
        for msg_key, msg in enumerate(folders[folder]):
            msg_cache.add(folder, msg_key, msg)
            computed_size += len(msg.as_string())
            number_msgs += 1

    # All the adds above are guaranteed to cause the cache to purge some
    # messages. So the number of messages in the cache and size of the cache
    # must be less than what we added.
    #
    assert msg_cache.num_msgs < number_msgs
    assert msg_cache.cur_size < computed_size
    assert msg_cache.cur_size <= MAX_SIZE
