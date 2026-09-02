"""The iSCSI login probe, against a fake target (#90).

The probe's job is to fail in a way that names the fault, so the tests
that matter here are the failing ones: a refusal must be a `LoginRefused`
carrying the target's own status code, a half-answered handshake must be
a connection error, and a target that never transits to full feature
phase must not be reported as a success.

None of that can be checked against the dev appliance, which is
well-behaved -- so the target is a real socket on the loopback interface
speaking real PDUs, scripted to misbehave. It is not a mock: the bytes go
through `socket.sendall` and come back through `socket.recv`, so the
encoding and framing are exercised, and only the appliance is stood in
for.

No Cinder import, so these run in the dependency-free environment. The
probe itself needs nothing beyond the standard library, which is why it
can live in the functional suite and still be tested here.
"""

import socket
import struct
import threading
import unittest

from tests.functional import iscsi_probe


INITIATOR = "iqn.2005-03.org.open-iscsi:cinder-test"
TARGET = "iqn.2005-10.org.freenas.ctl:volume-test"


def _response(status_class=0, status_detail=0, flags=0x87, tsih=0x1234,
              statsn=1, expcmdsn=7, keys=b""):
    """Build a Login Response PDU the way an appliance would."""
    header = bytearray(iscsi_probe.BHS_LENGTH)
    header[0] = iscsi_probe.OPCODE_LOGIN_RESPONSE
    header[1] = flags
    header[5:8] = struct.pack(">I", len(keys))[1:]
    header[14:16] = struct.pack(">H", tsih)
    header[24:28] = struct.pack(">I", statsn)
    header[28:32] = struct.pack(">I", expcmdsn)
    header[36] = status_class
    header[37] = status_detail
    padded = keys + b"\x00" * (-len(keys) % 4)
    return bytes(header) + padded


