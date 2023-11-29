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
    try:
        async with timeout(1):
            db = await Database.new(tmp_path)
        assert db
        schema: Dict[str, Dict[str, str]] = {}
        async with db.conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ) as tables:
            async for table in tables:
                table_name = table[0]
                schema[table_name] = {}
                async with db.conn.execute(
                    f"PRAGMA table_info({table_name})"
                ) as table_infos:
                    async for table_info in table_infos:
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
        await db.close()
