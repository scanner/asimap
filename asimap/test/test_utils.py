"""
Test our util functions
"""
# System imports
#
import asyncio
from queue import SimpleQueue

# 3rd party imports
#
import aiofiles.os
import pytest
from async_timeout import timeout

# Project imports
#
from ..exceptions import Bad
from ..utils import (
    UpgradeableReadWriteLock,
    get_uidvv_uid,
    sequence_set_to_list,
    utime,
    with_timeout,
)


####################################################################
#
@pytest.mark.asyncio
async def test_asyncio_utime(tmp_path):
    """
    We use aiofile's `wrap` to make our own asyncio version of os.utime.
    """
    test_file = tmp_path / "test_file.txt"
    with open(test_file, "w") as f:
        f.write("hello\n")
    mtime = await aiofiles.os.path.getmtime(str(test_file))

    # Set the mtime to 10 seconds ago.
    #
    new_mtime = mtime - 10.0

    await utime(str(test_file), (new_mtime, new_mtime))
    changed_mtime = await aiofiles.os.path.getmtime(str(test_file))

    assert changed_mtime == new_mtime


####################################################################
#
def test_uidvv_uid(faker):
    uid_vals = [
        (faker.pyint(max_value=999999999), faker.pyint(max_value=999999999))
        for _ in range(10)
    ]
    uids = [f"{x:010d}.{y:010d}" for x, y in uid_vals]
    for x, y in zip(uid_vals, uids):
        assert get_uidvv_uid(y) == x

    get_uidvv_uid("  012345.6789   ") == (12345, 6789)


####################################################################
#
def test_sequence_set_to_list(faker):
    # fmt: off
    valid_sequence_sets = [
        (
            ((319128, 319164),(319169, 319186),(319192, 319210),
             (319212, 319252),(319254, 319256),319258,319261,(319263, 319288),
             (319293, 319389),(319392, 319413),(319415, 319418),
             (319420, 319438),(319440, 319445),(319447, 319455),
             (319457, 319459),(319462, 319487),(319491, 319509),
             (319511, 319514),(319517, 319529),(319531, 319532),
             (319535, 319551),(319553, 319558),(319562, 319595),
             (319598, 319612),(319614, 319617),(319621, 319672),
             (319674, 319681),(319685, 319696)),
            319696
        ),
        (
            ((5637, 5648),(5797, 5800),(5810, 5820),(5823, 6507),(6509, 6623),
             6625),
            6625
        ),
        (
            ((152, 165),(168, 171),(177, 180),192,195,197,199,(205, 224),
             (226, 227),(229, 231),(233, 234),(236, 244),(246, 248),260,268,275,
             (278, 279),281,290,303,308,(316, 320),325,(330, 334),"*"),
            336
        ),
        (
            ((3, 6),(10, 15),(17, 18),20,(22, 27),(30, 33),(35, 47),(49, 59),66,
             (68, 69),72,75,77,(79, 80),85,(87, 90),92,(95, 96),(98, 101),
             (104, 107),110,(113, 120),(122, 123),(126, 132),(134, 145),151,
             (153, 158),(160, 167),(169, 172),(174, 175),177,(179, 181),186,
             (189, 190),(194, 197),200,202,(204, 206),(209, 211),(213, 234),
             237,239,242,(252, 253),260,266,(271, "*")),
            272
        ),
        (
            (1100,(1104, 1113),(1115, 1120),(1122, 1129),(1131, 1146),
             (1148, 1159),(1163, 1167),(1169, 1173),1176,(1178, 1181),
             (1183, 1189),(1191, 1216),(1218, 1230),1232,(1234, 1236),1238,1240,
             1242,(1244, 1246),(1248, 1249),1251,1260,1262,1268,1272,
             (1274, 1282),1284,1288,(1291, 1293),1295,(1298, 1300),1305,1307,
             (1309, 1310)),
            1310
        ),
    ]
    # fmt: on
    for seq_set, seq_max in valid_sequence_sets:
        coalesced = sequence_set_to_list(seq_set, seq_max)
        # Every message id in `coalesced` has to be an int that is either in
        # the seq_set as an int, or is inclusively between one of the tuples.
        #
        for elt in seq_set:
            if isinstance(elt, int):
                assert elt in coalesced
            if isinstance(elt, tuple):
                if elt[1] == "*":
                    elt = (elt[0], seq_max)
                for i in range(elt[0], elt[1] + 1):
                    assert i in coalesced
            if elt == "*":
                assert seq_max in coalesced

    # Bad sequences:
    #

    # '*' in sequence set when max_seq is 0. Only valid if this is a uid
    # command
    #
    with pytest.raises(Bad):
        _ = sequence_set_to_list(("*",), 0)
    coalesced = sequence_set_to_list(("*",), 0, uid_cmd=True)
    assert coalesced == [0]

    # Number in sequence set greater than max_seq
    #
    with pytest.raises(Bad):
        _ = sequence_set_to_list(((1, 10), 20), 10)

    # Inside tuple in sequence set we exceed the seq_max.
    #
    bad_seq_sets = (
        (((1, 10),), 5),
        (((10, 1),), 5),
        (((10, "*"),), 0),
    )
    for seq_set, seq_max in bad_seq_sets:
        with pytest.raises(Bad):
            _ = sequence_set_to_list(seq_set, seq_max)

    # But those bad sequence sets are valid for uid commands
    #
    expected = [
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    ]
    for (seq_set, seq_max), exp in zip(bad_seq_sets, expected):
        coalesced = sequence_set_to_list(seq_set, seq_max, uid_cmd=True)
        assert coalesced == exp


