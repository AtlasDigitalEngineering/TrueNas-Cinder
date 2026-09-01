"""A real iSCSI login, spoken directly rather than through the kernel (#90).

#25 asked for a test initiator to log in with the connection properties
the driver returns, on the grounds that everything short of a real login
is guesswork. That is right, and it was deferred because the obvious way
to do it -- `iscsiadm` -- needs `root`, `open-iscsi` and the kernel's
`iscsi_tcp` module, and leaves node records and possibly a stuck session
behind on a developer's machine.

None of that is actually required. iSCSI's login phase is a short
exchange of text key-value pairs over TCP, defined in RFC 7143 §11, and a
socket can speak it. What this module does is the same conversation
`iscsiadm` has; it simply stops at the point where the kernel would
normally take the session over and present a block device. So:

- no `root`, no `open-iscsi`, no kernel module;
- nothing persistent -- a session is a TCP connection, and closing the
  socket ends it. There is no node database to leave stale entries in;
- it runs on the NixOS workstation, where `iscsiadm` does not exist.

**What it proves.** That the portal address the driver put in
`provider_location` accepts TCP, that the target IQN it built exists
there, and that the appliance's access list admits the initiator -- each
failing distinguishably, which was the point. A rejected login says "the
target refused this initiator", not "the instance did not boot".

**What it does not prove.** That the LUN can be read. That needs SCSI
commands and, to be worth much, a real initiator stack. The end-to-end
run on #20 covers that ground with Nova, os-brick and a booted image;
this covers the part of it that fails most often and localises worst.

One appliance behaviour is worth knowing before reading a failure: ctld
answers *both* "no such target" and "your IQN is not in the access list"
with the same code (class 2, detail 3, "not found"). That is deliberate
on its part -- distinguishing them would tell an unauthorised initiator
which targets exist -- so this module reports what it was told and does
not pretend to know which of the two happened.
"""

import contextlib
import os
import socket
import struct


BHS_LENGTH = 48                       # Basic Header Segment, RFC 7143 §11.1
DEFAULT_PORT = 3260

OPCODE_LOGIN_REQUEST = 0x03
OPCODE_LOGIN_RESPONSE = 0x23
OPCODE_LOGOUT_REQUEST = 0x06
OPCODE_LOGOUT_RESPONSE = 0x26

IMMEDIATE = 0x40                      # I bit; mandatory on login and logout

# Login flags: transit, plus the current and next stage.
TRANSIT = 0x80
STAGE_SECURITY = 0
STAGE_OPERATIONAL = 1
STAGE_FULL_FEATURE = 3

STATUS_CLASSES = {
    0: 'success',
    1: 'redirection',
    2: 'initiator error',
    3: 'target error',
}

# Only the details a driver's own mistakes produce are named. An
# unrecognised code is reported numerically rather than guessed at.
STATUS_DETAILS = {
    (2, 0): 'miscellaneous initiator error',
    (2, 1): 'authentication failure',
    (2, 2): 'authorization failure',
    (2, 3): 'not found -- no such target, or this initiator is not in '
            'its access list (the appliance does not distinguish the two)',
    (2, 4): 'target removed',
    (2, 5): 'unsupported iSCSI version',
    (2, 7): 'missing parameter',
    (2, 9): 'session type not supported',
    (3, 1): 'target has no resources for the session',
    (3, 2): 'target is out of resources',
}


class LoginRefused(Exception):
    """The target answered the login, and said no.

    Distinct from a socket error on purpose: "the appliance refused this
    initiator" and "nothing is listening on that address" are different
    findings with different remedies, and a test that cannot tell them
    apart reports the wrong one.
    """

    def __init__(self, status_class, status_detail, target, address):
        self.status_class = status_class
        self.status_detail = status_detail
        self.target = target
        self.address = address
        super().__init__(
            'iSCSI login to %s at %s refused: %s (class %d, detail %d)'
            % (target or '<discovery>', address,
               STATUS_DETAILS.get(
                   (status_class, status_detail),
                   STATUS_CLASSES.get(status_class, 'unknown status')),
               status_class, status_detail))


def _encode_text(pairs):
    """Render login keys as the null-terminated form the wire uses."""
    return b''.join(('%s=%s\x00' % pair).encode('ascii') for pair in pairs)


