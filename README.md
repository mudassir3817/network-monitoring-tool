# netmon

A small network monitoring tool: it pings a list of devices on a schedule, asks
them for their interface status over SNMP, keeps the history in SQLite, and
raises an alert only when something has actually gone wrong.

No dependencies outside the Python standard library. No root. One file of
state.

```
uptime report (last 24 h)

device                   checks   uptime  pkt loss    avg rtt
-------------------------------------------------------------
HQ-R1                       288   100.0%      0.0%     0.9 ms
HQ-SW1                      288   100.0%      0.0%     1.1 ms
BR-R1                       288    97.2%      4.1%    21.4 ms
file-server                 288   100.0%      0.0%     0.4 ms

1 alert(s) open
  BR-R1                  slow         since 2026-11-15T09:14:02 - 48.2 ms against a baseline of 21.1 ms
```

## What it does

**Reachability and loss.** Each check sends several echo requests and records
how many came back and how long they took. Partial loss is recorded as loss,
not as the device being down — two replies out of four is a link with a
problem, not an outage, and the two need different responses.

**Alerts that wait.** Nothing fires on a single bad check. A device has to miss
three checks in a row before an alert opens, and it clears by itself as soon as
one reply comes back. A monitoring tool that pages someone for one dropped
packet is a tool people learn to ignore, which is worse than having none.

**Interface status over SNMP.** A GET of `ifOperStatus` from the IF-MIB, so a
device that answers a ping on one interface while another is down does not read
as healthy.

**Slow before down.** Each device is compared against its own recent average
rather than a fixed threshold, so a branch router at 180 ms over a WAN and a
switch in the rack at under a millisecond can both be watched by the same tool.
Degradation usually arrives before an outage does.

## Running it

```bash
cp devices.example.json devices.json   # then edit it
python -m netmon --once                # one pass, for cron
python -m netmon --watch               # keep going on the configured interval
python -m netmon --report              # uptime for the last 24 hours
python -m netmon --report --hours 0    # all of history
```

`devices.json` lists what to watch. A device with no `snmp` block is pinged
only:

```json
{
  "interval_s": 300,
  "ping": { "count": 3, "timeout_s": 2.0 },
  "thresholds": { "fail_streak": 3, "slow_factor": 2.0 },
  "devices": [
    { "name": "HQ-R1", "host": "192.168.10.1",
      "snmp": { "community": "public", "if_index": 1 } },
    { "name": "BR-R1", "host": "10.0.0.2" }
  ]
}
```

The addresses in `devices.example.json` are the lab from
[small-office-network](../../small-office-network), so the two line up if you
build that in Packet Tracer.

## How it is put together

| module | what it does |
|---|---|
| `ping.py` | runs the OS `ping` and parses what it prints |
| `snmp.py` | builds and reads an SNMPv2c GET, by hand |
| `store.py` | SQLite: every check written down, uptime read back |
| `alerts.py` | decides what should be alerting, from the history |
| `monitor.py` | one pass over the device list, and the command line |

## Design notes

**It shells out to `ping` instead of building ICMP.** An ICMP echo request has
to go out on a raw socket, and a raw socket needs Administrator on Windows or
root on Linux. A tool that only runs as root is a tool that mostly does not get
run. The cost is that ping's output is written for people and differs between
Windows, Linux and macOS, so the parser is pinned by tests against real
captured output from all three — including Windows' `time<1ms`, which a regex
written from memory misses completely.

**The database is the only state.** Nothing is held in memory between runs, so
`--once` from cron every five minutes and `--watch` as a long-running loop
behave identically, and a restart loses nothing. `alerts.evaluate()` is a pure
function of the recent rows: the same history always gives the same answer.

**Alerts are reconciled, not triggered.** Each pass compares what should be
open against what is open and fixes the difference, rather than reacting to the
transition it just saw. A pass that never ran, or a crash halfway through, is
caught up on the next one instead of being lost.

**The latency baseline excludes the readings it is judging.** If it did not, a
slowdown that lasted would quietly pull its own baseline up behind it and the
alert would clear while the device was still slow — the failure where the
monitoring decides everything is fine because everything has been bad for a
while.

**SNMP is only asked of devices that answered a ping.** Waiting two seconds for
an SNMP reply from a box already known to be down costs a timeout per dead
device per pass, which is what makes a pass overrun its own interval during an
outage — exactly when it most needs to keep up.

**SNMP is written out rather than pulled in.** The tool needs one thing from
SNMP: the value of one named object. That is a single GET and a single
response, about 150 lines including the BER encoding, and it keeps the install
to `git clone`. `snmp.py` documents the message structure it builds.

## Tests

```bash
python -m pytest -q
```

100 tests, no network needed. The ping fixtures are real captured output; the
SNMP encoder is checked against a request worked out by hand from the BER rules
rather than against its own output, because a test that only round-trips agrees
with whatever the encoder does, including the parts it does wrong — and over
UDP, a malformed request is indistinguishable from a device being down.

## Limits worth knowing

- **Windows ping reports whole milliseconds** and prints `time<1ms` below that,
  so on a LAN its figures are too coarse for the latency trend to mean much.
  Against WAN devices at tens of milliseconds it is fine. Linux and macOS
  report fractions of a millisecond.
- **SNMPv2c sends the community string in clear text.** It is a label, not a
  secret. Anything that matters belongs on SNMPv3, which authenticates and
  encrypts; that is a much longer piece of work and is not here.
- **One pass, one process.** Devices within a pass are checked in parallel, but
  there is no distribution across machines and no attempt at high availability.
  SQLite takes one writer.
- **No notifications.** Alerts are printed and stored. Wiring them to email or
  a chat webhook would be the next thing.

## Licence

MIT.
