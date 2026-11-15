"""The SNMP encoder and decoder.

The encoder is checked against a packet worked out by hand from the BER rules
rather than against its own output, because a test that only round-trips
agrees with whatever the encoder does, including the parts it does wrong. A
wrong packet is one no agent would answer, and over UDP that is indis-
tinguishable from a device being down - which is the sort of bug that gets
blamed on the network for a week.
"""
import pytest

from netmon import snmp
from netmon.snmp import (
    SnmpError, _integer, _length, _octet_string, _tlv, build_get, encode_oid,
    interface_status, parse_response,
)


class TestBerLength:
    def test_short_form_below_128(self):
        assert _length(0) == b"\x00"
        assert _length(127) == b"\x7f"

    def test_long_form_from_128(self):
        # 128 is 0x80, which as a length byte would mean "long form, zero
        # following bytes". So it has to go long: one length byte, value 0x80.
        assert _length(128) == b"\x81\x80"
        assert _length(200) == b"\x81\xc8"
        assert _length(300) == b"\x82\x01\x2c"


class TestBerInteger:
    def test_small_values(self):
        assert _integer(0) == b"\x02\x01\x00"
        assert _integer(1) == b"\x02\x01\x01"
        assert _integer(127) == b"\x02\x01\x7f"

    def test_a_leading_zero_is_added_to_keep_it_positive(self):
        # Without the pad, 200 would encode as 0xC8, whose top bit is set, and
        # the agent would read it back as -56.
        assert _integer(128) == b"\x02\x02\x00\x80"
        assert _integer(200) == b"\x02\x02\x00\xc8"
        assert _integer(65535) == b"\x02\x03\x00\xff\xff"


class TestOidEncoding:
    def test_first_two_arcs_share_a_byte(self):
        # 1.3 becomes 40*1 + 3 = 43 = 0x2b.
        assert encode_oid("1.3.6.1.2.1.1.5.0") == bytes.fromhex("06082b06010201010500")

    def test_if_oper_status_with_an_index(self):
        assert encode_oid("1.3.6.1.2.1.2.2.1.8.1") == bytes.fromhex("060a2b06010201020201 0801".replace(" ", ""))

    def test_an_arc_of_128_or_more_spans_several_bytes(self):
        # 311 is 0b10_0110111: low seven bits 0x37, next 0x02, and every byte
        # but the last carries the continuation bit. So 0x82 0x37.
        assert encode_oid("1.3.6.1.4.1.311") == bytes.fromhex("06072b0601040182 37".replace(" ", ""))

    def test_a_leading_dot_is_tolerated(self):
        assert encode_oid(".1.3.6.1.2.1.1.5.0") == encode_oid("1.3.6.1.2.1.1.5.0")

    def test_an_oid_needs_two_arcs(self):
        with pytest.raises(SnmpError):
            encode_oid("1")


class TestBuildGet:
    def test_matches_a_packet_worked_out_by_hand(self):
        """A v2c GET of sysName with community 'public' and request-id 1.

            30 26                          SEQUENCE, 38 bytes follow
               02 01 01                    INTEGER version = 1  (v2c)
               04 06 7075626c6963          OCTET STRING "public"
               a0 19                       GetRequest-PDU, 25 bytes
                  02 01 01                 request-id = 1
                  02 01 00                 error-status = 0
                  02 01 00                 error-index = 0
                  30 0e                    SEQUENCE OF VarBind
                     30 0c                 VarBind
                        06 08 2b06010201010500    sysName.0
                        05 00                     NULL, for the agent to fill
        """
        expected = (
            "3026"
            "020101"
            "04067075626c6963"
            "a019"
            "020101"
            "020100"
            "020100"
            "300e"
            "300c"
            "06082b06010201010500"
            "0500"
        )
        assert build_get("1.3.6.1.2.1.1.5.0", "public", 1).hex() == expected

    def test_is_forty_bytes(self):
        assert len(build_get("1.3.6.1.2.1.1.5.0", "public", 1)) == 40

    def test_community_changes_only_the_community(self):
        a = build_get(snmp.SYS_NAME, "public", 1)
        b = build_get(snmp.SYS_NAME, "secret", 1)
        assert len(a) == len(b) and a != b


