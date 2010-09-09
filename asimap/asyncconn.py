#!/usr/bin/env python
#
# File: $Id: asyncconn.py 1931 2009-05-05 17:05:26Z scanner $
#
"""
A wrapper around the asynchat.async_chat class to handle
multiprocess.Connection objects.

NOTE: We are going to be basically using a blocking socket to make
sure we get an entire pickled object at once.
"""

# system imports
#
from collections import deque
import asyncore
import asynchat
import socket
import logging

# By default every file is its own logging module. Kind of simplistic
# but it works for now.
#
log      = logging.getLogger("asimap.%s" % __name__)

##################################################################
##################################################################
#
class conn_wrapper(object):
    """
    Here we override just enough to make a multiprocessing.Connection
    look like a socket for the purposes of asynchat.
    """

    ##################################################################
    #
    def __init__(self, conn):
        """
        
        Arguments:
        - `conn`: The multiprocessing.Connection object we are wrapping.
        """
        self._conn = conn

    ##################################################################
    #
    def recv(self, *args):
        return self._conn.recv()

    ##################################################################
    #
    def send(self, *args):
        return self._conn.send(*args)
    
    read = recv
    write = send

    ##################################################################
    #
    def close(self):
        self._conn.close()

    ##################################################################
    #
    def fileno(self):
        return self._conn.fileno()

##################################################################
##################################################################
#
class async_conn(asynchat.async_chat):
    """
    A wrapper around a mulitprocessing.Connection object providing
    asynchat.async_chat like methods.
    """

    ##################################################################
    #
    def __init__(self, conn, map = None):
        """
        
        Arguments:
        - `conn`: The multiprocessing.Connection object we are wrapping.
        """
        self._conn = conn

        # for string terminator matching
        self.ac_in_buffer = ''

        # we use a list here rather than cStringIO for a few reasons...
        # del lst[:] is faster than sio.truncate(0)
        # lst = [] is faster than sio.truncate(0)
        # cStringIO will be gaining unicode support in py3k, which
        # will negatively affect the performance of bytes compared to
        # a ''.join() equivalent
        self.incoming = []

        # we toss the use of the "simple producer" and replace it with
        # a pure deque, which the original fifo was a wrapping of
        self.producer_fifo = deque()
        
        asyncore.dispatcher.__init__(self, None, map)
        
        self.connected = True
        fd = conn.fileno()
        self.set_file(conn)

        # XXX We are leaving our Connections in blocking mode because
        #     I do not think it will deal well with receiving partial
        #     messages.. although I could be wrong.
        #
#         # set it to non-blocking mode
#         flags = fcntl.fcntl(fd, fcntl.F_GETFL, 0)
#         flags = flags | os.O_NONBLOCK
#         fcntl.fcntl(fd, fcntl.F_SETFL, flags)

        return

    ##################################################################
    #
    def set_file(self, conn):
        """
        
        Arguments:
        - `conn`:
        """
        self.socket = conn_wrapper(conn)
        self._fileno = self.socket.fileno()
        self.add_channel()
    
    ##################################################################
    #
    def _get_data(self):
        """
        Will return and clear the collected data.
        """
        result = self.incoming
        self.incoming = []
        return result

    ##################################################################
    #
    def handle_read(self):
        """
        Handle reading the pickled objects off of our underlying
        connection. This is only called when there is something to
        read.

        XXX Currently we are blocking until it is all read.
        """
        try:
            data = self.socket.recv()
        except socket.error, why:
            self.handle_error()
            return

        # There is no terminator in this instance. We are fully reading a
        # pickled object and blocking until it is all read.
        #
        self.collect_incoming_data(data)
        self.found_terminator()
        return

    ##################################################################
    #
    def push (self, data):
        self.producer_fifo.append(data)
#         self.initiate_send()

    ##################################################################
    #
    def push_with_producer (self, producer):
        self.producer_fifo.append(producer)
#         self.initiate_send()

    ##################################################################
    #
    def initiate_send(self):
        """
        Replacing the default `initiate_send` with our own
        wrapper. Since we are writing pickled objects on an underlying
        multiprocessing.Connection.. and for now we are doing this in
        a blocking fashion this is a much simpler function.
        """
        if self.producer_fifo and self.connected:
            first = self.producer_fifo[0]
            # handle empty string/buffer or None entry
            if not first:
                del self.producer_fifo[0]
                if first is None:
                    self.handle_close()
                    return

            # send the data
            try:
                self.socket.send(first)
            except socket.error:
                self.handle_error()
                return
            
            del self.producer_fifo[0]
        

##################################################################
##################################################################
##
if __name__ == "__main__":
    import doctest
    doctest.testmod()
##
##################################################################
##################################################################