####################################################################
#
@pytest.mark.asyncio
@with_timeout(2)
async def test_rwlock_basic():
    """
    oh goody. Testing locking code.  The UpgradeableReadWriteLock is a
    asyncio locking tool. Many tasks can have the read lock, but if any of them
    wants to upgrade their existing read lock to a write lock, no one else can
    have a read lock.
    """
    urw_lock = UpgradeableReadWriteLock()

    # Make sure basic counting code works
    #
    async with urw_lock.read_lock():
        assert urw_lock._readers == 1
        assert urw_lock._want_write == 0
        assert not urw_lock.is_write_locked()
        async with urw_lock.write_lock():
            assert urw_lock._want_write == 0
            assert urw_lock.is_write_locked()
            assert urw_lock.this_task_has_write_lock()

    assert urw_lock._readers == 0
    assert urw_lock._want_write == 0

    # Exception if we see if this task has the write lock when no one has the
    # write lock.
    #
    with pytest.raises(RuntimeError):
        assert not urw_lock.is_write_locked()
        urw_lock.this_task_has_write_lock()

    # Can not get a write lock unless you already have a read lock
    #
    with pytest.raises(RuntimeError):
        async with urw_lock.write_lock():
            print("write lock")


####################################################################
#
@pytest.mark.asyncio
@with_timeout(2)
async def test_rwlock_exceptions():
    """
    Make sure if we get an exception while holding a lock things are
    released properly.
    """
    urw_lock = UpgradeableReadWriteLock()
    try:
        async with urw_lock.read_lock():
            assert urw_lock._readers == 1
            raise RuntimeError("whoop")
    except RuntimeError as exc:
        assert exc.args[0] == "whoop"
    assert urw_lock._readers == 0

    try:
        async with urw_lock.read_lock():
            async with urw_lock.write_lock():
                raise RuntimeError("whoop")
    except RuntimeError as exc:
        assert exc.args[0] == "whoop"
    assert urw_lock._readers == 0
    assert urw_lock._want_write == 0
    assert urw_lock._write_lock_task is None
    assert not urw_lock.is_write_locked()


