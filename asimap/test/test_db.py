"""
Test our asyncio sqlite db
"""

# system imports
#
import asyncio

# 3rd party imports
#
import pytest
import pytest_asyncio

# Project imports
#
from ..db import Database


####################################################################
#
@pytest_asyncio.fixture
async def db(tmp_path):
    """
    Fixture that sets up a asimap sqlite db in a temp dir.
    """
    db = None
    try:
        async with asyncio.timeout(5):
            db = await Database.new(tmp_path)
        assert db
        yield db
    finally:
        if db:
            await db.close()


####################################################################
#
@pytest.mark.asyncio
async def test_db_init_migrate(db):
    """
    This actually tests all of the db methods so until we need to test
    something more complex this is good enough unit test for the Database and
    its methods.

    The `new()` method runs the migrations and tests fetchone, execute,
    commit. we check the results via the `query` method as an
    asynccontextmanager.
    """
    schema: dict[str, dict[str, str]] = {}
    async for table in db.query(
        "SELECT name FROM sqlite_schema WHERE type='table'"
    ):
        table_name = table[0]
        schema[table_name] = {}
        async for table_info in db.query(f"PRAGMA table_info({table_name})"):
            schema[table_name][table_info[1]] = table_info[2]
    # NOTE: This requires we update our expected results whenever
    #       migrations change this pseudo-schema we generate.
    expected = {
        "versions": {"version": "INTEGER", "date": "TEXT"},
        "user_server": {
            "id": "INTEGER",
            "uid_vv": "INTEGER",
            "date": "TEXT",
        },
        "mailboxes": {
            "id": "INTEGER",
            "name": "TEXT",
            "uid_vv": "INTEGER",
            "attributes": "TEXT",
            "mtime": "INTEGER",
            "next_uid": "INTEGER",
            "num_msgs": "INTEGER",
            "num_recent": "INTEGER",
            "date": "TEXT",
            "uids": "TEXT",
            "last_resync": "INTEGER",
            "msg_keys": "TEXT",
            "subscribed": "INTEGER",
        },
        "sequences": {
            "id": "INTEGER",
            "name": "TEXT",
            "mailbox_id": "INTEGER",
            "sequence": "TEXT",
            "date": "TEXT",
        },
    }
    assert schema == expected
