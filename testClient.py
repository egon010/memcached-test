#!/usr/bin/env python
"""
Binary memcached test client.

Copyright (c) 2007  Dustin Sallings <dustin@spy.net>
"""

import sys
import time
import socket
import random
import struct
import exceptions

import unittest

from memcacheConstants import REQ_MAGIC_BYTE, RES_MAGIC_BYTE
from memcacheConstants import REQ_PKT_FMT, RES_PKT_FMT, MIN_RECV_PACKET
from memcacheConstants import SET_PKT_FMT, DEL_PKT_FMT, CAS_PKT_FMT
import memcacheConstants

class MemcachedError(exceptions.Exception):
    """Error raised when a command fails."""

    def __init__(self, status, msg):
        supermsg='Memcached error #' + `status`
        if msg: supermsg += ":  " + msg
        exceptions.Exception.__init__(self, supermsg)

        self.status=status
        self.msg=msg

    def __repr__(self):
        return "<MemcachedError #%d ``%s''>" % (self.status, self.msg)

class MemcachedClient(object):
    """Simple memcached client."""

    def __init__(self, host='127.0.0.1', port=11212):
        self.s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.connect_ex((host, port))
        self.r=random.Random()

    def close(self):
        self.s.close()

    def __del__(self):
        self.close()

    def _sendCmd(self, cmd, key, val, opaque, extraHeader=''):
        dtype=0
        msg=struct.pack(REQ_PKT_FMT, REQ_MAGIC_BYTE,
            cmd, len(key), len(extraHeader), dtype,
                len(key) + len(extraHeader) + len(val), opaque)
        self.s.send(msg + extraHeader + key + val)

    def _handleSingleResponse(self, myopaque):
        response=self.s.recv(MIN_RECV_PACKET)
        assert len(response) == MIN_RECV_PACKET
        magic, cmd, errcode, extralen, dtype, remaining, opaque=\
            struct.unpack(RES_PKT_FMT, response)
        rv=self.s.recv(remaining)
        assert magic == RES_MAGIC_BYTE, "Got magic:  %d" % magic
        assert myopaque is None or opaque == myopaque
        if errcode != 0:
            raise MemcachedError(errcode,  rv)
        return opaque, rv

    def _doCmd(self, cmd, key, val, extraHeader=''):
        """Send a command and await its response."""
        opaque=self.r.randint(0, 2**32)
        self._sendCmd(cmd, key, val, opaque, extraHeader)
        return self._handleSingleResponse(opaque)[1]

    def _mutate(self, cmd, key, exp, flags, cas, val):
        self._doCmd(cmd, key, val, struct.pack(SET_PKT_FMT, flags, exp, cas))

    def __incrdecr(self, cmd, key, amt, init, exp):
        return long(self._doCmd(cmd, key, '',
            struct.pack(memcacheConstants.INCRDECR_PKT_FMT, amt, init, exp)))

    def incr(self, key, amt=1, init=0, exp=0):
        """Increment or create the named counter."""
        return self.__incrdecr(memcacheConstants.CMD_INCR, key, amt, init, exp)

    def decr(self, key, amt=1, init=0, exp=0):
        """Decrement or create the named counter."""
        return self.__incrdecr(memcacheConstants.CMD_DECR, key, amt, init, exp)

    def set(self, key, exp, flags, val):
        """Set a value in the memcached server."""
        self._mutate(memcacheConstants.CMD_SET, key, exp, flags, 0, val)

    def add(self, key, exp, flags, val):
        """Add a value in the memcached server iff it doesn't already exist."""
        self._mutate(memcacheConstants.CMD_ADD, key, exp, flags, 0, val)

    def replace(self, key, exp, flags, val):
        """Replace a value in the memcached server iff it already exists."""
        self._mutate(memcacheConstants.CMD_REPLACE, key, exp, flags, 0, val)

    def __parseGet(self, data):
        parts=struct.unpack(memcacheConstants.GET_RES_FMT, data[:12])
        return parts[0], parts[1], data[12:]

    def get(self, key):
        """Get the value for a given key within the memcached server."""
        parts=self._doCmd(memcacheConstants.CMD_GET, key, '')
        return self.__parseGet(parts)

    def cas(self, key, exp, flags, oldVal, val):
        """CAS in a new value for the given key and comparison value."""
        self._mutate(memcacheConstants.CMD_SET, key, exp, flags,
            oldVal, val)

    def version(self):
        """Get the value for a given key within the memcached server."""
        return self._doCmd(memcacheConstants.CMD_VERSION, '', '')

    def getMulti(self, keys):
        """Get values for any available keys in the given iterable.

        Returns a dict of matched keys to their values."""
        opaqued=dict(enumerate(keys))
        terminal=len(opaqued)+10
        # Send all of the keys in quiet
        for k,v in opaqued.iteritems():
            self._sendCmd(memcacheConstants.CMD_GETQ, v, '', k)

        self._sendCmd(memcacheConstants.CMD_NOOP, '', '', terminal)

        # Handle the response
        rv={}
        done=False
        while not done:
            opaque, data=self._handleSingleResponse(None)
            if opaque != terminal:
                rv[opaqued[opaque]]=self.__parseGet(data)
            else:
                done=True

        return rv

    def noop(self):
        """Send a noop command."""
        self._doCmd(memcacheConstants.CMD_NOOP, '', '')

    def delete(self, key, when=0):
        """Delete the value for a given key within the memcached server."""
        self._doCmd(memcacheConstants.CMD_DELETE, key, '',
            struct.pack(DEL_PKT_FMT, when))

    def flush(self):
        """Flush all storage in a memcached instance."""
        self._doCmd(memcacheConstants.CMD_FLUSH, '', '')

