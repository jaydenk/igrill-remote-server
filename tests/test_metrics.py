"""Tests for Prometheus metrics."""

from service.metrics import MetricsRegistry


def test_counter_increment():
    m = MetricsRegistry()
    m.inc("igrill_ble_reads_total")
    assert m.get("igrill_ble_reads_total") == 1
    m.inc("igrill_ble_reads_total")
    assert m.get("igrill_ble_reads_total") == 2


def test_gauge_set():
    m = MetricsRegistry()
    m.set("igrill_devices_connected", 3)
    assert m.get("igrill_devices_connected") == 3


def test_labelled_counter():
    m = MetricsRegistry()
    m.inc("igrill_ws_messages_sent_total", labels={"type": "reading"})
    m.inc("igrill_ws_messages_sent_total", labels={"type": "reading"})
    m.inc("igrill_ws_messages_sent_total", labels={"type": "status"})
    output = m.render()
    assert 'igrill_ws_messages_sent_total{type="reading"} 2' in output
    assert 'igrill_ws_messages_sent_total{type="status"} 1' in output


def test_render_prometheus_format():
    m = MetricsRegistry()
    m.set("igrill_devices_connected", 2)
    m.inc("igrill_ble_reads_total")
    output = m.render()
    assert "igrill_devices_connected 2" in output
    assert "igrill_ble_reads_total 1" in output


def test_gauge_overwrite():
    m = MetricsRegistry()
    m.set("igrill_devices_connected", 3)
    m.set("igrill_devices_connected", 1)
    assert m.get("igrill_devices_connected") == 1


def test_get_nonexistent_returns_zero():
    m = MetricsRegistry()
    assert m.get("nonexistent") == 0
