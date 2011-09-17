#!/usr/bin/env python
#
# File: $Id$
#
"""
This module provides an interface between the sqlite3 database we use
to maintain our user and mailbox state and the rest of the user
server.
"""

# system imports
#
import sqlite3


##################################################################
##################################################################
#
class Database(object):
    """
    The interface to the database.
    """

    ##################################################################
    #
    def __init__(self, maildir):
        """
        Opens/creates the sqlite3 database and applies any migrations
        we might need to bring the database up to snuff.

        Arguments:
        - `maildir`: The directory where our database file lives.
        """
        self.maildir = maildir
        self.conn = sqlite3.connect(os.path.join(self.maildir, "asimap.db"))

        # Set up the database if necessary. Apply any migrations that
        # we need to.
        #
        self.apply_migrations()
        return

    ##################################################################
    #
    def apply_migrations(self):
        """
        See what version the database is at and apply all migrations
        that we need to bring it up to the highest version level.

        If the versions table does not exist then we need to create it
        first (also means that this is an initial database.)
        """
        version = 0
        try:
            c = self.conn.cursor()
            c.execute("select version from versions "
                      "order desc by version limit 1")
            v = c.fetchone()
            version = int(v[0])
            
        except sqlite3.OperationalError, e:
            # if we have no versions table then our first migration is 0.
            #
            if str(e) != "no such table: versions":
                raise e

        # Apply all the migrations that have not been applied yet.
        #
        for migration in MIGRATIONS[version:]:
            migration(c)
        return

    ##################################################################
    #
    def close(self):
        self.conn.close()
        return

    ##################################################################
    #
    def commit(self):
        self.conn.commit()
        return


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
def initial_migration(c):
    """
    Arguments:
    - `c`: sqlite3 db connection
    """
    c.execute("create table versions (v int primary key)")
    c.commit()


MIGRATIONS = [
    initial_migration,
    ]
