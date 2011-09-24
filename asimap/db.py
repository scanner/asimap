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
import os.path
import logging

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
        self.log = logging.getLogger("%s.%s" % (__name__,
                                                self.__class__.__name__))
        self.maildir = maildir
        self.db_filename = os.path.join(self.maildir, "asimap.db")
        self.log.debug("Opening database file: '%s'" % self.db_filename)
        self.conn = sqlite3.connect(self.db_filename,
                                    detect_types = sqlite3.PARSE_DECLTYPES)
        self.conn.execute("vacuum")

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
                      "order by version desc limit 1")
            v = c.fetchone()
            c.close()
            version = int(v[0]) + 1
        except sqlite3.OperationalError, e:
            # if we have no versions table then our first migration is 0.
            #
            if str(e) != "no such table: versions":
                raise

        # Apply all the migrations that have not been applied yet.
        #
        for idx,migration in enumerate(MIGRATIONS[version:], start=version):
            self.log.debug("Applying migration version %d (%s)" % \
                               (idx, migration.__name__))
            c = self.conn.cursor()
            migration(c)
            c.execute("insert into versions (version) values (?)", str(idx))
            self.conn.commit()
            c.close()

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
    #
    def cursor(self):
        """
        A convenience method that retrieves a cursor for people to use.
        """
        return self.conn.cursor()
        
    
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
    c.execute("create table versions (version integer primary key, "
                                      "date text default CURRENT_TIMESTAMP)")
    c.execute("create table user_server (id integer primary key, "
                                        "uid_vv integer, "
                                        "date text default CURRENT_TIMESTAMP)")
    c.execute("create table mailboxes (name text primary key,"
                                      "uid_vv integer, attributes text, "
                                      "mtime integer, next_uid integer, "
                                      "num_msgs integer, num_recent integer, "
                                      "first_unseen integer, "
                                      "date text default CURRENT_TIMESTAMP)")
    return

# The list of migrations we have so far. These are executed in order. They are
# executed only once. They are executed when the database is opened. We track
# which ones have been executed and new ones are executed when the database is
# next opened.
#
MIGRATIONS = [
    initial_migration,
    ]