def _pad(data):
    """Pad a data segment to the 4-byte boundary every PDU sits on."""
    return data + b'\x00' * (-len(data) % 4)


def _login_request(flags, isid, tsih, cmdsn, expstatsn, pairs):
    """Build one Login Request PDU.

    Args:
        flags: Transit bit and the current/next stage, already combined
        isid: The 6-byte initiator session id
        tsih: 0 on the first PDU, then whatever the target assigned
        cmdsn: Command sequence number
        expstatsn: The next status sequence number expected
        pairs: (key, value) tuples for the data segment

    Returns:
        The complete PDU as bytes
    """
    text = _encode_text(pairs)
    header = bytearray(BHS_LENGTH)
    header[0] = IMMEDIATE | OPCODE_LOGIN_REQUEST
    header[1] = flags
    # VersionMax and VersionMin are both 0x00: iSCSI has exactly one
    # version, and offering anything else is how a login gets refused
    # with "unsupported version".
    header[5:8] = struct.pack('>I', len(text))[1:]
    header[8:14] = isid
    header[14:16] = struct.pack('>H', tsih)
    header[16:20] = struct.pack('>I', 0x636E6472)      # 'cndr', any value
    header[24:28] = struct.pack('>I', cmdsn)
    header[28:32] = struct.pack('>I', expstatsn)
    return bytes(header) + _pad(text)


def _logout_request(cmdsn, expstatsn):
    """Build a Logout Request that closes the whole session.

    Closing the socket would end the session too -- ctld drops it and the
    appliance stops reporting it, verified on the dev box. The logout is
    sent anyway because it is what the protocol asks for, and because a
    target that answers it confirms the session was genuinely established
    rather than merely appearing to be.
    """
    header = bytearray(BHS_LENGTH)
    header[0] = IMMEDIATE | OPCODE_LOGOUT_REQUEST
    header[1] = 0x80                                   # reason: close session
    header[16:20] = struct.pack('>I', 0x636E6472)
    header[24:28] = struct.pack('>I', cmdsn)
    header[28:32] = struct.pack('>I', expstatsn)
    return bytes(header)


def _recv_exactly(sock, count):
    """Read exactly `count` bytes, or say how far it got.

    A short read here means the target hung up mid-PDU, which is a
    different fault from a refusal and must not be reported as one.
    """
    buffer = b''
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise ConnectionError(
                'target closed the connection after %d of %d bytes'
                % (len(buffer), count))
        buffer += chunk
    return buffer


def _read_response(sock):
    """Read one response PDU and return its header fields and keys."""
    header = _recv_exactly(sock, BHS_LENGTH)
    length = struct.unpack('>I', b'\x00' + header[5:8])[0]
    data = _recv_exactly(sock, length + (-length % 4)) if length else b''

    pairs = {}
    for entry in data[:length].decode('ascii', 'replace').split('\x00'):
        if '=' in entry:
            key, value = entry.split('=', 1)
            pairs[key] = value

    return {
        'opcode': header[0] & 0x3F,
        'flags': header[1],
        'tsih': struct.unpack('>H', header[14:16])[0],
        'statsn': struct.unpack('>I', header[24:28])[0],
        'expcmdsn': struct.unpack('>I', header[28:32])[0],
        'status_class': header[36],
        'status_detail': header[37],
        'keys': pairs,
    }


def _stage_flags(current, following):
    """Combine the transit bit with a current and next stage."""
    return TRANSIT | (current << 2) | following


# Operational keys sent in the second PDU. These are the conservative end
# of every range: this side never transfers data, so negotiating for
# throughput would only add ways for the login to be refused.
_OPERATIONAL_KEYS = (
    ('HeaderDigest', 'None'),
    ('DataDigest', 'None'),
    ('MaxRecvDataSegmentLength', '8192'),
    ('DefaultTime2Wait', '2'),
    ('DefaultTime2Retain', '0'),
    ('ErrorRecoveryLevel', '0'),
)

_NORMAL_SESSION_KEYS = (
    ('InitialR2T', 'Yes'),
    ('ImmediateData', 'Yes'),
    ('MaxBurstLength', '262144'),
    ('FirstBurstLength', '65536'),
    ('MaxConnections', '1'),
    ('DataPDUInOrder', 'Yes'),
    ('DataSequenceInOrder', 'Yes'),
    ('MaxOutstandingR2T', '1'),
)