class ComplianceTest(unittest.TestCase):

    def setUp(self):
        self.mc=MemcachedClient()
        self.mc.flush()

    def tearDown(self):
        self.mc.flush()
        self.mc.close()

    def testVersion(self):
        """Test the version command returns something."""
        v=self.mc.version()
        self.assertTrue(len(v) > 0, "Bad version:  ``" + str(v) + "''")

    def testSimpleSetGet(self):
        """Test a simple set and get."""
        self.mc.set("x", 5, 19, "somevalue")
        self.assertGet((19, "somevalue"), self.mc.get("x"))

    def assertNotExists(self, key):
        try:
            x=self.mc.get(key)
            self.fail("Expected an exception, got " + `x`)
        except MemcachedError, e:
            self.assertEquals(memcacheConstants.ERR_NOT_FOUND, e.status)

    def assertGet(self, exp, gv):
        self.assertTrue(gv is not None)
        self.assertEquals((gv[0], gv[2]), exp)

    def testDelete(self):
        """Test a set, get, delete, get sequence."""
        self.mc.set("x", 5, 19, "somevalue")
        self.assertGet((19, "somevalue"), self.mc.get("x"))
        self.mc.delete("x")
        self.assertNotExists("x")

    def testReservedDelete(self):
        """Test a delete with a reservation timestamp."""
        self.mc.set("x", 5, 19, "somevalue")
        self.assertGet((19, "somevalue"), self.mc.get("x"))
        self.mc.delete("x", 1)
        self.assertNotExists("x")
        try:
            self.mc.add("x", 5, 19, "ex2")
            self.fail("Expected failure to add during timed delete")
        except MemcachedError, e:
            self.assertEquals(memcacheConstants.ERR_EXISTS, e.status)
        time.sleep(1.1)
        self.mc.add("x", 5, 19, "ex2")

    def testFlush(self):
        """Test flushing."""
        self.mc.set("x", 5, 19, "somevaluex")
        self.mc.set("y", 5, 17, "somevaluey")
        self.assertGet((19, "somevaluex"), self.mc.get("x"))
        self.assertGet((17, "somevaluey"), self.mc.get("y"))
        self.mc.flush()
        self.assertNotExists("x")
        self.assertNotExists("y")

    def testNoop(self):
        """Making sure noop is understood."""
        self.mc.noop()

    def testAdd(self):
        """Test add functionality."""
        self.assertNotExists("x")
        self.mc.add("x", 5, 19, "ex")
        self.assertGet((19, "ex"), self.mc.get("x"))
        try:
            self.mc.add("x", 5, 19, "ex2")
            self.fail("Expected failure to add existing key")
        except MemcachedError, e:
            self.assertEquals(memcacheConstants.ERR_EXISTS, e.status)
        self.assertGet((19, "ex"), self.mc.get("x"))

    def testReplace(self):
        """Test replace functionality."""
        self.assertNotExists("x")
        try:
            self.mc.replace("x", 5, 19, "ex")
            self.fail("Expected failure to replace missing key")
        except MemcachedError, e:
            self.assertEquals(memcacheConstants.ERR_NOT_FOUND, e.status)
        self.mc.add("x", 5, 19, "ex")
        self.assertGet((19, "ex"), self.mc.get("x"))
        self.mc.replace("x", 5, 19, "ex2")
        self.assertGet((19, "ex2"), self.mc.get("x"))

    def testMultiGet(self):
        """Testing multiget functionality"""
        self.mc.add("x", 5, 1, "ex")
        self.mc.add("y", 5, 2, "why")
        vals=self.mc.getMulti('xyz')
        self.assertGet((1, 'ex'), vals['x'])
        self.assertGet((2, 'why'), vals['y'])
        self.assertEquals(2, len(vals))

    def testIncrDoesntExistNoCreate(self):
        """Testing incr when a value doesn't exist (and not creating)."""
        try:
            self.mc.incr("x", exp=memcacheConstants.INCRDECR_SPECIAL)
            self.fail("Expected failure to increment non-existent key")
        except MemcachedError, e:
            self.assertEquals(memcacheConstants.ERR_NOT_FOUND, e.status)
        self.assertNotExists("x")

    def testIncrDoesntExistCreate(self):
        """Testing incr when a value doesn't exist (and we make a new one)"""
        self.assertNotExists("x")
        self.assertEquals(19, self.mc.incr("x", init=19))

    def testDecrDoesntExistNoCreate(self):
        """Testing decr when a value doesn't exist (and not creating)."""
        try:
            self.mc.decr("x", exp=memcacheConstants.INCRDECR_SPECIAL)
            self.fail("Expected failiure to decrement non-existent key.")
        except MemcachedError, e:
            self.assertEquals(memcacheConstants.ERR_NOT_FOUND, e.status)
        self.assertNotExists("x")

    def testDecrDoesntExistCreate(self):
        """Testing decr when a value doesn't exist (and we make a new one)"""
        self.assertNotExists("x")
        self.assertEquals(19, self.mc.decr("x", init=19))

    def testIncr(self):
        """Simple incr test."""
        val=self.mc.incr("x")
        self.assertEquals(0, val)
        val=self.mc.incr("x")
        self.assertEquals(1, val)
        val=self.mc.incr("x", 211)
        self.assertEquals(212, val)
        val=self.mc.incr("x", 2**33)
        self.assertEquals(8589934804L, val)

    def testDecr(self):
        """Simple decr test."""
        val=self.mc.incr("x", init=5)
        self.assertEquals(5, val)
        val=self.mc.decr("x")
        self.assertEquals(4, val)
        val=self.mc.decr("x", 211)
        self.assertEquals(0, val)

    def testCas(self):
        """Test CAS operation."""
        try:
            self.mc.cas("x", 5, 19, 0x7fffffffff, "bad value")
            self.fail("Expected error CASing with no existing value")
        except MemcachedError, e:
            self.assertEquals(memcacheConstants.ERR_NOT_FOUND, e.status)
        self.mc.add("x", 5, 19, "original value")
        flags, i, val=self.mc.get("x")
        self.assertEquals("original value", val)
        try:
            self.mc.cas("x", 5, 19, i+1, "broken value")
            self.fail("Expected error CASing with invalid id")
        except MemcachedError, e:
            self.assertEquals(memcacheConstants.ERR_EXISTS, e.status)
        self.mc.cas("x", 5, 19, i, "new value")
        newflags, newi, newval=self.mc.get("x")
        self.assertEquals("new value", newval)

        # Test a CAS replay
        try:
            self.mc.cas("x", 5, 19, i, "crap value")
            self.fail("Expected error CASing with invalid id")
        except MemcachedError, e:
            self.assertEquals(memcacheConstants.ERR_EXISTS, e.status)
        newflags, newi, newval=self.mc.get("x")
        self.assertEquals("new value", newval)

if __name__ == '__main__':
    unittest.main()
