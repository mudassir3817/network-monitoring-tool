"""The SQLite layer: what goes in, what comes back, and the uptime arithmetic."""
from datetime import datetime, timedelta, timezone

import pytest

from netmon.store import Store, _stamp


@pytest.fixture()
def store(tmp_path):
    with Store(str(tmp_path / "t.db")) as store:
        yield store


class TestRecordingAndReading:
    def test_recent_comes_back_newest_first(self, store):
        for i in range(5):
            store.record("HQ-R1", True, float(i), 3, 3)
        rtts = [c.rtt_ms for c in store.recent("HQ-R1", 5)]
        assert rtts == [4.0, 3.0, 2.0, 1.0, 0.0]

    def test_recent_respects_the_limit(self, store):
        for _ in range(10):
            store.record("HQ-R1", True, 1.0, 3, 3)
        assert len(store.recent("HQ-R1", 4)) == 4

    def test_devices_are_kept_apart(self, store):
        store.record("HQ-R1", True, 1.0, 3, 3)
        store.record("BR-R1", False, None, 3, 0)
        assert len(store.recent("HQ-R1")) == 1
        assert store.recent("BR-R1")[0].reachable is False

    def test_a_missing_device_reads_as_empty_not_as_an_error(self, store):
        assert store.recent("never-seen") == []

    def test_round_trip_of_every_field(self, store):
        store.record("HQ-R1", True, 12.5, 4, 3, if_status="up", note="n")
        check = store.recent("HQ-R1")[0]
        assert (check.device, check.reachable, check.rtt_ms) == ("HQ-R1", True, 12.5)
        assert (check.sent, check.received, check.if_status) == (4, 3, "up")

    def test_a_down_device_stores_a_null_rtt_not_a_zero(self, store):
        # Zero would be read back as "answered instantly" by anything
        # averaging the column, including the uptime report.
        store.record("BR-R1", False, None, 3, 0)
        assert store.recent("BR-R1")[0].rtt_ms is None

    def test_devices_lists_what_has_been_seen(self, store):
        store.record("b", True, 1.0, 1, 1)
        store.record("a", True, 1.0, 1, 1)
        assert store.devices() == ["a", "b"]


class TestUptime:
    def test_counts_checks_not_packets(self, store):
        for _ in range(8):
            store.record("HQ-R1", True, 10.0, 3, 3)
        for _ in range(2):
            store.record("HQ-R1", False, None, 3, 0)

        row = store.uptime(hours=None)[0]
        assert row.checks == 10
        assert row.reachable_checks == 8
        assert row.uptime_pct == pytest.approx(80.0)

    def test_packet_loss_is_separate_from_uptime(self, store):
        # Four checks that all answered, but half the packets went missing.
        # The device was up the whole time and the link is still unhealthy;
        # one number cannot say both.
        for _ in range(4):
            store.record("BR-R1", True, 20.0, 4, 2)

        row = store.uptime(hours=None)[0]
        assert row.uptime_pct == pytest.approx(100.0)
        assert row.loss_pct == pytest.approx(50.0)

    def test_average_rtt_ignores_the_checks_that_failed(self, store):
        store.record("HQ-R1", True, 10.0, 3, 3)
        store.record("HQ-R1", True, 20.0, 3, 3)
        store.record("HQ-R1", False, None, 3, 0)
        assert store.uptime(hours=None)[0].avg_rtt_ms == pytest.approx(15.0)

    def test_a_window_leaves_out_older_rows(self, store):
        store.record("HQ-R1", True, 10.0, 3, 3)
        # A row from two days ago, written straight in rather than waiting two
        # days for one to age. Comparing against "now" at the boundary would
        # make the test depend on the clock ticking between two statements,
        # which on Windows it sometimes does not.
        old = _stamp(datetime.now(timezone.utc) - timedelta(days=2))
        store.db.execute(
            "INSERT INTO checks (device, checked_at, reachable, rtt_ms, sent, received)"
            " VALUES (?,?,1,10.0,3,3)",
            ("HQ-R1", old),
        )
        store.db.commit()

        assert store.uptime(hours=24)[0].checks == 1, "the older row is outside the day"
        assert store.uptime(hours=None)[0].checks == 2, "but inside all of history"

    def test_timestamps_are_a_fixed_width(self):
        """Text order has to be time order, because that is how rows are sorted.

        isoformat() drops the microseconds when they are exactly zero, which
        happens about one row in a million. "+" sorts before ".", so that row
        would sort ahead of rows written before it and fall outside windows it
        belongs in - a bug that appears rarely and never reproduces.
        """
        on_the_microsecond = datetime(2026, 11, 15, 13, 31, 2, 0, tzinfo=timezone.utc)
        just_after = datetime(2026, 11, 15, 13, 31, 2, 500000, tzinfo=timezone.utc)

        assert _stamp(on_the_microsecond) == "2026-11-15T13:31:02.000000+00:00"
        assert len(_stamp(on_the_microsecond)) == len(_stamp(just_after))
        assert _stamp(on_the_microsecond) < _stamp(just_after)

    def test_no_history_reads_as_empty(self, store):
        assert store.uptime() == []

    def test_percentages_do_not_divide_by_zero(self, store):
        store.record("HQ-R1", False, None, 0, 0)
        row = store.uptime(hours=None)[0]
        assert row.loss_pct == 0.0 and row.uptime_pct == 0.0


