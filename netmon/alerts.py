"""When a run of bad checks becomes an alert, and when it stops being one.

The whole point of this module is to not cry wolf. A single dropped echo reply
is ordinary - it happens on any busy link - and a monitoring tool that pages
somebody for one is a monitoring tool people start ignoring, which is worse
than having none. So nothing here reacts to a single check:

  unreachable   the last `fail_streak` checks in a row all failed
  slow          the last `slow_streak` checks in a row were all more than
                `slow_factor` times the device's own recent baseline

Both clear by themselves as soon as the condition stops holding, so a device
that comes back does not leave an alert behind for someone to tidy up.

Two decisions worth spelling out.

The baseline is per device, not global. A branch router 2,000 km away over a
WAN might sit at 180 ms while the switch in the rack answers in under 1 ms;
a single threshold for both is either useless for one or deafening for the
other. Comparing a device against its own recent history is what makes "slow"
mean anything.

The baseline excludes the readings being judged. If it did not, a slowdown
that lasts would quietly pull its own baseline up behind it and the alert
would clear while the device was still slow - the failure mode where the
monitoring agrees that everything is fine because everything has been bad for
a while.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

UNREACHABLE = "unreachable"
SLOW = "slow"


class CheckLike(Protocol):
    """What evaluate() needs from a check - store.Check satisfies this."""

    reachable: bool
    rtt_ms: float | None


@dataclass(frozen=True)
class Thresholds:
    # Three failures at a five-minute interval is about fifteen minutes down
    # before anyone is told. Tighten the interval rather than this number if
    # that is too slow: one failed check is still not evidence.
    fail_streak: int = 3
    slow_streak: int = 2
    slow_factor: float = 2.0
    # Below this many good samples there is no baseline worth comparing to, so
    # no slow alert is raised at all. A device that has only just been added
    # should not be called slow on its third ever reading.
    baseline_min: int = 8
    baseline_window: int = 20


@dataclass(frozen=True)
class Evaluation:
    kinds: frozenset[str]
    baseline_ms: float | None = None
    latest_ms: float | None = None

    def __contains__(self, kind: object) -> bool:
        return kind in self.kinds


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def evaluate(recent: list[CheckLike], limits: Thresholds | None = None) -> Evaluation:
    """Decide which alerts should be open for a device right now.

    `recent` is that device's checks newest first, as store.recent() returns
    them. This is a pure function of the history: the same rows always give the
    same answer, which is what lets the tool run from cron and from a loop
    without keeping anything in memory between runs.
    """
    limits = limits or Thresholds()
    kinds: set[str] = set()

    # Not enough history to say anything yet. Staying quiet is the right
    # default: a tool that alerts on its own first run gets switched off.
    if len(recent) < limits.fail_streak:
        return Evaluation(frozenset())

    if all(not check.reachable for check in recent[: limits.fail_streak]):
        kinds.add(UNREACHABLE)

    latest, baseline = _latency_view(recent, limits)
    if UNREACHABLE not in kinds and _is_slow(recent, limits, baseline):
        kinds.add(SLOW)

    return Evaluation(frozenset(kinds), baseline, latest)


def _latency_view(recent: list[CheckLike], limits: Thresholds) -> tuple[float | None, float | None]:
    latest = recent[0].rtt_ms if recent and recent[0].reachable else None
    window = recent[limits.slow_streak : limits.slow_streak + limits.baseline_window]
    samples = [c.rtt_ms for c in window if c.reachable and c.rtt_ms is not None]
    if len(samples) < limits.baseline_min:
        return latest, None
    return latest, _mean(samples)


def _is_slow(recent: list[CheckLike], limits: Thresholds, baseline: float | None) -> bool:
    if baseline is None:
        return False
    # A baseline at or near zero makes the multiplier meaningless - everything
    # is infinitely more than nothing. This is the normal case on a LAN under
    # Windows, whose ping rounds to whole milliseconds, so it is handled rather
    # than left to produce nonsense.
    if baseline < 1.0:
        return False

    head = recent[: limits.slow_streak]
    if len(head) < limits.slow_streak:
        return False
    return all(
        check.reachable and check.rtt_ms is not None and check.rtt_ms > baseline * limits.slow_factor
        for check in head
    )


def reconcile(store, device: str, evaluation: Evaluation) -> list[str]:
    """Make the stored alerts agree with the evaluation, and say what changed.

    Opening and clearing are driven from the difference between what should be
    open and what is open, rather than from the transition that was just
    observed. That is what makes a missed run harmless: whatever the tool
    failed to notice last time is noticed on the next pass.
    """
    events: list[str] = []
    currently_open = {row["kind"] for row in store.open_alerts(device)}

    for kind in sorted(evaluation.kinds - currently_open):
        note = None
        if kind == SLOW and evaluation.baseline_ms and evaluation.latest_ms:
            note = "%.1f ms against a baseline of %.1f ms" % (
                evaluation.latest_ms, evaluation.baseline_ms,
            )
        if store.open_alert(device, kind, note):
            events.append("OPEN  %s %s%s" % (device, kind, " - " + note if note else ""))

    for kind in sorted(currently_open - evaluation.kinds):
        if store.clear_alert(device, kind):
            events.append("CLEAR %s %s" % (device, kind))

    return events
