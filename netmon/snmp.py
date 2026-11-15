"""A minimal SNMPv2c GET, written out rather than pulled in.

The tool needs one thing from SNMP: the value of a named object on a device,
in practice ifOperStatus, which says whether an interface is up or down. That
is a single GET request and a single response, so it is implemented here
instead of taking on a dependency - the whole exchange is about 150 lines and
every one of them is readable.

The message is a GetRequest, encoded in BER, sent over UDP to port 161:

    SEQUENCE
        INTEGER        version          1 means v2c (0 would be v1)
        OCTET STRING   community        the shared password, in clear text
        [0] IMPLICIT   GetRequest-PDU   tag 0xA0
            INTEGER    request-id       echoed back, so a late reply to an
                                        earlier request can be told apart
            INTEGER    error-status     0 in a request
            INTEGER    error-index      0 in a request
            SEQUENCE OF VarBind
                SEQUENCE
                    OBJECT IDENTIFIER  what is being asked for
                    NULL               the empty slot the agent fills in

The response is the same shape with tag 0xA2 and the NULL replaced by the
value. error-status is non-zero if the agent refused: 1 tooBig, 2 noSuchName,
3 badValue, 4 readOnly, 5 genErr.

A word on the community string: in v2c it crosses the network unencrypted, so
it is a label rather than a secret. Anything that matters should be on SNMPv3,
which authenticates and encrypts. v2c is implemented here because it is what
is switched on by default on most kit, and because v3's key derivation is a
different and much longer piece of work.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass

# ifOperStatus from IF-MIB: the operational state of an interface. The index of
# the interface is appended, so .1 is the first interface in the table.
IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
# A couple of useful scalars from SNMPv2-MIB. The trailing .0 marks a scalar
# rather than a row in a table.
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"

# The values ifOperStatus can take, from the MIB.
OPER_STATUS = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}

SNMP_PORT = 161


class SnmpError(Exception):
    """The agent refused, did not answer, or answered with something unreadable."""


# --------------------------------------------------------------------- BER out


def _length(n: int) -> bytes:
    """BER length: short form below 128, long form above it.

    Short form is the length in one byte. Long form sets the top bit of the
    first byte and uses the low seven bits as a count of the bytes that follow,
    which then hold the length big-endian. 200 is 0x81 0xC8, not 0xC8 - the
    difference matters because 0xC8 on its own would be read as a long form
    announcing 72 further length bytes.
    """
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _length(len(value)) + value


def _integer(value: int) -> bytes:
    """BER INTEGER: two's complement, shortest form, always signed.

    The signed part is the catch. A request-id of 200 encodes as 0x00 0xC8, not
    0xC8, because a leading byte of 0xC8 has its top bit set and would be read
    back as a negative number.
    """
    raw = value.to_bytes((value.bit_length() // 8) + 1, "big", signed=True)
    return _tlv(0x02, raw)


def _octet_string(text: str) -> bytes:
    return _tlv(0x04, text.encode("ascii"))


def encode_oid(oid: str) -> bytes:
    """BER OBJECT IDENTIFIER.

    Two quirks. The first two arcs share one byte as 40*first + second, because
    the first arc is only ever 0, 1 or 2. And any arc of 128 or more is spread
    over several bytes, seven bits at a time, with the top bit set on every
    byte but the last - so 1.3.6.1.2.1.2.2.1.8 ends up as 2B 06 01 02 01 02 02
    01 08, where 0x2B is 40*1 + 3.
    """
    arcs = [int(part) for part in oid.strip(".").split(".")]
    if len(arcs) < 2:
        raise SnmpError("an OID needs at least two arcs: %r" % oid)

    out = bytearray([40 * arcs[0] + arcs[1]])
    for arc in arcs[2:]:
        if arc < 0x80:
            out.append(arc)
            continue
        chunks = []
        while arc:
            chunks.append(arc & 0x7F)
            arc >>= 7
        chunks.reverse()
        for i, chunk in enumerate(chunks):
            out.append(chunk | (0x80 if i < len(chunks) - 1 else 0))
    return _tlv(0x06, bytes(out))


def build_get(oid: str, community: str = "public", request_id: int = 1) -> bytes:
    """The complete GetRequest message, ready to put on the wire."""
    varbind = _tlv(0x30, encode_oid(oid) + _tlv(0x05, b""))  # 05 00 is NULL
    varbind_list = _tlv(0x30, varbind)
    pdu = _tlv(
        0xA0,
        _integer(request_id) + _integer(0) + _integer(0) + varbind_list,
    )
    return _tlv(0x30, _integer(1) + _octet_string(community) + pdu)


# ---------------------------------------------------------------------- BER in


@dataclass(frozen=True)
class _Tlv:
    tag: int
    value: bytes
    end: int  # where this TLV finishes in the buffer it came from


def _read_tlv(data: bytes, start: int = 0) -> _Tlv:
    if start + 2 > len(data):
        raise SnmpError("truncated SNMP message")
    tag = data[start]
    first = data[start + 1]
    if first < 0x80:
        length, value_start = first, start + 2
    else:
        count = first & 0x7F
        value_start = start + 2 + count
        if value_start > len(data):
            raise SnmpError("truncated SNMP length")
        length = int.from_bytes(data[start + 2 : value_start], "big")
    end = value_start + length
    if end > len(data):
        raise SnmpError("SNMP message is shorter than its own length field")
    return _Tlv(tag, data[value_start:end], end)


def _read_integer(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=True)


def parse_response(message: bytes) -> object:
    """Pull the single value out of a GetResponse, or raise if the agent refused."""
    body = _read_tlv(message).value                      # the outer SEQUENCE

    version = _read_tlv(body)
    community = _read_tlv(body, version.end)
    pdu = _read_tlv(body, community.end)
    if pdu.tag != 0xA2:
        raise SnmpError("expected a GetResponse (0xA2), got tag 0x%02X" % pdu.tag)

    request_id = _read_tlv(pdu.value)
    error_status = _read_tlv(pdu.value, request_id.end)
    code = _read_integer(error_status.value)
    if code != 0:
        raise SnmpError("agent returned error-status %d" % code)

    error_index = _read_tlv(pdu.value, error_status.end)
    varbind_list = _read_tlv(pdu.value, error_index.end)
    varbind = _read_tlv(varbind_list.value)
    name = _read_tlv(varbind.value)
    value = _read_tlv(varbind.value, name.end)

    # These three are how an agent says "I have never heard of that object".
    # They arrive as a successful response, so they have to be checked here
    # rather than left to error-status above.
    if value.tag in (0x80, 0x81, 0x82):
        raise SnmpError("the agent has no such object")
    if value.tag == 0x02:                                  # INTEGER
        return _read_integer(value.value)
    if value.tag in (0x41, 0x42, 0x43, 0x44, 0x45):        # Counter/Gauge/TimeTicks
        return int.from_bytes(value.value, "big")
    if value.tag == 0x04:                                  # OCTET STRING
        return value.value.decode("utf-8", "replace")
    if value.tag == 0x05:                                  # NULL
        return None
    return value.value


# ------------------------------------------------------------------- the wire


def get(host: str, oid: str, community: str = "public", timeout_s: float = 2.0) -> object:
    """Send one GET and return the value, over UDP."""
    message = build_get(oid, community, request_id=int.from_bytes(os.urandom(2), "big"))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout_s)
        try:
            sock.sendto(message, (host, SNMP_PORT))
            reply, _ = sock.recvfrom(4096)
        except socket.timeout as exc:
            # UDP, so silence is the normal failure. It means any of: the
            # device is down, SNMP is switched off, the community string is
            # wrong, or an ACL is dropping it. The protocol cannot tell these
            # apart and neither can this.
            raise SnmpError("no SNMP reply from %s within %gs" % (host, timeout_s)) from exc
        except OSError as exc:
            raise SnmpError("could not reach %s: %s" % (host, exc)) from exc
    return parse_response(reply)


def interface_status(
    host: str, if_index: int = 1, community: str = "public", timeout_s: float = 2.0
) -> str:
    """ifOperStatus for one interface, as a word rather than a number."""
    value = get(host, "%s.%d" % (IF_OPER_STATUS, if_index), community, timeout_s)
    if not isinstance(value, int):
        raise SnmpError("ifOperStatus came back as %r, which is not a status" % (value,))
    return OPER_STATUS.get(value, "unknown(%d)" % value)
