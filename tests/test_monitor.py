"""Config loading, and one pass with the network stubbed out."""
import json

import pytest

from netmon import monitor, snmp
from netmon.ping import PingResult
from netmon.store import Store


def write_config(tmp_path, payload):
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


class TestLoadConfig:
    def test_reads_devices_and_settings(self, tmp_path):
        path = write_config(tmp_path, {
            "interval_s": 60,
            "ping": {"count": 5, "timeout_s": 1.5},
            "devices": [{"name": "HQ-R1", "host": "192.168.10.1",
                         "snmp": {"community": "public", "if_index": 2}}],
        })
        config = monitor.load_config(path)

        assert config.interval_s == 60
        assert (config.ping_count, config.ping_timeout_s) == (5, 1.5)
        assert config.devices[0].community == "public"
        assert config.devices[0].if_index == 2

    def test_a_device_without_snmp_is_not_asked_for_it(self, tmp_path):
        path = write_config(tmp_path, {"devices": [{"host": "10.0.0.2"}]})
        assert monitor.load_config(path).devices[0].community is None

    def test_the_name_falls_back_to_the_address(self, tmp_path):
        path = write_config(tmp_path, {"devices": [{"host": "10.0.0.2"}]})
        assert monitor.load_config(path).devices[0].name == "10.0.0.2"

    def test_an_empty_device_list_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="no devices"):
            monitor.load_config(write_config(tmp_path, {"devices": []}))

    def test_a_misspelt_threshold_is_refused_rather_than_ignored(self, tmp_path):
        """The quietest way for monitoring to stop watching something.

        Ignoring the key would leave the default in place while whoever wrote
        the file believes their value took effect, and nothing would ever say
        otherwise.
        """
        path = write_config(tmp_path, {
            "devices": [{"host": "10.0.0.2"}],
            "thresholds": {"fail_streek": 5},
        })
        with pytest.raises(ValueError, match="fail_streek"):
            monitor.load_config(path)

    def test_a_device_with_no_host_is_refused(self, tmp_path):
        with pytest.raises(KeyError):
            monitor.load_config(write_config(tmp_path, {"devices": [{"name": "x"}]}))


class TestRunOnce:
    @pytest.fixture()
    def store(self, tmp_path):
        with Store(str(tmp_path / "t.db")) as store:
            yield store

    @pytest.fixture()
    def config(self):
        return monitor.Config(devices=[
            monitor.Device("HQ-R1", "192.168.10.1", community="public"),
            monitor.Device("BR-R1", "10.0.0.2"),
        ])

    def test_every_device_gets_a_row(self, store, config, monkeypatch):
        monkeypatch.setattr(monitor.ping, "ping",
                            lambda host, *a: PingResult(host, 3, 3, 12.0))
        monkeypatch.setattr(monitor.snmp, "interface_status", lambda *a, **k: "up")

        monitor.run_once(store, config)

        assert len(store.recent("HQ-R1")) == 1
        assert len(store.recent("BR-R1")) == 1

    def test_snmp_is_only_asked_of_devices_that_answered(self, store, config, monkeypatch):
        """Waiting on SNMP from a box already known to be down buys nothing.

        It costs the SNMP timeout per dead device per pass, which is what
        makes a pass overrun its own interval during an outage - exactly when
        the tool most needs to keep up.
        """
        asked = []
        monkeypatch.setattr(monitor.ping, "ping",
                            lambda host, *a: PingResult(host, 3, 0, None))
        monkeypatch.setattr(monitor.snmp, "interface_status",
                            lambda host, *a, **k: asked.append(host) or "up")

        monitor.run_once(store, config)

        assert asked == []
        assert store.recent("HQ-R1")[0].if_status is None

    def test_an_snmp_failure_is_recorded_but_does_not_stop_the_pass(
        self, store, config, monkeypatch
    ):
        # A device that pings but will not answer SNMP usually means the
        # community string or an ACL. Worth writing down, not worth an alert,
        # and certainly not worth abandoning the other devices over.
        monkeypatch.setattr(monitor.ping, "ping",
                            lambda host, *a: PingResult(host, 3, 3, 10.0))

        def refuse(*_a, **_k):
            raise snmp.SnmpError("no SNMP reply")

        monkeypatch.setattr(monitor.snmp, "interface_status", refuse)
        monitor.run_once(store, config)

        assert store.recent("HQ-R1")[0].reachable is True
        assert store.recent("HQ-R1")[0].if_status is None
        assert store.open_alerts() == []

    def test_a_run_of_failures_eventually_opens_an_alert(self, store, config, monkeypatch):
        monkeypatch.setattr(monitor.ping, "ping",
                            lambda host, *a: PingResult(host, 3, 0, None))

        events = []
        for _ in range(3):
            events += monitor.run_once(store, config)

        assert len(store.open_alerts("HQ-R1")) == 1
        assert sum("OPEN" in e for e in events) == 2, "one per device, once each"

    def test_recovery_clears_it(self, store, config, monkeypatch):
        monkeypatch.setattr(monitor.ping, "ping",
                            lambda host, *a: PingResult(host, 3, 0, None))
        for _ in range(3):
            monitor.run_once(store, config)

        monkeypatch.setattr(monitor.ping, "ping",
                            lambda host, *a: PingResult(host, 3, 3, 10.0))
        events = monitor.run_once(store, config)

        assert sum("CLEAR" in e for e in events) == 2
        assert store.open_alerts() == []