class _FakeTarget(object):
    """A loopback socket that answers login PDUs from a script.

    Records what it was sent, so a test can assert on the request as well
    as on how the probe handled the answer -- the encoding is half of what
    this module does, and a probe that sent a malformed PDU to a real
    appliance would be refused for reasons no assertion here would
    explain.
    """

    def __init__(self, script):
        self.script = list(script)
        self.received = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.address, self.port = self._sock.getsockname()

    def __enter__(self):
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._thread.join(timeout=5)
        self._sock.close()

    def _serve(self):
        connection, _peer = self._sock.accept()
        connection.settimeout(5)
        try:
            for reply in self.script:
                self.received.append(self._read_pdu(connection))
                if reply is None:          # hang up mid-handshake
                    return
                connection.sendall(reply)
        except (OSError, EOFError):
            pass
        finally:
            connection.close()

    @staticmethod
    def _read_pdu(connection):
        """Return the header, the declared length, and the padded segment.

        The declared length is kept separate from the bytes rather than
        applied to them. Slicing here would make every later assertion
        about DataSegmentLength circular -- a PDU that declared its
        padding would read back consistently, and the mistake would pass.
        """
        header = b""
        while len(header) < iscsi_probe.BHS_LENGTH:
            chunk = connection.recv(iscsi_probe.BHS_LENGTH - len(header))
            if not chunk:
                raise EOFError
            header += chunk
        declared = struct.unpack(">I", b"\x00" + header[5:8])[0]
        wire = declared + (-declared % 4)
        data = b""
        while len(data) < wire:
            chunk = connection.recv(wire - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return header, declared, data


class TestSuccessfulLogin(unittest.TestCase):
    def test_a_normal_login_yields_the_targets_session_handle(self):
        script = [_response(tsih=0),
                  _response(tsih=0x2222,
                            keys=b"MaxRecvDataSegmentLength=8192\x00"),
                  _response()]           # the logout response
        with _FakeTarget(script) as target:
            with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                   port=target.port) as session:
                self.assertEqual(session["tsih"], 0x2222)
                self.assertEqual(session["keys"]["MaxRecvDataSegmentLength"],
                                 "8192")

    def test_the_first_pdu_carries_the_initiator_and_target_names(self):
        with _FakeTarget([_response(tsih=0), _response(),
                          _response()]) as target:
            with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                   port=target.port):
                pass

        _header, _declared, data = target.received[0]
        keys = dict(pair.split("=", 1)
                    for pair in data.decode().split("\x00") if "=" in pair)
        self.assertEqual(keys["InitiatorName"], INITIATOR)
        self.assertEqual(keys["TargetName"], TARGET)
        self.assertEqual(keys["SessionType"], "Normal")

    def test_a_discovery_login_sends_no_target_name(self):
        # Discovery is the check that separates "the portal is up" from
        # "this initiator may attach", so it must not smuggle a target in.
        with _FakeTarget([_response(tsih=0), _response(),
                          _response()]) as target:
            with iscsi_probe.login(target.address, INITIATOR,
                                   port=target.port):
                pass

        _header, _declared, data = target.received[0]
        self.assertIn(b"SessionType=Discovery\x00", data)
        self.assertNotIn(b"TargetName=", data)

    def test_the_request_is_an_immediate_login_padded_to_four_bytes(self):
        with _FakeTarget([_response(tsih=0), _response(),
                          _response()]) as target:
            with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                   port=target.port):
                pass

        header, declared, wire = target.received[0]
        self.assertEqual(header[0],
                         iscsi_probe.IMMEDIATE
                         | iscsi_probe.OPCODE_LOGIN_REQUEST)

        # The segment on the wire is padded to a 4-byte boundary.
        self.assertEqual(len(wire) % 4, 0)
        # DataSegmentLength is the text without that padding. Declaring
        # the padded figure shifts every later PDU by up to three bytes,
        # and the target answers with a status this side cannot explain.
        self.assertNotEqual(
            declared, len(wire),
            "these key values happen to be 4-aligned, so this test cannot "
            "tell a padded DataSegmentLength from an unpadded one")
        text = wire[:declared]
        self.assertTrue(text.endswith(b"\x00"), "no key terminator")
        self.assertFalse(text.endswith(b"\x00\x00"),
                         "DataSegmentLength counts the padding")

    def test_the_stages_transit_from_security_to_full_feature(self):
        with _FakeTarget([_response(tsih=0), _response(),
                          _response()]) as target:
            with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                   port=target.port):
                pass

        first, second = target.received[0][0], target.received[1][0]
        self.assertEqual(first[1], iscsi_probe.TRANSIT | (0 << 2) | 1)
        self.assertEqual(second[1], iscsi_probe.TRANSIT | (1 << 2) | 3)

    def test_leaving_the_session_logs_out(self):
        with _FakeTarget([_response(tsih=0), _response(),
                          _response()]) as target:
            with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                   port=target.port):
                self.assertEqual(len(target.received), 2)

        self.assertEqual(len(target.received), 3)
        logout = target.received[2][0]
        self.assertEqual(logout[0],
                         iscsi_probe.IMMEDIATE
                         | iscsi_probe.OPCODE_LOGOUT_REQUEST)
        self.assertEqual(logout[1], 0x80)      # close the whole session


class TestRefusal(unittest.TestCase):
    def test_a_refused_login_raises_with_the_targets_own_status(self):
        with _FakeTarget([_response(status_class=2, status_detail=3,
                                    flags=0)]) as target:
            with self.assertRaises(iscsi_probe.LoginRefused) as caught:
                with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                       port=target.port):
                    pass

        self.assertEqual(caught.exception.status_class, 2)
        self.assertEqual(caught.exception.status_detail, 3)

    def test_the_message_names_the_target_the_address_and_the_reason(self):
        # This string is the whole point of the probe: it is what a
        # developer reads instead of "the instance did not boot".
        with _FakeTarget([_response(status_class=2, status_detail=3,
                                    flags=0)]) as target:
            with self.assertRaises(iscsi_probe.LoginRefused) as caught:
                with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                       port=target.port):
                    pass

        message = str(caught.exception)
        self.assertIn(TARGET, message)
        self.assertIn(target.address, message)
        self.assertIn("access list", message)

    def test_a_refusal_at_the_operational_stage_is_still_a_refusal(self):
        # The first PDU can succeed and the second be refused -- a target
        # demanding CHAP, for instance. Checking only the first would
        # report that as a login.
        with _FakeTarget([_response(tsih=0),
                          _response(status_class=2, status_detail=7,
                                    flags=0)]) as target:
            with self.assertRaises(iscsi_probe.LoginRefused) as caught:
                with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                       port=target.port):
                    pass

        self.assertEqual(caught.exception.status_detail, 7)

    def test_a_success_that_never_transits_is_not_a_login(self):
        # Status class 0 with the transit bit clear means the target wants
        # to keep negotiating, not that the session is usable. Yielding
        # here would report a login that never happened -- the exact
        # always-green shape this suite exists to avoid.
        with _FakeTarget([_response(tsih=0), _response(flags=0)]) as target:
            with self.assertRaises(iscsi_probe.LoginRefused):
                with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                       port=target.port):
                    pass

    def test_a_security_stage_that_does_not_transit_stops_there(self):
        """The security phase is one round trip, and says so.

        A target wanting a second one answers the first PDU with status 0
        and the transit bit clear. Reading only the status would send the
        operational PDU into a target still in the security stage, and the
        refusal that came back would name a stage mismatch rather than the
        authentication this side cannot do.
        """
        with _FakeTarget([_response(flags=0, tsih=0)]) as target:
            with self.assertRaises(iscsi_probe.LoginRefused) as caught:
                with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                       port=target.port):
                    pass

        # Reported as an authentication failure, which is what it is: the
        # target wants credentials, and CHAP is #27.
        self.assertEqual(caught.exception.status_class, 2)
        self.assertEqual(caught.exception.status_detail, 1)
        self.assertEqual(len(target.received), 1,
                         "sent a second PDU to a target still negotiating")

    def test_an_unexpected_opcode_is_not_treated_as_a_login(self):
        reply = bytearray(_response())
        reply[0] = 0x21                      # not a Login Response
        with _FakeTarget([bytes(reply)]) as target:
            with self.assertRaises(iscsi_probe.LoginRefused):
                with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                       port=target.port):
                    pass


