#
# This file is part of LUNA.
#
# Copyright (c) 2020 Great Scott Gadgets <info@greatscottgadgets.com>
# SPDX-License-Identifier: BSD-3-Clause

""" Core stream definitions. """

import unittest

from nmigen          import Elaboratable, Signal, Module
from nmigen.hdl.rec  import Record, DIR_FANIN, DIR_FANOUT
from nmigen.hdl.xfrm import DomainRenamer

from ..stream       import StreamInterface
from ..test         import LunaUSBGatewareTestCase, usb_domain_test_case


class USBInStreamInterface(StreamInterface):
    """ Variant of LUNA's StreamInterface optimized for USB IN transmission.

    This stream interface is nearly identical to StreamInterface, with the following
    restriction: the `valid` signal _must_ be held high for every packet between `first`
    and `last`, inclusively.

    This means that the relevant interface can easily be translated to the UTMI transmit
    signals, with the following mappings:

        Stream  | UTMI
        --------|-----------
        valid   | tx_valid
        payload | tx_data
        ready   | tx_ready
    """

    def bridge_to(self, utmi_tx):
        """ Generates a list of connections that connect this stream to the provided UTMITransmitInterface. """

        return [
            utmi_tx.valid  .eq(self.valid),
            utmi_tx.data   .eq(self.payload),

            self.ready     .eq(utmi_tx.ready)
        ]



class USBOutStreamInterface(Record):
    """ Variant of LUNA's StreamInterface optimized for USB OUT receipt.

    This is a heavily simplified version of our StreamInterface, which omits the 'first',
    'last', and 'ready' signals. Instead, the streamer indicates when data is valid using
    the 'next' signal; and the receiver must keep times.

    This is selected so the relevant interface can easily be translated to the UTMI receive
    signals, with the following mappings:

        UTMI      | Stream
        --------- |-----------
        rx_active | valid
        rx_data   | payload
        rx_valid  | next

    """

    def __init__(self, payload_width=8):
        """
        Parameter:
            payload_width -- The width of the payload packets.
        """
        super().__init__([
            ('valid',    1,             DIR_FANOUT),
            ('next',     1,             DIR_FANOUT),

            ('payload',  payload_width, DIR_FANOUT),
        ])


    def bridge_to(self, utmi_rx):
        """ Generates a list of connections that connect this stream to the provided UTMIReceiveInterface. """

        return [
            self.valid     .eq(utmi_rx.rx_active),
            self.next      .eq(utmi_rx.rx_valid),
            self.data      .eq(utmi_rx.payload)
        ]



