#!/usr/bin/env python
#
# File: $Id$
#
"""
Classes and their supporting methods that represent an IMAP Search
structure.
"""

# system imports
#
import os.path

# asimap imports
#
import asimap.utils

############################################################################
#
class BadSearchOp(Exception):
    def __init__(self, value = "bad search operation"):
        self.value = value
    def __str__(self):
        return "BadSearchOp: %s" % self.value

############################################################################
#
#
class IMAPSearch(object):
    """THis is an IMAPSearch object. It can instantiate all the possible
    criteria for a search of the messages in a mailbox. The possible search
    parameters are as defined in rfc2060.

    What this object does is it takes a provided search strng (that we have
    gotten from a client) and converts it into a Python expression that when
    applied in an execution environment that has various variables representing
    a message can return True or False depending on whether that message
    matches the given search criteria.
    """

    OP_ALL         = 'all'
    OP_AND         = 'and'
    OP_BEFORE      = 'before'
    OP_BODY        = 'body'
    OP_HEADER      = 'header'
    OP_KEYWORD     = 'keyword'
    OP_LARGER      = 'larger'
    OP_MESSAGE_SET = 'message_set'
    OP_NOT         = 'not'
    OP_ON          = 'on'
    OP_OR          = 'or'
    OP_SENTBEFORE  = 'sentbefore'
    OP_SENTON      = 'senton'
    OP_SENTSINCE   = 'sentsince'
    OP_SINCE       = 'since'
    OP_SMALLER     = 'smaller'
    OP_TEXT        = 'text'
    OP_UID         = 'uid'

    VALID_OPS = (OP_ALL, OP_AND, OP_BEFORE, OP_BODY, OP_HEADER, OP_KEYWORD,
                 OP_LARGER, OP_MESSAGE_SET, OP_NOT, OP_ON, OP_OR, OP_SENTON,
                 OP_SENTBEFORE, OP_SENTSINCE, OP_SINCE, OP_SMALLER, OP_TEXT,
                 OP_UID)

    #########################################################################
    #
    def __init__(self, op, **kwargs):
        """This sets up the IMAPSearch object. It must be given at least the
        'search operation' keyword and a bunch of keyword arguments that are
        required for that search operation.
        """

        if op not in self.VALID_OPS:
            raise BadSearchOp("'%s' is not a valid search op" % op)
        self.op = op
        self.args = kwargs

    #########################################################################
    #
    def __repr__(self):
        return "IMAPSearch, operation: %s" % self.op

    #########################################################################
    #
    def __str__(self):
        result = "IMAPSearch('%s'" % self.op
        if self.op in (self.OP_AND, self.OP_OR):
            elt = []
            for search in self.args['search_key']:
                elt.append(str(search))
            result += ', [%s]' % ', '.join(elt)
        elif self.op in (self.OP_NOT):
            result += ', search_key = %s' % self.args['search_key']
        elif self.op in (self.OP_BEFORE, self.OP_ON, self.OP_SENTON,
                         self.OP_SENTBEFORE, self.OP_SENTSINCE, self.OP_SINCE):
            result += ', date = "%s"' % self.args['date']
        elif self.op in (self.OP_LARGER, self.OP_SMALLER):
            result += ', n = %d' % self.args['n']
        elif self.op in (self.OP_TEXT, self.OP_BODY):
            result += ', string = "%s"' % self.args['string']
        elif self.op in (self.OP_HEADER):
            result += ', header = "%s", string = "%s"' % (self.args['header'],
                                                          self.args['string'])
        elif self.op in (self.OP_KEYWORD):
            result += ', keyword = "%s"' % self.args['keyword']
        return result + ")"
            
    
    #########################################################################
    #
    def match(self, message, db_entry, uid, sequence_number, max, uid_max):
        """This will apply the search criteria expressed in the creation of
        this IMAPSearch against the given message.

        A complete message consists of the actual message from the MH folder
        (that we parse in an email object), the database entry that keeps track
        of this message's flags and other relevant fields, its uid (without the
        mailbox uid-vv), and the sequence number of this message in the current
        mailbox.

        This routine will return True if the message matches, False otherwise.

        XXX The way this works right now is HELLISHLY inefficient for many
        XXX queries. Frequently we do not need to load the message object
        XXX some queries do not need to load anything (like message set
        XXX queries.) We should really have our caller do efficiency
        XXX checks on some simple queries.
        """
        self.msg = message
        self.msg_entry = db_entry
        self.uid = uid
        self.uid_max = uid_max
        self.number = sequence_number
        self.max = max

        return getattr(self, '_match_%s' % self.op)()

    #########################################################################
    #########################################################################
    #
    #

    #########################################################################
    #
    def _match_keyword(self):
        """
        True if the given flag is set on this message.
        """
        if self.args['keyword'] in self.msg_entry.flags:
            return True
        return False

    #########################################################################
    #
    def _match_header(self):
        """
        Messages that have a header with the specified field-name (as
        defined in [RFC-822]) and that contains the specified string
        in the [RFC-822] field-body.
        """
        header = self.args["header"]
        if header in self.msg and \
           self.msg[header].lower().find(self.args['string']) != -1:
            return True
        return False
            
    #########################################################################
    #
    def _match_and(self):
        """
        We have a list of search keys. If any of these are false then
        the match is false.
        """
        for search_op in self.args['search_key']:
            if not search_op.match(self.msg, self.msg_entry, self.uid,
                                   self.number, self.max, self.uid_max):
                return False
        return True

    #########################################################################
    #
    def _match_all(self):
        """
        All messages in the mailbox; the default initial key for
        ANDing.
        """
        return True

    #########################################################################
    #
    def _match_or(self):
        """
        We have a list of search keys. If any of these are false then
        the match is false.
        """
        for search_op in self.args['search_key']:
            if search_op.match(self.msg, self.msg_entry, self.uid, self.number,
                               self.max, self.uid_max):
                return True
        return False

    #########################################################################
    #
    def _match_before(self):
        """
        Messages whose internal date is earlier than the specified
        date.
        """
        if self.msg_entry.internal_date < self.args["date"]:
            return True
        return False

    #########################################################################
    #
    def _match_body(self):
        """
        Messages that contain the specified string in the body of the
        message.
        """
        text = self.args['string']
        for msg_part in self.msg.walk():
            if msg_part.is_multipart():
                continue
            if msg_part.get_payload(decode = True).lower().find(text) != -1:
                return True
        return False
            
    #########################################################################
    #
    def _match_larger(self):
        """
        Messages with an [RFC-822] size larger than the specified
        number of octets.
        """
        if os.path.getsize(self.msg_entry.msg_file) > self.args["n"]:
            return True
        return False

    #########################################################################
    #
    def _match_message_set(self):
        """
        Messages with message sequence numbers corresponding to the
        specified message sequence number set

        The sequence will be a list of integers and tuples. An integer
        indicates a specific sequence number. A tuple indicates a range.

        One trick, an integer may be '*' which means the last message
        sequence number in our mailbox.

        XXX This is helishly inefficient. We load all these message
        XXX objects up and never even look at them.
        XXX We should front load our search and have it do short cuts
        XXX for things like 'message set' matches
        """
        for elt in self.args["msg_set"]:
            if isinstance(elt,str) and elt == "*" and self.number == self.max:
                return True
            elif isinstance(elt,int) and elt == self.number:
                return True
            elif isinstance(elt, tuple) and (self.number >= elt[0] and \
                                             self.number <= elt[1]):
                return True
        return False

    #########################################################################
    #
    def _match_not(self):
        """
        Messages that do not match the specified search key.
        """
        return not self.args['search_key'].match(self.msg, self.msg_entry,
                                                 self.uid, self.number,
                                                 self.max, self.uid_max)

    #########################################################################
    #
    def _match_on(self):
        """
        Messages whose internal date is within the specified date.

        NOTE: We use the 'date' aspect of the datetime objects to
        compare 'on'ness. Ie: if they are on the same day. (rfc2060 is
        vague about this and just says what is listed above 'within
        the specific date')
        """
        return self.msg_entry.internal_date.date() == self.args["date"].date()
    
    #########################################################################
    #
    def _match_sentbefore(self):
        """
        Messages whose [RFC-822] Date: header is earlier than the
        specified date.
        """
        if 'date' in self.msg and \
                self.args['date'] > asimap.utils.parsedate(self.msg['date']):
            return True
        return False

    #########################################################################
    #
    def _match_senton(self):
        """
        Messages whose [RFC-822] Date: header is within the specified
        date.
        """
        if 'date' in self.msg and \
                self.args['date'].date() == \
                asimap.utils.parsedate(self.msg['date']).date():
            return True
        return False

    #########################################################################
    #
    def _match_sentsince(self):
        """
        Messages whose [RFC-822] Date: header is later than the
        specified date.
        """
        if 'date' in self.msg and \
                self.args['date'] < asimap.utils.parsedate(self.msg['date']):
            return True
        return False

    #########################################################################
    #
    def _match_since(self):
        """
        Messages whose internal date is within or later than the
        specified date.
        """
        if self.msg_entry.internal_date > self.args["date"] or \
                self.msg_entry.internal_date.date() == self.args["date"].date():
            return True
        return False

    #########################################################################
    #
    def _match_smaller(self):
        """
        Messages with an [RFC-822] size larger than the specified
        number of octets.
        """
        if os.path.getsize(self.msg_entry.msg_file) < self.args["n"]:
            return True
        return False

    #########################################################################
    #
    def _match_text(self):
        """
        Messages that contain the specified string in the header or
        body of the message.
        """
        # Look in the headers.. and if it is not in the headers, look
        # in the body.
        #
        text = self.args['string']
        for header in self.msg.values():
            if header.lower().find(text) != -1:
                return True
        return self._match_body()

    #########################################################################
    #
    def _match_uid(self):
        """
        Messages with unique identifiers corresponding to the
        specified unique identifier set.
        """
        for elt in self.args["msg_set"]:
            if isinstance(elt,str) and elt == "*":
                if self.uid == self.uid_max:
                    return True
            elif isinstance(elt,int) and elt == self.uid:
                return True
            elif isinstance(elt, tuple) and (self.uid >= elt[0] and \
                                             self.uid <= elt[1]):
                return True
        return False
