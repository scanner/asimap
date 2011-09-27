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
import re

# We compile regexps as we use them and store the compiled object in this dict
# to save us from having to recompile popular regular expressions.
#
# NOTE: If we know what they are ahead of time we should pre-populate this
# dict.
#
USED_REGEXPS = { }

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
    log = logging.getLogger("%s.regexp()" % __name__)
    try:
        if expr in USED_REGEXPS:
            reg = USED_REGEXPS[expr]
        else:
            reg = re.compile(expr)
            USED_REGEXPS[expr] = reg
        log.debug("testing '%s' against '%s'" % (expr, item))
        return reg.search(item) is not None
    except Exception, e:
        log.error("got exception: %s" % e)
    return None

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
        # We want to enable regexp matching in sqlite and in order to do that
        # we have to supply it with a regexp function.
        #
        self.conn.create_function("REGEXP", 2, regexp)

        # We do some housecleaning when we open the db.
        #
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
    c.execute("create table mailboxes (id integer primary key, "
                                      "name text,"
                                      "uid_vv integer, attributes text, "
                                      "mtime integer, next_uid integer, "
                                      "num_msgs integer, num_recent integer, "
                                      "date text default CURRENT_TIMESTAMP)")
    c.execute("create unique index mailbox_names on mailboxes (name)")
    c.execute("create table sequences (id integer primary key, "
                                      "name text, mailbox_id integer, "
                                      "sequence text, "
                                      "date text default CURRENT_TIMESTAMP)")
    c.execute("create unique index seq_name_mbox on sequences (name,mailbox_id)")
    c.execute("create index seq_mbox_id on sequences (mailbox_id)")
    return

# The list of migrations we have so far. These are executed in order. They are
# executed only once. They are executed when the database is opened. We track
# which ones have been executed and new ones are executed when the database is
# next opened.
#
MIGRATIONS = [
    initial_migration,
    ]