@contextlib.contextmanager
def login(address, initiator, target=None, port=DEFAULT_PORT, timeout=10):
    """Log in to a target, hold the session open, then log out.

    Held open rather than opened and dropped so a caller can cross-check
    the appliance's own view -- `/iscsi/global/sessions` lists the session
    while this context is active, which is a second, independent witness
    that the login reached full feature phase.

    Args:
        address: Portal address, as it appears in `provider_location`
        initiator: The IQN logging in. It must be in the target's
            initiator group or the login is refused.
        target: Full target IQN (`<basename>:<name>`). Omit for a
            discovery session, which needs no access list and proves only
            that the portal is answering.
        port: Portal port, from the appliance's iSCSI global config
        timeout: Socket timeout in seconds

    Yields:
        The negotiated session: `tsih`, and `keys` as the target returned
        them.

    Raises:
        LoginRefused: The target answered and declined
        OSError: Nothing answered at that address and port
    """
    # Random ISID format (top two bits 10b). The remaining bytes only have
    # to be unique among this initiator's concurrent sessions, which the
    # parallel-attach test does create.
    isid = b'\x80\x00' + os.urandom(4)
    sock = socket.create_connection((address, port), timeout=timeout)
    try:
        keys = [('InitiatorName', initiator)]
        if target:
            keys.append(('TargetName', target))
        keys.append(('SessionType', 'Normal' if target else 'Discovery'))
        # No CHAP anywhere yet -- that is #27, deferred past v1. Offering
        # only None means a target configured to demand CHAP refuses here,
        # which is the correct and visible outcome.
        keys.append(('AuthMethod', 'None'))

        sock.sendall(_login_request(
            _stage_flags(STAGE_SECURITY, STAGE_OPERATIONAL),
            isid, 0, 0, 0, keys))
        security = _read_response(sock)
        _check(security, target, address)

        operational = list(_OPERATIONAL_KEYS)
        if target:
            operational += list(_NORMAL_SESSION_KEYS)
        sock.sendall(_login_request(
            _stage_flags(STAGE_OPERATIONAL, STAGE_FULL_FEATURE),
            isid, security['tsih'], 0, security['statsn'] + 1, operational))
        full = _read_response(sock)
        _check(full, target, address)

        if not full['flags'] & TRANSIT:
            raise LoginRefused(2, 0, target, address)

        yield {'tsih': full['tsih'], 'keys': full['keys']}

        sock.sendall(_logout_request(full['expcmdsn'], full['statsn'] + 1))
        # Read it, but do not assert on it: the session is already over
        # from the appliance's side by the time this returns, and a
        # teardown that can fail the test it is tidying up after reports
        # the wrong fault.
        try:
            _read_response(sock)
        except (OSError, ConnectionError):
            pass
    finally:
        sock.close()


def _check(response, target, address):
    """Raise unless the target said yes."""
    if response['opcode'] != OPCODE_LOGIN_RESPONSE:
        raise LoginRefused(2, 0, target, address)
    if response['status_class']:
        raise LoginRefused(response['status_class'],
                           response['status_detail'], target, address)


def parse_provider_location(location):
    """Split a `provider_location` into what a login needs.

    The driver builds this string and the inherited
    `_get_iscsi_properties()` parses it back, so a test that reconstructs
    the address and IQN from configuration instead would be testing its
    own arithmetic. Reading the driver's own output is the point.

    Args:
        location: `"<ip>:<port>[;<ip>:<port>...],<tag> <IQN> <lun>"`

    Returns:
        `(portals, iqn, lun)` where portals is a list of `(address, port)`

    Raises:
        ValueError: The string is not in that form
    """
    try:
        addresses, iqn, lun = location.split(' ')
        portals = addresses.rsplit(',', 1)[0]
    except ValueError:
        raise ValueError('not a provider_location: %r' % location)

    resolved = []
    for entry in portals.split(';'):
        address, _, port = entry.rpartition(':')
        if not address or not port.isdigit():
            raise ValueError('not an address:port in %r: %r'
                             % (location, entry))
        resolved.append((address, int(port)))
    return resolved, iqn, int(lun)