####################################################################
#
@pytest.mark.asyncio
@with_timeout(2)
async def test_rwlock_two_read_locks():
    """
    Make sure multiple tasks can have the read lock at the same time
    """

    async def parallel_read_locks(
        start_event: asyncio.Event,
        done_event: asyncio.Event,
        urw: UpgradeableReadWriteLock,
    ):
        """
        Acquire the read lock. Signal via an event when we have acquired
        the read lock.
        """
        async with urw.read_lock():
            start_event.set()
            await done_event.wait()
        start_event.set()

    urw_lock = UpgradeableReadWriteLock()
    start_event = asyncio.Event()
    done_event = asyncio.Event()
    read_lock_task = asyncio.create_task(
        parallel_read_locks(start_event, done_event, urw_lock)
    )

    # Get the read lock and then wait for our parallel task to also get the
    # read lock.
    async with urw_lock.read_lock():
        await start_event.wait()

        # At this point two tasks have the read lock.
        #
        assert urw_lock._readers == 2

        # Clear the start_event so we can wait on it again, and signal the
        # done_event so our other task clears the read lock (and then signals
        # that it is done with the read lock.)
        #
        start_event.clear()
        done_event.set()

        # and we wait for the other task to release its read lock.
        #
        await start_event.wait()
        assert urw_lock._readers == 1
    assert urw_lock._readers == 0
    await read_lock_task


####################################################################
#
@pytest.mark.asyncio
@with_timeout(5)
async def test_rwlock_only_one_write_lock():
    """
    There Can Be Only One Task (that holds the write lock)
    """

    async def get_and_hold_the_write_lock(
        start_event: asyncio.Event,
        done_event: asyncio.Event,
        urw: UpgradeableReadWriteLock,
    ):
        async with urw.read_lock():
            start_event.set()  # se #1

            # Wait for the done event which tells us the other task at least
            # acquired the read lock.
            #
            await done_event.wait()  # de #1
            done_event.clear()

            # At this point both tasks have acquired the read lock. Signal the
            # other task to continue by setting the start_event again. This
            # task will try to acquire the write lock and hit the `wait_for` in
            # the lock acquiring code. This will allow the other task to wake
            # up, try to acquire the write lock, succeed (because now all tasks
            # that have read locks are trying to get write locks)
            #
            # The other task will finish the code in the write lock. Release
            # it. At this point there are two read locks and one task wants the
            # write lock, so this task will not be able to acquire the write
            # lock and stay in the `wait_for`. The other task will finish,
            # release the read lock. Then this task will wake up, finish the
            # `wait_for` because there is now 1 read lock and 1 task (this
            # task) wants the write lock.
            #
            # It will get the write lock, finish the write lock clause, release
            # it... finish the read lock clause. Release it. Done.
            #
            assert urw._readers == 2
            start_event.set()  # se #2

            async with urw.write_lock():
                assert urw.is_write_locked()
                assert urw.this_task_has_write_lock()
            assert not urw.is_write_locked()

    urw_lock = UpgradeableReadWriteLock()
    start_event = asyncio.Event()
    done_event = asyncio.Event()
    lock_task = asyncio.create_task(
        get_and_hold_the_write_lock(start_event, done_event, urw_lock)
    )

    # Get the read lock and now wait for the other task to get and hold the
    # write lock (in addition to the read lock)
    #
    await start_event.wait()  # se #1
    start_event.clear()
    async with urw_lock.read_lock():
        # Signal the other task that we have our read lock and it can proceed
        # toget teh write lock. We will wait on start_event until it is done
        # and released the write lock.
        #
        done_event.set()  # de #1
        await start_event.wait()  # se #2
        start_event.clear()

        # At this point the other task is waiting to get the write lock.  It
        # needs the number of tasks wanting the write lock to equal the number
        # of readers that have a read lock.
        #
        # However, it is in `wait_for`. This task will proceed, acquire the
        # write lock (because number of readers is 2, and number of tasks
        # wanting the write lock is 2.)
        #
        assert not urw_lock.is_write_locked()
        assert urw_lock._readers == 2

        async with urw_lock.write_lock():
            # The other task is stuck in the `wait_for` and will not get the
            # write lock until this task exits the read lock.
            #
            assert urw_lock.is_write_locked()
            assert urw_lock.this_task_has_write_lock()
            assert urw_lock._readers == 2

        # At this point still two readers, one task wants the write lock so it
        # can not wake up yet.
        #
        assert not urw_lock.is_write_locked()

    # We now release one of the read locks, this will `notify` and the other
    # task stuck in `wait_for` will wake up.. get the write lock.. and finish.
    #
    # And now wait for the other task to finish.
    #
    await lock_task


