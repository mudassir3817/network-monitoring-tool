"""When a run of bad checks becomes an alert, and when it stops being one."""
from dataclasses import dataclass

import pytest

from netmon.alerts import SLOW, UNREACHABLE, Thresholds, evaluate, reconcile
from netmon.store import Store


@dataclass
class FakeCheck:
    reachable: bool
    rtt_ms: float | None


def ok(rtt=20.0):
    return FakeCheck(True, rtt)


def down():
    return FakeCheck(False, None)


LIMITS = Thresholds()  # fail_streak 3, slow_streak 2, factor 2.0, baseline_min 8


class TestUnreachable:
    def test_quiet_until_there_is_enough_history(self):
        # A tool that alerts on its own first run is a tool that gets switched
        # off before it is ever useful.
        assert UNREACHABLE not in evaluate([down(), down()], LIMITS)

    def test_two_failures_are_not_enough(self):
        recent = [down(), down()] + [ok()] * 10
        assert UNREACHABLE not in evaluate(recent, LIMITS)

    def test_three_in_a_row_opens_it(self):
        recent = [down(), down(), down()] + [ok()] * 10
        assert UNREACHABLE in evaluate(recent, LIMITS)

    def test_one_reply_clears_it(self):
        # The newest check is at the head, so this is a device that has just
        # come back after three failures.
        recent = [ok(), down(), down(), down()] + [ok()] * 10
        assert UNREACHABLE not in evaluate(recent, LIMITS)

    def test_a_gap_in_the_run_is_not_a_run(self):
        # fail, fail, ok - the device answered in between, so this is loss,
        # not an outage.
        recent = [down(), down(), ok(), down(), down()] + [ok()] * 10
        assert UNREACHABLE not in evaluate(recent, LIMITS)

    def test_the_threshold_is_configurable(self):
        recent = [down(), down()] + [ok()] * 10
        assert UNREACHABLE in evaluate(recent, Thresholds(fail_streak=2))


class TestSlow:
    def test_sustained_slowness_against_its_own_baseline(self):
        recent = [ok(50), ok(50)] + [ok(20)] * 10
        assert SLOW in evaluate(recent, LIMITS)

    def test_one_slow_reply_is_not_a_trend(self):
        recent = [ok(50), ok(20)] + [ok(20)] * 10
        assert SLOW not in evaluate(recent, LIMITS)

    def test_no_baseline_means_no_judgement(self):
        # Five samples is not a baseline. A device added ten minutes ago must
        # not be called slow on its third reading.
        recent = [ok(50), ok(50)] + [ok(20)] * 5
        evaluation = evaluate(recent, LIMITS)
        assert SLOW not in evaluation
        assert evaluation.baseline_ms is None

    def test_the_baseline_excludes_the_readings_being_judged(self):
        """Otherwise a slowdown that lasts quietly raises its own baseline.

        With a baseline of 20 ms from the ten older checks, two readings at
        50 ms are over the 2x line and should alert. Were the two 50 ms
        readings folded into the baseline it would rise to 33 ms, the line
        would move to 67 ms, and the alert would never open - the failure
        where monitoring decides everything is fine because everything has
        been bad for a while.
        """
        recent = [ok(50), ok(50)] + [ok(20)] * 8
        evaluation = evaluate(recent, LIMITS)
        assert evaluation.baseline_ms == pytest.approx(20.0)
        assert SLOW in evaluation

    def test_a_near_zero_baseline_does_not_make_everything_slow(self):
        # Windows ping rounds to whole milliseconds, so a LAN device reads 0.
        # Against a baseline of 0 every reading is infinitely slower, which
        # would mean a permanent alert on every local device.
        recent = [ok(1.0), ok(1.0)] + [ok(0.0)] * 10
        assert SLOW not in evaluate(recent, LIMITS)

    def test_unreachable_wins_over_slow(self):
        # A device that is down is not also slow, and saying both says nothing
        # extra to whoever is woken up.
        recent = [down(), down(), down()] + [ok(20)] * 10
        evaluation = evaluate(recent, LIMITS)
        assert UNREACHABLE in evaluation and SLOW not in evaluation

    def test_failed_checks_are_left_out_of_the_baseline(self):
        # A failed check has no round-trip time. Counting it as zero would
        # drag the baseline down and make the recovery look like a slowdown.
        recent = [ok(50), ok(50)] + [down()] * 3 + [ok(20)] * 10
        assert evaluate(recent, LIMITS).baseline_ms == pytest.approx(20.0)


class TestEvaluationIsPure:
    def test_the_same_history_always_gives_the_same_answer(self):
        # This is what lets the tool run from cron with no memory between runs
        # and from a loop, and behave identically.
        recent = [down(), down(), down()] + [ok()] * 10
        assert evaluate(recent, LIMITS).kinds == evaluate(recent, LIMITS).kinds

    def test_an_empty_history_says_nothing(self):
        assert evaluate([], LIMITS).kinds == frozenset()


class TestReconcile:
    @pytest.fixture()
    def store(self, tmp_path):
        with Store(str(tmp_path / "t.db")) as store:
            yield store

    def test_opens_once_and_does_not_repeat(self, store):
        recent = [down(), down(), down()] + [ok()] * 10
        evaluation = evaluate(recent, LIMITS)

        first = reconcile(store, "HQ-R1", evaluation)
        second = reconcile(store, "HQ-R1", evaluation)

        assert len(first) == 1 and "OPEN" in first[0]
        assert second == [], "an alert that is already open must not re-fire every pass"
        assert len(store.open_alerts("HQ-R1")) == 1

    def test_clears_when_the_device_comes_back(self, store):
        reconcile(store, "HQ-R1", evaluate([down()] * 3 + [ok()] * 10, LIMITS))
        events = reconcile(store, "HQ-R1", evaluate([ok()] * 13, LIMITS))

        assert len(events) == 1 and "CLEAR" in events[0]
        assert store.open_alerts("HQ-R1") == []

    def test_can_open_again_after_clearing(self, store):
        history = [down()] * 3 + [ok()] * 10
        reconcile(store, "HQ-R1", evaluate(history, LIMITS))
        reconcile(store, "HQ-R1", evaluate([ok()] * 13, LIMITS))
        events = reconcile(store, "HQ-R1", evaluate(history, LIMITS))

        assert len(events) == 1 and "OPEN" in events[0]

    def test_a_missed_pass_is_caught_up_rather_than_lost(self, store):
        """Reconciling against state, not against transitions.

        The device goes down and comes back while the tool is not running. The
        next pass sees only the current history and opens nothing, rather than
        replaying an edge it never saw.
        """
        events = reconcile(store, "HQ-R1", evaluate([ok()] * 13, LIMITS))
        assert events == [] and store.open_alerts("HQ-R1") == []

    def test_the_slow_note_carries_both_numbers(self, store):
        recent = [ok(50), ok(50)] + [ok(20)] * 10
        events = reconcile(store, "BR-R1", evaluate(recent, LIMITS))
        assert "50.0 ms" in events[0] and "20.0 ms" in events[0]

    def test_devices_do_not_share_alerts(self, store):
        reconcile(store, "HQ-R1", evaluate([down()] * 3 + [ok()] * 10, LIMITS))
        assert store.open_alerts("BR-R1") == []
