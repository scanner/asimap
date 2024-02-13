"""
Test our asyncio sqlite db
"""

# system imports
#
from typing import Dict

# 3rd party imports
#
import pytest
from async_timeout import timeout

# Project imports
#
from ..db import Database


####################################################################
#
@pytest.mark.asyncio
async def test_db_init_migrate(tmp_path):
    """
    This actually tests all of the db methods so until we need to test
    something more complex this is good enough unit test for the Database and
    its methods.

    The `new()` method runs the migrations and tests fetchone, execute,
    commit. we check the results via the `query` method as an
    asynccontextmanager.
    """
    db = None
    try:
        async with timeout(1):
            db = await Database.new(tmp_path)
        assert db
        schema: Dict[str, Dict[str, str]] = {}
        async for table in db.query(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ):
            table_name = table[0]
            schema[table_name] = {}
            async for table_info in db.query(
                f"PRAGMA table_info({table_name})"
            ):
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
    finally:
        if db:
            await db.close()