class USBOutStreamBoundaryDetector(Elaboratable):
    """ Gateware that detects USBOutStream packet boundaries, and generates First and Last signals.

    As UTMI/ULPI do not denote the last byte of a packet; this module injects two bytes of delay in
    order to correctly identify the last bytes.

    Attributes
    ----------
    unprocessed_stream: USBOutStreamInterface, input stream
        The stream to work with; will be processed and then output on :attr:``processed_stream``.
    processed_stream: USBOutStreamInterface, output stream
        The stream produced by this module. This stream is two bytes delayed from :attr:``unprocessed_stream``;
        and in-phase with the :attr::``first`` and :attr::``last`` signals.

    complete_in: Signal(), input, optional
        Input that accepts an RxComplete signal. If provided; a delayed version will be produced on
        :attr:``complete_out`` after a :attr:``processed_stream`` packet terminates.
    invalid_in: Signal(), input, optional
        Input that accepts an RxInvalid signal. If provided; a delayed version will be produced on
        :attr:``complete_out`` after a :attr:``processed_stream`` packet terminates.


    complete_out: Signal(), output
        If :attr:``complete_in`` is provided; this signal provides a delayed version of that signal
        timed so it is strobed after :attr:``processed_stream`` packets complete.
    invalid_out: Signal(), output
        If :attr:``invalid_out`` is provided; this signal provides a delayed version of that signal
        timed so it is strobed after :attr:``processed_stream`` packets complete.

    first: Signal(), output
        Indicates that the byte present on :attr:``processed_stream`` is the first byte of a packet.
    last: Signal(), output
        Indicates that the byte present on :attr:``processed_stream`` is the last byte of a packet.

    Parameters
    ----------
    domain: str
        The name of the domain the stream belongs to; defaults to "usb".

    """


    def __init__(self, domain="usb"):

        self._domain = domain

        #
        # I/O port
        #
        self.unprocessed_stream = USBOutStreamInterface()
        self.processed_stream   = USBOutStreamInterface()

        self.complete_in        = Signal()
        self.invalid_in         = Signal()

        self.complete_out       = Signal()
        self.invalid_out        = Signal()

        self.first              = Signal()
        self.last               = Signal()


    def elaborate(self, platform):
        m = Module()

        in_stream  = self.unprocessed_stream
        out_stream = self.processed_stream

        # We'll buffer a single byte of the stream, so we can always be one byte ahead.
        buffered_byte = Signal(8)
        is_first_byte = Signal()

        buffered_complete = Signal()
        buffered_invalid  = Signal()

        with m.FSM(domain='usb'):

            # WAIT_FOR_FIRST_BYTE -- we're not actively receiving data, yet. Wait for the
            # first byte of a new packet.
            with m.State('WAIT_FOR_FIRST_BYTE'):
                m.d.usb += out_stream.valid.eq(0)

                m.d.usb += [
                    # We have no data to output, so this can't be our first or last bytes...
                    self.first       .eq(0),
                    self.last        .eq(0),
                    out_stream.next  .eq(0),

                    # ... and we can't have gotten a complete or invalid strobe that matters to us.
                    buffered_complete     .eq(0),
                    buffered_invalid      .eq(0),
                    self.complete_out     .eq(0),
                    self.invalid_out      .eq(0),
                ]

                # Once we've received our first byte, buffer it, and mark it as our first byte.
                with m.If(in_stream.valid & in_stream.next):
                    m.d.usb += [
                        buffered_byte.eq(in_stream.payload),
                        is_first_byte.eq(1)
                    ]
                    m.next = 'RECEIVE_AND_TRANSMIT'

            # RECEIVE_AND_TRANSMIT -- receive incoming bytes, and transmit our buffered bytes.
            # We'll transmit one byte per byte received; ensuring we always retain a single byte --
            # our last byte.
            with m.State('RECEIVE_AND_TRANSMIT'):
                m.d.usb += [
                    out_stream.valid  .eq(1),
                    out_stream.next   .eq(0)
                ]

                # Buffer any complete/invalid signals we get while receiving, so we don't output
                # them before we finish outputting our processed stream.
                m.d.usb += [
                    buffered_complete  .eq(buffered_complete | self.complete_in),
                    buffered_invalid   .eq(buffered_invalid  | self.invalid_in)
                ]

                # If we get a new byte, emit our buffered byte, and store the incoming byte.
                with m.If(in_stream.valid & in_stream.next):
                    m.d.usb += [
                        # Output our buffered byte...
                        out_stream.payload  .eq(buffered_byte),
                        out_stream.next     .eq(1),

                        # indicate whether our current byte was the first byte captured...
                        self.first          .eq(is_first_byte),

                        # ... and store the new, incoming byte.
                        buffered_byte       .eq(in_stream.payload),
                        is_first_byte       .eq(0)
                    ]

                # Once we no longer have an active packet, transmit our _last_ byte,
                # and move back to waiting for an active packet.
                with m.If(~in_stream.valid):
                    m.d.usb += [

                        # Output our buffered byte...
                        out_stream.payload  .eq(buffered_byte),
                        out_stream.next     .eq(1),
                        self.first          .eq(is_first_byte),

                        # ... and indicate that it's the last byte in our stream.
                        self.last           .eq(1)
                    ]
                    m.next = 'OUTPUT_STROBES'

            with m.State('OUTPUT_STROBES'):
                m.d.usb += [
                    # We've just finished transmitting our processed stream; so clear our data strobes...
                    self.first        .eq(0),
                    self.last         .eq(0),
                    out_stream.next   .eq(0),

                    # ... and output our buffered complete/invalid strobes.
                    self.complete_out .eq(buffered_complete),
                    self.invalid_out  .eq(buffered_invalid)
                ]
                m.next = 'WAIT_FOR_FIRST_BYTE'


        if self._domain != "usb":
            m = DomainRenamer({"usb": self._domain})(m)

        return m


class USBOutStreamBoundaryDetectorTest(LunaUSBGatewareTestCase):
    FRAGMENT_UNDER_TEST   = USBOutStreamBoundaryDetector

    @usb_domain_test_case
    def test_boundary_detection(self):
        dut                 = self.dut
        processed_stream    = self.dut.processed_stream
        unprocesesed_stream = self.dut.unprocessed_stream

        # Before we see any data, we should have all of our strobes de-asserted, and an invalid stream.
        self.assertEqual((yield processed_stream.valid), 0)
        self.assertEqual((yield processed_stream.next), 0)
        self.assertEqual((yield dut.first), 0)
        self.assertEqual((yield dut.last), 0)

        # If our stream goes valid...
        yield unprocesesed_stream.valid.eq(1)
        yield unprocesesed_stream.next.eq(1)
        yield unprocesesed_stream.payload.eq(0xAA)
        yield

        # ... we shouldn't see anything this first cycle...
        self.assertEqual((yield processed_stream.valid), 0)
        self.assertEqual((yield processed_stream.next), 0)
        self.assertEqual((yield dut.first), 0)
        self.assertEqual((yield dut.last), 0)

        # ... but after two cycles...
        yield unprocesesed_stream.payload.eq(0xBB)
        yield
        yield unprocesesed_stream.payload.eq(0xCC)
        yield

        # ... we should see a valid stream's first byte.
        self.assertEqual((yield processed_stream.valid), 1)
        self.assertEqual((yield processed_stream.next),  1)
        self.assertEqual((yield processed_stream.payload),  0xAA)
        self.assertEqual((yield dut.first), 1)
        self.assertEqual((yield dut.last), 0)
        yield unprocesesed_stream.payload.eq(0xDD)

        # ... followed by a byte that's neither first nor last...
        yield
        self.assertEqual((yield processed_stream.payload),  0xBB)
        self.assertEqual((yield dut.first), 0)
        self.assertEqual((yield dut.last), 0)

        # Once our stream is no longer valid...
        yield unprocesesed_stream.valid.eq(0)
        yield unprocesesed_stream.next.eq(0)
        yield
        yield

        # ... we should see our final byte.
        self.assertEqual((yield processed_stream.payload),  0xDD)
        self.assertEqual((yield dut.first), 0)
        self.assertEqual((yield dut.last), 1)


if __name__ == "__main__":
    unittest.main()
