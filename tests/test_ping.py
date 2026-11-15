"""The ping output parser, against real output from all three platforms."""
import sys

import pytest

from netmon.ping import PingResult, parse_ping_output, _command
from tests import fixtures


class TestWindows:
    def test_all_replies(self):
        result = parse_ping_output(fixtures.WINDOWS_OK, "127.0.0.1")
        assert (result.sent, result.received, result.lost) == (3, 3, 0)
        assert result.reachable
        assert result.loss_pct == 0.0
        # "time<1ms" everywhere, so the only figure available is the summary's
        # "Average = 0ms". Reading 0 is right; failing to read anything is not.
        assert result.rtt_ms == 0.0

    def test_partial_loss_is_still_reachable(self):
        result = parse_ping_output(fixtures.WINDOWS_PARTIAL, "10.0.0.2")
        assert (result.sent, result.received, result.lost) == (4, 2, 2)
        assert result.reachable, "two replies out of four is not a device being down"
        assert result.loss_pct == 50.0
        assert result.rtt_ms == 22.0

    def test_total_loss(self):
        result = parse_ping_output(fixtures.WINDOWS_DOWN, "192.0.2.1")
        assert (result.sent, result.received) == (2, 0)
        assert not result.reachable
        assert result.loss_pct == 100.0
        # No replies means no average line to read, and inventing one would be
        # worse than leaving it empty.
        assert result.rtt_ms is None
        assert result.error is None

    def test_slow_but_healthy(self):
        result = parse_ping_output(fixtures.WINDOWS_SLOW, "10.0.0.2")
        assert result.reachable and result.lost == 0
        assert result.rtt_ms == 183.0


class TestLinux:
    def test_all_replies(self):
        result = parse_ping_output(fixtures.LINUX_OK, "192.168.1.1")
        assert (result.sent, result.received) == (3, 3)
        assert result.rtt_ms == pytest.approx(0.351)

    def test_partial_loss(self):
        result = parse_ping_output(fixtures.LINUX_PARTIAL, "10.0.0.2")
        assert (result.sent, result.received, result.lost) == (4, 2, 2)
        assert result.rtt_ms == pytest.approx(19.004)

    def test_total_loss(self):
        result = parse_ping_output(fixtures.LINUX_DOWN, "192.0.2.1")
        assert not result.reachable
        assert result.rtt_ms is None

    def test_error_count_does_not_confuse_the_counts(self):
        # "+2 errors" sits between the received count and the loss percentage.
        result = parse_ping_output(fixtures.LINUX_ERRORS, "192.0.2.1")
        assert (result.sent, result.received) == (2, 0)


class TestMacos:
    def test_all_replies(self):
        # "3 packets received", not "3 received"; "round-trip", not "rtt".
        result = parse_ping_output(fixtures.MACOS_OK, "192.168.1.1")
        assert (result.sent, result.received) == (3, 3)
        assert result.rtt_ms == pytest.approx(1.456)

    def test_total_loss(self):
        result = parse_ping_output(fixtures.MACOS_DOWN, "192.0.2.1")
        assert not result.reachable


class TestNameThatDoesNotResolve:
    """A name that will not resolve is not a device that is down.

    One is a DNS or typing problem and needs a person; the other is a network
    problem and needs an engineer. Reporting the first as the second sends
    somebody to look at a device that may not exist.
    """

    @pytest.mark.parametrize(
        "output",
        [fixtures.WINDOWS_BAD_NAME, fixtures.LINUX_BAD_NAME, fixtures.MACOS_BAD_NAME],
    )
    def test_reported_as_an_error_not_a_down_device(self, output):
        result = parse_ping_output(output, "no-such-host-xyz.invalid", requested=3)
        assert result.error is not None
        assert "no-such-host-xyz.invalid" in result.error
        assert not result.reachable
        assert result.sent == 3, "the requested count is kept, so loss still reads sensibly"

    def test_empty_output_does_not_crash(self):
        result = parse_ping_output("", "nowhere")
        assert result.error and not result.reachable


class TestResultArithmetic:
    def test_loss_percentage_of_nothing_sent(self):
        # Guards a division by zero on a path that only happens when ping
        # itself could not be run at all.
        assert PingResult("h", 0, 0, None).loss_pct == 100.0

    def test_lost_never_goes_negative(self):
        # If a platform ever reports more replies than requests, the count is
        # wrong but the tool should not start reporting negative loss.
        assert PingResult("h", 2, 3, 1.0).lost == 0


class TestCommandLine:
    """The timeout flag differs on every platform and fails quietly when wrong."""

    def test_windows_uses_milliseconds(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert _command("h", 3, 2.0) == ["ping", "-n", "3", "-w", "2000", "h"]

    def test_macos_uses_milliseconds(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert _command("h", 3, 2.0) == ["ping", "-c", "3", "-W", "2000", "h"]

    def test_linux_uses_whole_seconds(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert _command("h", 3, 2.0) == ["ping", "-c", "3", "-W", "2", "h"]

    def test_linux_never_asks_for_a_zero_second_timeout(self, monkeypatch):
        # int(0.4) is 0, and -W 0 on Linux means "wait forever", which in a
        # monitoring loop means "hang".
        monkeypatch.setattr(sys, "platform", "linux")
        assert _command("h", 1, 0.4) == ["ping", "-c", "1", "-W", "1", "h"]