def _response(oid, value_tlv, error_status=0, pdu_tag=0xA2, request_id=1):
    """Build a GetResponse the way an agent would, for the decoder to read."""
    varbind = _tlv(0x30, encode_oid(oid) + value_tlv)
    pdu = _tlv(
        pdu_tag,
        _integer(request_id) + _integer(error_status) + _integer(0) + _tlv(0x30, varbind),
    )
    return _tlv(0x30, _integer(1) + _octet_string("public") + pdu)


class TestParseResponse:
    def test_integer_value(self):
        assert parse_response(_response(snmp.IF_OPER_STATUS + ".1", _integer(1))) == 1

    def test_octet_string_value(self):
        message = _response(snmp.SYS_NAME, _tlv(0x04, b"HQ-R1"))
        assert parse_response(message) == "HQ-R1"

    def test_time_ticks_are_unsigned(self):
        # TimeTicks (tag 0x43) counts hundredths of a second since the device
        # booted and is unsigned. Read as signed it goes negative after 248
        # days of uptime, which is exactly when nobody is looking.
        message = _response(snmp.SYS_UPTIME, _tlv(0x43, (3_000_000_000).to_bytes(4, "big")))
        assert parse_response(message) == 3_000_000_000

    def test_an_agent_error_is_raised_not_returned(self):
        # error-status 2 is noSuchName. Returning it as a value would put the
        # number 2 in the database, which happens to mean "interface down".
        with pytest.raises(SnmpError, match="error-status 2"):
            parse_response(_response(snmp.SYS_NAME, _tlv(0x04, b""), error_status=2))

    def test_no_such_object_is_raised(self):
        # Tag 0x80 in the value slot: the agent answered, but does not have
        # that object. Same reasoning as above.
        with pytest.raises(SnmpError, match="no such object"):
            parse_response(_response("1.3.6.1.2.1.99.1", _tlv(0x80, b"")))

    def test_a_pdu_that_is_not_a_response_is_rejected(self):
        with pytest.raises(SnmpError, match="GetResponse"):
            parse_response(_response(snmp.SYS_NAME, _tlv(0x04, b"x"), pdu_tag=0xA0))

    def test_a_truncated_message_is_rejected(self):
        whole = _response(snmp.SYS_NAME, _tlv(0x04, b"HQ-R1"))
        with pytest.raises(SnmpError):
            parse_response(whole[:-4])

    def test_empty_input_is_rejected(self):
        with pytest.raises(SnmpError):
            parse_response(b"")


class TestInterfaceStatus:
    @pytest.mark.parametrize("code,word", [(1, "up"), (2, "down"), (6, "notPresent")])
    def test_codes_become_words(self, monkeypatch, code, word):
        monkeypatch.setattr(snmp, "get", lambda *a, **k: code)
        assert interface_status("192.0.2.1", 1) == word

    def test_an_unknown_code_is_reported_rather_than_hidden(self, monkeypatch):
        monkeypatch.setattr(snmp, "get", lambda *a, **k: 42)
        assert interface_status("192.0.2.1", 1) == "unknown(42)"

    def test_a_non_integer_is_an_error(self, monkeypatch):
        monkeypatch.setattr(snmp, "get", lambda *a, **k: "up")
        with pytest.raises(SnmpError):
            interface_status("192.0.2.1", 1)

    def test_the_index_is_appended_to_the_oid(self, monkeypatch):
        seen = {}

        def record(host, oid, *_a, **_k):
            seen["oid"] = oid
            return 1

        monkeypatch.setattr(snmp, "get", record)
        assert interface_status("192.0.2.1", 7) == "up"
        assert seen["oid"] == snmp.IF_OPER_STATUS + ".7"