####################################################################
#
@pytest.mark.asyncio
async def test_rwlock_can_not_nest_write_locks():
    urw_lock = UpgradeableReadWriteLock()
    async with urw_lock.read_lock():
        async with urw_lock.write_lock():
            with pytest.raises(RuntimeError):
                async with urw_lock.write_lock():
                    pass


####################################################################
#
@pytest.mark.asyncio
@with_timeout(2)
async def test_rwlock_nesting_read_locks():
    """
    You can nest read locks ONLY if they do not try to upgrade to a write
    lock.
    """
    urw_lock = UpgradeableReadWriteLock()
    async with urw_lock.read_lock():
        async with urw_lock.read_lock():
            async with urw_lock.read_lock():
                pass


####################################################################
#
@pytest.mark.asyncio
@with_timeout(2)
async def test_rwlock_nesting_read_locks_w_write_lock():
    """
    This is going to fail with a deadlock because the writelock can never
    be obtained with how the UpgradeableReadWriteLock is written.
    """
    urw_lock = UpgradeableReadWriteLock()
    async with urw_lock.read_lock():
        async with urw_lock.read_lock():
            with pytest.raises(asyncio.TimeoutError):
                async with timeout(1):
                    async with urw_lock.write_lock():
                        pass


####################################################################
#
@pytest.mark.asyncio
@with_timeout(2)
async def test_rwlock_only_one_write_lock_delayed():
    """
    When we have two tasks, and one already has a write lock, the other
    one, only going for a read lock, blocks until the write lock is released.

    We use a SimpleQueue to record the order in which things happened in our
    two async tasks to make sure the order is as expected.
    """

    async def get_write_lock(
        sq: SimpleQueue,
        event_one: asyncio.Event,
        event_two: asyncio.Event,
        urw: UpgradeableReadWriteLock,
    ):
        # Get a read lock. Get the write lock. Send a signal on `event_one`
        # once we have gotten the write lock.
        async with urw_lock.read_lock():
            async with urw_lock.write_lock():
                event_one.set()
                await event_two.wait()
                event_two.clear()
                sq.put(1)

    async def get_read_lock(
        sq: SimpleQueue,
        urw: UpgradeableReadWriteLock,
    ):
        async with urw_lock.read_lock():
            sq.put(2)

    urw_lock = UpgradeableReadWriteLock()
    event_one = asyncio.Event()
    event_two = asyncio.Event()
    sq: SimpleQueue = SimpleQueue()

    # Start the task that immediately goes for a write lock and wait for the
    # event that signals that it has gotten that write lock.
    #
    write_lock_task = asyncio.create_task(
        get_write_lock(sq, event_one, event_two, urw_lock)
    )
    await event_one.wait()
    event_one.clear()

    # At this point task-1 has a write lock. It will be waiting until event_two
    # is signaled. Even if task-2 starts to run immediately it will block
    # attempting to get the read lock (since task-1 has the write lock)
    #
    read_lock_task = asyncio.create_task(get_read_lock(sq, urw_lock))

    # Now unblock task-1 waiting on event_two. This will push `1` on to our
    # SimpleQueue `sq`, release the write lock, and release the read
    # lock. Which should unblock task-2 to letting it push `2` on to `sq`.
    #
    event_two.set()

    await write_lock_task
    await read_lock_task

    # `sq` should have a size of 2. The elements should be `1` and `2` in that
    # order.
    #
    assert sq.qsize() == 2
    assert sq.get() == 1
    assert sq.get() == 2


####################################################################
#
@pytest.mark.asyncio
async def test_rwlock_check_read_lock():
    """
    We can test if a task currently has a read lock.
    """
    urw_lock = UpgradeableReadWriteLock()
    assert urw_lock.this_task_has_read_lock() is False

    async with urw_lock.read_lock():
        assert urw_lock.this_task_has_read_lock() is True
        # We make sure that exceptions while processing do not mess up the task
        # lock tracking.
        #
        try:
            async with urw_lock.read_lock():
                assert urw_lock.this_task_has_read_lock() is True
                raise Exception("hey!")
        except Exception:
            pass
        assert urw_lock.this_task_has_read_lock() is True
    assert urw_lock.this_task_has_read_lock() is False
