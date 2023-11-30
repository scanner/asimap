#!/usr/bin/env python
#
# File: $Id$
#
"""
This module provides an interface between the sqlite3 database we use
to maintain our user and mailbox state and the rest of the user
server.

This database has a very simplistic concept of migrations. There is the list
`MIGRATIONS` and it contains a list of callables. Each callable takes as its
sole parameter the sqlite database connection and performs the migration.

The table `version` contains the index in to this array of the migrations that
have been completed. If the length of the list is greater than the version in
the db, then we need to run all of the migration functions after that index in
the array.

Simple but it works for our very limited set of migrations.
"""
# system imports
#
import logging
import os.path
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Dict

# 3rd party imports
#
import aiosqlite

if TYPE_CHECKING:
    from _typeshed import StrPath

logger = logging.getLogger("asimap.db")

# We compile regexps as we use them and store the compiled object in this dict
# to save us from having to recompile popular regular expressions.
#
# NOTE: If we know what they are ahead of time we should pre-populate this
# dict.
#
USED_REGEXPS: Dict[str, re.Pattern] = {}


####################################################################
#
def regexp(expr, item):
    """
    sqlite supports a regexp syntax but needs us to supply the function to
    use. This is that function.

    Arguments:
    - `expr`: regular expression
    - `item`: item to apply regular expression to
    """
    try:
        if expr in USED_REGEXPS:
            reg = USED_REGEXPS[expr]
        else:
            reg = re.compile(expr)
            USED_REGEXPS[expr] = reg
        return reg.search(item) is not None
    except Exception as e:
        logger.error("exception: %s" % e)
    return None


##################################################################
##################################################################
#
class Database:
    """
    A relatively shallow interface over aiosqlite. The main thing this
    class manages is the central connection to the sqlite db and executing
    necessary migrations when the db is first opened.
    """

    ##################################################################
    #
    def __init__(self, maildir: "StrPath"):
        """
        Opens/creates the sqlite3 database and applies any migrations
        we might need to bring the database up to snuff.

        Arguments:
        - `maildir`: The directory where our database file lives.
        """
        maildir = Path(maildir)
        self.log = logging.getLogger(
            "%s.%s" % (__name__, self.__class__.__name__)
        )
        self.maildir = maildir
        self.db_filename = os.path.join(self.maildir, "asimap.db")
        self.log.debug("Opening database file: '%s'" % self.db_filename)
        self.conn: aiosqlite.Connection

    ####################################################################
    #
    @classmethod
    async def new(cls, maildir: "StrPath") -> "Database":
        """
        Create the database object and do the bits that need to be async.
        """
        db = cls(maildir)
        db.conn = await aiosqlite.connect(
            db.db_filename, detect_types=sqlite3.PARSE_DECLTYPES
        )
        # We want to enable regexp matching in sqlite and in order to do that
        # we have to supply it with a regexp function.
        #
        await db.conn.create_function("REGEXP", 2, regexp, deterministic=True)
        await db.execute("vacuum")

        # Set up the database if necessary. Apply any migrations that
        # we need to.
        #
        await db.apply_migrations()
        return db

    ##################################################################
    #
    async def apply_migrations(self):
        """
        See what version the database is at and apply all migrations
        that we need to bring it up to the highest version level.

        If the versions table does not exist then we need to create it
        first (also means that this is an initial database.)
        """
        version = 0
        try:
            row = await self.fetchone(
                "select version from versions order by version desc limit 1"
            )
            version = int(row["version"]) + 1
        except aiosqlite.OperationalError as e:
            # if we have no versions table then our first migration is 0.
            #
            if str(e) != "no such table: versions":
                raise

        # Apply all the migrations that have not been applied yet.
        #
        for idx, migration in enumerate(MIGRATIONS[version:], start=version):
            self.log.info(
                "Applying migration version %d (%s)" % (idx, migration.__name__)
            )
            await migration(self.conn)
            await self.execute(
                "insert into versions (version) values (?)",
                str(idx),
                commit=True,
            )

    ####################################################################
    #
    async def fetchone(self, sql: str, *args, **kwargs):
        """
        Sometimes we want just the first row of a query. This helper makes
        that simpler.

        The args and kwargs are passed to sqlite's `execute()`

        We have a function separate from `query` because we can not have a
        `return` with a value inside an async generator (ie: once we use
        `yield` we can not use `return <anything>`)
        """
        async with self.conn.execute(sql, *args, **kwargs) as cursor:
            async for row in cursor:
                return row

    ####################################################################
    #
    async def query(self, sql: str, *args, **kwargs):
        """
        An async context manager that yields the rows from the query.

        The args and kwargs are passed to sqlite's `execute()`
        """
        async with self.conn.execute(sql, *args, **kwargs) as cursor:
            async for row in cursor:
                yield row

    ####################################################################
    #
    async def execute(self, sql: str, *args, commit=False, **kwargs):
        """
        This is for operations that will update the db. INSERT, UPDATE,
        DELETE, etc.

        If `commit` is True we do a commit on the db after the successful
        execute. If `commit` is False we assume our caller is going to handle
        when to do the commit.
        """
        await self.conn.execute(sql, *args, **kwargs)
        if commit:
            await self.conn.commit()

    ##################################################################
    #
    async def commit(self):
        await self.conn.commit()

    ##################################################################
    #
    async def close(self):
        await self.conn.close()