class TestTransportFailures(unittest.TestCase):
    """A network fault must not be reported as an appliance refusal."""

    def test_a_target_that_hangs_up_raises_a_connection_error(self):
        with _FakeTarget([None]) as target:
            with self.assertRaises(ConnectionError) as caught:
                with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                       port=target.port):
                    pass

        self.assertNotIsInstance(caught.exception,
                                 iscsi_probe.LoginRefused)

    def test_a_truncated_pdu_says_how_far_it_got(self):
        with _FakeTarget([_response()[:20]]) as target:
            with self.assertRaises(ConnectionError) as caught:
                with iscsi_probe.login(target.address, INITIATOR, TARGET,
                                       port=target.port):
                    pass

        self.assertIn("20 of 48", str(caught.exception))

    def test_nothing_listening_raises_oserror_rather_than_refusal(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()

        with self.assertRaises(OSError) as caught:
            with iscsi_probe.login("127.0.0.1", INITIATOR, TARGET,
                                   port=port, timeout=5):
                pass

        self.assertNotIsInstance(caught.exception,
                                 iscsi_probe.LoginRefused)


class TestParseProviderLocation(unittest.TestCase):
    def test_a_single_portal_location(self):
        portals, iqn, lun = iscsi_probe.parse_provider_location(
            "10.0.0.1:3260,1 %s 0" % TARGET)

        self.assertEqual(portals, [("10.0.0.1", 3260)])
        self.assertEqual(iqn, TARGET)
        self.assertEqual(lun, 0)

    def test_every_multipath_address_is_returned_in_order(self):
        # Order is the contract: the inherited _get_iscsi_properties()
        # makes the first entry the singular target_portal a
        # non-multipath connector uses.
        portals, _iqn, _lun = iscsi_probe.parse_provider_location(
            "10.0.0.1:3260;10.0.1.1:3260;10.0.2.1:3260,1 %s 0" % TARGET)

        self.assertEqual(portals, [("10.0.0.1", 3260), ("10.0.1.1", 3260),
                                   ("10.0.2.1", 3260)])

    def test_a_non_default_port_survives(self):
        portals, _iqn, _lun = iscsi_probe.parse_provider_location(
            "10.0.0.1:3261,1 %s 0" % TARGET)

        self.assertEqual(portals, [("10.0.0.1", 3261)])

    def test_something_that_is_not_a_location_is_rejected(self):
        for bad in ("", "nonsense", "10.0.0.1:3260,1 %s" % TARGET,
                    "10.0.0.1,1 %s 0" % TARGET,
                    "10.0.0.1:http,1 %s 0" % TARGET):
            with self.subTest(location=bad):
                with self.assertRaises(ValueError):
                    iscsi_probe.parse_provider_location(bad)


if __name__ == "__main__":
    unittest.main()
