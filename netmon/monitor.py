"""One pass over the device list, and the command line around it.

A pass is: ping every device, ask the ones that answered for their interface
status over SNMP, write both down, then let alerts.py read the recent history
back and decide what should be open.

The order matters. SNMP is only asked of devices that answered a ping, because
waiting two seconds for an SNMP reply from a box that is already known to be
down is two seconds spent learning nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import alerts, ping, snmp
from .store import Store

DEFAULT_CONFIG = "devices.json"
DEFAULT_DB = "netmon.db"


@dataclass
class Device:
    name: str
    host: str
    community: str | None = None   # None means "do not ask this device for SNMP"
    if_index: int = 1


@dataclass
class Config:
    devices: list[Device] = field(default_factory=list)
    ping_count: int = ping.DEFAULT_COUNT
    ping_timeout_s: float = ping.DEFAULT_TIMEOUT_S
    interval_s: int = 300
    thresholds: alerts.Thresholds = field(default_factory=alerts.Thresholds)


def load_config(path: str = DEFAULT_CONFIG) -> Config:
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)

    devices = []
    for entry in raw.get("devices", []):
        snmp_cfg = entry.get("snmp") or {}
        devices.append(
            Device(
                name=entry.get("name") or entry["host"],
                host=entry["host"],
                community=snmp_cfg.get("community"),
                if_index=int(snmp_cfg.get("if_index", 1)),
            )
        )
    if not devices:
        raise ValueError("%s lists no devices" % path)

    ping_cfg = raw.get("ping") or {}
    # Unknown keys in the thresholds block are rejected rather than ignored. A
    # typo in a monitoring config that silently leaves the default in place is
    # how a tool ends up not watching what someone believes it is watching.
    given = raw.get("thresholds") or {}
    known = {f for f in alerts.Thresholds.__dataclass_fields__}
    unknown = set(given) - known
    if unknown:
        raise ValueError(
            "unknown threshold(s) %s - valid ones are %s"
            % (", ".join(sorted(unknown)), ", ".join(sorted(known)))
        )

    return Config(
        devices=devices,
        ping_count=int(ping_cfg.get("count", ping.DEFAULT_COUNT)),
        ping_timeout_s=float(ping_cfg.get("timeout_s", ping.DEFAULT_TIMEOUT_S)),
        interval_s=int(raw.get("interval_s", 300)),
        thresholds=alerts.Thresholds(**given),
    )


def check_device(device: Device, config: Config) -> tuple[ping.PingResult, str | None, str | None]:
    result = ping.ping(device.host, config.ping_count, config.ping_timeout_s)
    note = result.error
    if_status: str | None = None

    if device.community and result.reachable:
        try:
            if_status = snmp.interface_status(
                device.host, device.if_index, device.community, config.ping_timeout_s
            )
        except snmp.SnmpError as exc:
            # A device that pings but will not answer SNMP is worth recording
            # and is not worth alerting on: it usually means the community
            # string or an ACL, not an outage.
            note = "%s; %s" % (note, exc) if note else str(exc)

    return result, if_status, note


def run_once(store: Store, config: Config) -> list[str]:
    """Check every device once and return whatever alert events that produced.

    Devices are checked in parallel. Sequentially, each unreachable device
    costs its full timeout, so a handful of dead ones can make a pass take
    longer than the interval between passes.
    """
    history = config.thresholds.slow_streak + config.thresholds.baseline_window + 5

    with ThreadPoolExecutor(max_workers=min(8, len(config.devices))) as pool:
        outcomes = list(pool.map(lambda d: (d, check_device(d, config)), config.devices))

    events: list[str] = []
    for device, (result, if_status, note) in outcomes:
        store.record(
            device.name, result.reachable, result.rtt_ms,
            result.sent, result.received, if_status, note,
        )
        evaluation = alerts.evaluate(store.recent(device.name, history), config.thresholds)
        events.extend(alerts.reconcile(store, device.name, evaluation))

    return events


def print_report(store: Store, hours: int | None) -> None:
    rows = store.uptime(hours)
    if not rows:
        print("nothing recorded yet")
        return

    span = "all time" if hours is None else "last %d h" % hours
    print("uptime report (%s)\n" % span)
    print("%-22s %8s %8s %9s %10s" % ("device", "checks", "uptime", "pkt loss", "avg rtt"))
    print("-" * 61)
    for row in rows:
        rtt = "%.1f ms" % row.avg_rtt_ms if row.avg_rtt_ms is not None else "-"
        print("%-22s %8d %7.1f%% %8.1f%% %10s"
              % (row.device, row.checks, row.uptime_pct, row.loss_pct, rtt))

    open_alerts = store.open_alerts()
    print("\n%d alert(s) open" % len(open_alerts))
    for alert in open_alerts:
        note = " - %s" % alert["note"] if alert["note"] else ""
        print("  %-22s %-12s since %s%s"
              % (alert["device"], alert["kind"], alert["opened_at"][:19], note))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="netmon", description="Ping and SNMP monitoring with history in SQLite."
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    parser.add_argument("-d", "--database", default=DEFAULT_DB)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="one pass, then exit (for cron)")
    mode.add_argument("--watch", action="store_true", help="keep going on the configured interval")
    mode.add_argument("--report", action="store_true", help="print the uptime report and exit")
    parser.add_argument("--hours", type=int, default=24,
                        help="report window in hours; 0 means all of history")
    args = parser.parse_args(argv)

    with Store(args.database) as store:
        if args.report:
            print_report(store, None if args.hours == 0 else args.hours)
            return 0

        try:
            config = load_config(args.config)
        except (OSError, ValueError, KeyError) as exc:
            print("could not read %s: %s" % (args.config, exc), file=sys.stderr)
            return 2

        if not args.watch:
            for event in run_once(store, config):
                print(event)
            return 0

        print("watching %d device(s) every %ds - ctrl-c to stop"
              % (len(config.devices), config.interval_s))
        try:
            while True:
                started = time.monotonic()
                for event in run_once(store, config):
                    print(event, flush=True)
                # Sleep for what is left of the interval, not the whole of it,
                # so a pass that takes 40 seconds does not push the next one
                # 40 seconds late and the one after that 80.
                time.sleep(max(0.0, config.interval_s - (time.monotonic() - started)))
        except KeyboardInterrupt:
            print("\nstopped")
            return 0
