"""Reachability and round-trip time, by asking the operating system's own ping.

Why not build ICMP directly: an ICMP echo request has to go out on a raw
socket, and a raw socket needs Administrator on Windows and root (or
CAP_NET_RAW) on Linux. A monitoring tool that only runs as root is a tool that
mostly does not get run, so this shells out to the `ping` that ships with the
operating system and reads what it prints.

What that costs is parsing. Ping's output is written for a person, not for a
program, and the wording differs between Windows, Linux and macOS. So the
parsing here is pinned by tests against real captured output from each one
rather than written from memory - see tests/fixtures.py.

One limit worth knowing: Windows ping reports whole milliseconds and prints
"time<1ms" for anything faster, so on a LAN its figures are too coarse for the
latency trend in alerts.py to mean much. Against real WAN devices, where the
round trip is tens of milliseconds, it is fine. Linux and macOS report
fractions of a millisecond and do not have the problem.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

# How many echo requests one check sends, and how long to wait for each.
DEFAULT_COUNT = 3
DEFAULT_TIMEOUT_S = 2.0

# "Packets: Sent = 3, Received = 2, Lost = 1 (33% loss),"
_WIN_STATS = re.compile(r"Sent\s*=\s*(\d+),\s*Received\s*=\s*(\d+)", re.I)
# "Minimum = 0ms, Maximum = 1ms, Average = 0ms"
_WIN_AVG = re.compile(r"Average\s*=\s*(\d+)\s*ms", re.I)

# "3 packets transmitted, 2 received, 33% packet loss, time 2003ms"
# macOS says "2 packets received"; Linux may insert "+2 errors" before the loss.
_NIX_STATS = re.compile(r"(\d+) packets transmitted,\s*(\d+)\s*(?:packets )?received", re.I)
# "rtt min/avg/max/mdev = 0.045/0.052/0.058/0.005 ms"  (Linux)
# "round-trip min/avg/max/stddev = 0.045/0.052/0.058/0.005 ms"  (macOS)
_NIX_AVG = re.compile(
    r"(?:rtt|round-trip)\s+min/avg/max(?:/mdev|/stddev)?\s*=\s*[\d.]+/([\d.]+)/", re.I
)


@dataclass(frozen=True)
class PingResult:
    """What one check of one device came to."""

    host: str
    sent: int
    received: int
    rtt_ms: float | None
    error: str | None = None

    @property
    def reachable(self) -> bool:
        # One reply is enough to call the device reachable. Partial loss is
        # recorded in `lost` and shows up in the uptime report, but it is not
        # the same thing as the device being down.
        return self.received > 0

    @property
    def lost(self) -> int:
        return max(0, self.sent - self.received)

    @property
    def loss_pct(self) -> float:
        return 100.0 * self.lost / self.sent if self.sent else 100.0


def parse_ping_output(output: str, host: str = "", requested: int = DEFAULT_COUNT) -> PingResult:
    """Read a PingResult out of whatever the local ping printed.

    Both dialects are tried because the output, not the platform, is what is
    actually in hand - which also lets the tests feed in Linux output on a
    Windows machine and the other way round.

    A name that does not resolve produces no statistics block at all, on every
    platform. That is reported as an error rather than as a device that is
    down, because they need different responses: one is a DNS or typo problem,
    the other is a network problem.
    """
    stats = _WIN_STATS.search(output) or _NIX_STATS.search(output)
    if not stats:
        return PingResult(host, requested, 0, None, error=_first_line(output))

    sent, received = int(stats.group(1)), int(stats.group(2))

    rtt: float | None = None
    avg = _WIN_AVG.search(output) or _NIX_AVG.search(output)
    if avg and received > 0:
        rtt = float(avg.group(1))

    return PingResult(host, sent, received, rtt)


def _first_line(output: str) -> str:
    for line in output.splitlines():
        if line.strip():
            return line.strip()
    return "ping produced no output"


def _command(host: str, count: int, timeout_s: float) -> list[str]:
    """The ping invocation for this platform.

    The timeout flag is the awkward part: -w on Windows is milliseconds for the
    whole reply wait, -W on Linux is whole seconds, and -W on macOS is
    milliseconds again. Getting this wrong does not fail loudly - it just waits
    a thousand times too long or not at all - so each is spelled out.
    """
    if sys.platform.startswith("win"):
        return ["ping", "-n", str(count), "-w", str(int(timeout_s * 1000)), host]
    if sys.platform == "darwin":
        return ["ping", "-c", str(count), "-W", str(int(timeout_s * 1000)), host]
    return ["ping", "-c", str(count), "-W", str(max(1, int(timeout_s))), host]


def ping(host: str, count: int = DEFAULT_COUNT, timeout_s: float = DEFAULT_TIMEOUT_S) -> PingResult:
    """Send `count` echo requests to one host and report what came back."""
    try:
        completed = subprocess.run(
            _command(host, count, timeout_s),
            capture_output=True,
            text=True,
            # A backstop only. Ping should have given up long before this; if
            # it has not, something is wrong with it rather than with the
            # device, and a monitoring loop must not block on it.
            timeout=timeout_s * count + 5,
        )
    except subprocess.TimeoutExpired:
        return PingResult(host, count, 0, None, error="ping did not return in time")
    except FileNotFoundError:
        return PingResult(host, count, 0, None, error="no ping command on this system")

    # Windows writes the "could not find host" message to stdout, Linux to
    # stderr, so both are read.
    return parse_ping_output(completed.stdout + completed.stderr, host, count)