##################################################################
##################################################################
#
# Migration functions. These are called in the order they are listed
# in the MIGRATIONS global.
#
# They are handed the db connection and basically just do their work.
# Right now we only handle forward migrations. We will decide if we
# need to revisit this later.
#
####################################################################
#
async def initial_migration(c: aiosqlite.Connection):
    await c.execute(
        "create table versions (version integer primary key, "
        "date text default CURRENT_TIMESTAMP)"
    )
    await c.execute(
        "create table user_server (id integer primary key, "
        "uid_vv integer, "
        "date text default CURRENT_TIMESTAMP)"
    )
    await c.execute(
        "create table mailboxes (id integer primary key, "
        "name text,"
        "uid_vv integer, attributes text, "
        "mtime integer, next_uid integer, "
        "num_msgs integer, num_recent integer, "
        "date text default CURRENT_TIMESTAMP)"
    )
    await c.execute("create unique index mailbox_names on mailboxes (name)")
    await c.execute(
        "create table sequences (id integer primary key, "
        "name text, mailbox_id integer, "
        "sequence text, "
        "date text default CURRENT_TIMESTAMP)"
    )
    await c.execute(
        "create unique index seq_name_mbox on sequences " "(name,mailbox_id)"
    )
    await c.execute("create index seq_mbox_id on sequences (mailbox_id)")


####################################################################
#
async def add_uids_to_mbox(c: aiosqlite.Connection):
    """
    Adds a uids text column to the mailbox.
    """
    await c.execute("alter table mailboxes add column uids text default ''")


####################################################################
#
async def add_last_check_time_to_mbox(c: aiosqlite.Connection):
    """
    Adds a 'last checked' timestamp to the mailbox so we can know how long it
    has been since we last did a resync for a mailbox.

    This field is used to do a better way of 'checking all mailboxes' every
    five minutes. Instead we will queue up checks for mailboxes that have not
    had a resync in five minutes and spread out the load a bit.

    The value is stored as integer seconds since the unix epoch.
    """
    await c.execute(
        "alter table mailboxes add column last_resync integer default 0"
    )


####################################################################
#
async def folders_can_be_subscribed(c: aiosqlite.Connection):
    """
    Folders can be subscribed to. When they are subscribed to this bit gets set
    to true.
    """
    await c.execute(
        "alter table mailboxes add column subscribed integer default 0"
    )


####################################################################
#
async def get_rid_of_root_folder(c: aiosqlite.Connection):
    """
    Due to a now fixed bug in the 'find_all_folders' algorithm we were
    counting the root of the MH mailbox as a folder with an empty
    name.

    This is now fixed but all existing user's have this mailbox laying
    around and we want to get rid of it.
    """
    await c.execute("delete from mailboxes where name=''")


# The list of migrations we have so far. These are executed in order. They are
# executed only once. They are executed when the database is opened. We track
# which ones have been executed and new ones are executed when the database is
# next opened.
#
MIGRATIONS = [
    initial_migration,
    add_uids_to_mbox,
    add_last_check_time_to_mbox,
    folders_can_be_subscribed,
    get_rid_of_root_folder,
]
