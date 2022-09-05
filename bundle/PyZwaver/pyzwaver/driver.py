#!/usr/bin/python3
# Copyright 2016 Robert Muth <robert@muth.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 3
# of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

"""
driver.py contains the code interacting directly with serial device
"""

import collections
import logging
import queue
import serial
import threading
import time

from typing import List, Tuple

from pyzwaver import zwave as z
from pyzwaver import zmessage


def MakeSerialDevice(port="/dev/ttyUSB0"):
    dev = serial.Serial(
        port=port,
        baudrate=115200,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        bytesize=serial.EIGHTBITS,
        # blocking but with short timeout
        timeout=0.2)
    # dev.open()
    return dev


def MessageStatsString(history):
    cutoff = 0
    with_can = 0
    total_can = 0
    by_node_cnt = collections.Counter()
    by_node_can = collections.Counter()
    by_node_dur = collections.Counter()
    by_node_bad = collections.Counter()
    by_state = collections.Counter()
    sum_duration = 0
    mm = history
    if cutoff > 0:
        mm = mm[-cutoff:]
    count = len(mm)
    for m in mm:
        node = m.node
        if m.can > 0:
            with_can += 1
            total_can += m.can
            by_node_can[node] += 1
        by_state[m.state] += 1
        by_node_cnt[node] += 1
        if m.WasAborted():
            by_node_bad[node] += 1
        if m.end:
            duration = int(1000.0 * (m.end - m.start))
            by_node_dur[node] += duration
            sum_duration += duration
    out = [
        "processed: %d  with-can: %d (total can: %d) avg-time: %dms" %
        (count, with_can, total_can, sum_duration // count),
        "by state:"
    ]
    for n in sorted(by_state.keys()):
        out.append(" %-20s: %4d" % (n, by_state[n]))

    out.append("node  cnt nt can dur. bad")
    for n in sorted(by_node_cnt.keys()):
        out.append(" %2d: %4d (%3d) %4dms (%3d)" % (
            n, by_node_cnt[n], by_node_can[n],
            by_node_dur[n] // by_node_cnt[n], by_node_bad[n]))
    return "\n".join(out)


class MessageQueueOut:
    """
    MessageQueue for outbound messages. Tries to support
    priorities and fairness.
    """

    def __init__(self):
        self._q = queue.PriorityQueue()
        self._lo_counts = collections.defaultdict(int)
        self._hi_counts = collections.defaultdict(int)
        self._lo_min = 0
        self._hi_min = 0
        self._counter = 0
        self._per_node_size = collections.defaultdict(int)

    def qsize(self):
        return self._q.qsize()

    def qsize_for_node(self, n):
        return self._per_node_size[n]

    def put(self, priority, message):
        if self._q.empty():
            self._lo_counts = collections.defaultdict(int)
            self._hi_counts = collections.defaultdict(int)
            self._lo_min = 0
            self._hi_min = 0

        level, count, node = priority
        if level == 2:
            count = self._hi_counts[node]
            count = max(count + 1, self._hi_min)
            self._hi_counts[node] = count
        elif level == 3:
            count = self._lo_counts[node]
            count = max(count + 1, self._lo_min)
            self._lo_counts[node] = count
        else:
            count = self._counter
            self._counter += 1
        self._per_node_size[node] += 1
        self._q.put(((level, count, node), message))

    def get(self):
        priority, message = self._q.get()
        level = priority[0]
        if level == 2:
            self._hi_min = priority[1]
        elif level == 2:
            self._lo_min = priority[1]
        self._per_node_size[priority[2]] -= 1
        return message

    def __str__(self):
        non_empty = {a: b for a, b in self._per_node_size.items() if b}
        return "Per node queue length: " + str(non_empty)


class Driver(object):
    """
    Driver is responsible for sending and receiving raw
    Z-Wave message (arrays of bytes) to/from a serial
    Z-Wave stick. Some of the messages will not go out to
    any Z-Wave node but will just be local communication
    with the stick.

    Outgoing message can be sent via the SendMessage API
    which will queue them if necessary.
    Incoming messages can be observed by registering a listener.

    The Driver spawns several threads:
    * tx_thread: a sending thread which in a loop picks a message from
      the outgoing queue, sends it, waits for any related
      replies and triggers actions based on the replies
    * rx_thread: a receiving thread which waits from new messages to
      arrive. If the messages are related to a current in_flight
      message it will be handled accordingly otherwise it will
      be put into the _in_queue
    * forwarding_thread: forwards messages in the _in_queue
      to the listeners. We do not do this in the rx_thread because
      we do not want the latter to be blocked.
    """

    def __init__(self, serialDevice):
        self._device = serialDevice
        self._out_queue = MessageQueueOut()  # stuff being send to the stick
        self._raw_history: List[Tuple[int, bool, zmessage.Message, str]] = []
        # a message is copied into this once if makes it into _inflight.
        self._history: List[zmessage.Message] = []
        self._device_idle = True
        self._terminate = False  # True if we want to shut things down
        self._in_queue = queue.Queue()  # stuff coming from the stick unrelated to _inflight
        self._listeners = []   # receive all the stuff from _in_queue

        # Make sure we flush old stuff
        self._ClearDevice()
        self._ClearDevice()

        child_threads = [
            ['_tx_thread', self._DriverSendingThread],
            ['_rx_thread', self._DriverReceivingThread],
            ['_forwarding_thread', self._DriverForwardingThread],
        ]

        # Set up a synchronization barrier for making sure all threads have terminated
        # Each child thread plus the calling thread wait until they're all done
        self._termination_barrier = threading.Barrier(len(child_threads) + 1)

        # Start all the child threads
        for [name, fn] in child_threads:
            thread = threading.Thread(target=lambda: self._run_child_thread(fn),
                    name=fn.__name__, daemon=True)
            thread.start()
            setattr(self, name, thread)

        self._last = None
        self._inflight = zmessage.InflightMessage()

        self._send_lock = threading.Lock()

    def __str__(self):
        out = [str(self._out_queue),
               "inflight: " + str(self._inflight.GetMessage()),
               MessageStatsString(self._history)]
        return "\n".join(out)

    def AddListener(self, listener):
        self._listeners.append(listener)

    def HasInflight(self):
        return self._inflight.GetMessage() is not None

    def History(self):
        return self._history

    def _LogSent(self, ts, m, comment):
        self._raw_history.append((ts, True, m, comment))
        logging.debug("sent: %s", zmessage.PrettifyRawMessage(m))

    def _LogReceived(self, ts, m, comment):
        logging.debug("recv: %s", zmessage.PrettifyRawMessage(m))
        self._raw_history.append((ts, False, m, comment))

    def _RecordInflight(self, m):
        self._history.append(m)

    def SendMessage(self, m: zmessage.Message):
        self._out_queue.put(m.priority, m)

    def WaitUntilAllPreviousMessagesHaveBeenHandled(self):
        lock: threading.Lock = threading.Lock()
        lock.acquire()
        # send dummy message to clear out pipe
        mesg = zmessage.Message(
            None, zmessage.LowestPriority(), lambda _: lock.release(), None)
        self.SendMessage(mesg)
        # wait until semaphore is released by callback
        lock.acquire()

    def Terminate(self):
        """
        Terminate shuts down the driver object.

        """
        lock = threading.Lock()
        lock.acquire()

        def cb(_):
            self._terminate = True
            lock.release()

        # send listeners signal to shutdown
        self._in_queue.put((time.time(), None))
        self.SendMessage(zmessage.Message(
            None, zmessage.LowestPriority(), cb, None))
        lock.acquire()

        # Wait for child threads to terminate
        self._termination_barrier.wait()

        logging.info("Driver terminated")

    def GetInFlightMessage(self):
        """"
        Returns the current outbound message being processed or None.
        """
        return self._inflight.GetMessage()

    def OutQueueString(self):
        out = ["queue length: %d" % self._out_queue.qsize(),
               "by node: %s" % str(self._out_queue)]
        return "\n".join(out)

    def OutQueueSizeForNode(self, n):
        return self._out_queue.qsize_for_node(n)

    def _SendRaw(self, payload, comment=""):
        # if len(payload) >= 5:
        #    if self._last == payload[4]:
        #        time.sleep(SEND_DELAY_LARGE)
        #    self._last = payload[4]

        # logging.info("sending: %s", zmessage.PrettifyRawMessage(payload))
        # TODO: maybe add some delay for non-control payload: len(payload) ==
        # 0)
        # play it safe an use a lock to make sure only one thread
        # is writing at a time.
        with self._send_lock:
            self._LogSent(time.time(), payload, comment)
            self._device.write(payload)
            self._device.flush()

    def _run_child_thread(self, fn):
        name = fn.__name__
        logging.warning("%s started", name)

        delay = .5

        # Run the function continuously, catching exceptions, until we're asked to terminate
        while not self._terminate:
            try:
                fn()
                break
            except Exception:
                logging.exception('%s got exception, sleeping for %s seconds', name, delay)
                # XXX crappy backoff algorithm
                time.sleep(delay)
                if delay < 60:
                    delay *= 2

        # Signal that we're done
        self._termination_barrier.wait()

        logging.warning("%s terminated", name)

    def _DriverSendingThread(self):
        """
        Forwards message from _mq to device.

        """
        while not self._terminate:
            inflight = self._out_queue.get()  # type: zmessage.Message
            if self._inflight.StartMessage(inflight, time.time()):
                self._RecordInflight(inflight)
                self._SendRaw(inflight.payload, "")
                self._inflight.WaitForMessageCompletion()

    def _ClearDevice(self):
        # Note, this is only run at startup so it does
        # not have to go through  _SendRaw()
        self._device.write(zmessage.RAW_MESSAGE_NAK)
        self._device.write(zmessage.RAW_MESSAGE_NAK)
        self._device.write(zmessage.RAW_MESSAGE_NAK)
        self._device.flush()
        self._device.flushInput()
        self._device.flushOutput()

    def _DriverReceivingThread(self):
        buf = b""
        last_sof_arrival = None
        while not self._terminate:
            r = self._device.read(1)
            if not r:
                # logging.warning("received empty message/timeout")
                continue
            buf += r
            m = buf[0:1]
            ts = time.time()
            if m[0] == z.SOF:
                if last_sof_arrival is None:
                    last_sof_arrival = ts
                # see if we have a complete message by trying to extract it
                m = zmessage.ExtracRawMessage(buf)
                if not m:
                    if ts - last_sof_arrival > 2.0:
                        # Note, this does not seem to happen in practice
                        logging.error("incomplete message after 2sec: %s", repr(buf))
                        # make sure we notice when it does
                        assert False
                        last_sof_arrival = None
                    continue
            last_sof_arrival = None
            buf = buf[len(m):]
            next_action, comment = self._inflight.NextActionForReceivedMessage(
                ts, m)
            self._LogReceived(ts, m, comment)
            if next_action == zmessage.DO_ACK:
                self._SendRaw(zmessage.RAW_MESSAGE_ACK)
            elif next_action == zmessage.DO_RETRY:
                # small race here (the message may no longer be around) -
                # should be benign
                self._SendRaw(self._inflight.GetMessage().payload, "re-try")
            elif next_action == zmessage.DO_PROPAGATE:
                self._SendRaw(zmessage.RAW_MESSAGE_ACK)
                self._in_queue.put((ts, m))

    def _DriverForwardingThread(self):
        while True:
            ts, m = self._in_queue.get()
            if m is None:
                break
            for listener in self._listeners:
                listener.put(ts, m)