class TestAlerts:
    def test_opening_twice_only_opens_once(self, store):
        assert store.open_alert("HQ-R1", "unreachable") is True
        assert store.open_alert("HQ-R1", "unreachable") is False
        assert len(store.open_alerts("HQ-R1")) == 1

    def test_the_two_kinds_are_independent(self, store):
        store.open_alert("HQ-R1", "unreachable")
        assert store.open_alert("HQ-R1", "slow") is True
        assert len(store.open_alerts("HQ-R1")) == 2

    def test_clearing_something_that_is_not_open_is_not_an_error(self, store):
        assert store.clear_alert("HQ-R1", "unreachable") is False

    def test_an_alert_can_be_opened_again_after_it_clears(self, store):
        # The uniqueness rule only covers alerts that are still open, so a
        # device that goes down twice gets two rows in the history rather
        # than one row that cannot be reopened.
        store.open_alert("HQ-R1", "unreachable")
        store.clear_alert("HQ-R1", "unreachable")
        assert store.open_alert("HQ-R1", "unreachable") is True
        assert len(store.alert_history()) == 2

    def test_clearing_does_not_delete_the_history(self, store):
        store.open_alert("HQ-R1", "unreachable", note="three in a row")
        store.clear_alert("HQ-R1", "unreachable")
        row = store.alert_history()[0]
        assert row["cleared_at"] is not None and row["note"] == "three in a row"

    def test_open_alerts_without_a_device_returns_all_of_them(self, store):
        store.open_alert("HQ-R1", "unreachable")
        store.open_alert("BR-R1", "slow")
        assert len(store.open_alerts()) == 2


class TestPersistence:
    def test_the_database_survives_being_closed_and_reopened(self, tmp_path):
        """The only state the tool has is this file.

        If it did not survive a restart, `--once` from cron would start from
        nothing every five minutes and no failure streak could ever reach its
        threshold.
        """
        path = str(tmp_path / "t.db")
        with Store(path) as store:
            store.record("HQ-R1", False, None, 3, 0)
            store.open_alert("HQ-R1", "unreachable")

        with Store(path) as reopened:
            assert len(reopened.recent("HQ-R1")) == 1
            assert len(reopened.open_alerts("HQ-R1")) == 1

    def test_opening_an_existing_database_does_not_wipe_it(self, tmp_path):
        path = str(tmp_path / "t.db")
        with Store(path) as store:
            store.record("HQ-R1", True, 1.0, 3, 3)
        with Store(path) as reopened:
            reopened.record("HQ-R1", True, 2.0, 3, 3)
            assert len(reopened.recent("HQ-R1")) == 2
