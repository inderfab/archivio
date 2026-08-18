"""Tests für die mDNS-Discovery in shared/menubar_bridge.py (advertise_service,
stop_advertising, discover_servers). zeroconf wird komplett gemockt -- keine echten
mDNS-Pakete im Testlauf."""
import logging
import sys
from pathlib import Path

import zeroconf as zeroconf_module

sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))
import menubar_bridge as bridge  # noqa: E402

log = logging.getLogger("test")


class _FakeServiceInfo:
    def __init__(self, type_, name, addresses=None, port=None, server=None):
        self.type_ = type_
        self.name = name
        self.addresses = addresses
        self.port = port
        self.server = server


class _FakeZeroconf:
    instances = []

    def __init__(self):
        self.registered = []
        self.unregistered = []
        self.closed = False
        _FakeZeroconf.instances.append(self)

    def register_service(self, info):
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed = True


# ── advertise_service / stop_advertising ──────────────────────────────────────

def test_advertise_service_registers_and_returns_handles(monkeypatch):
    _FakeZeroconf.instances.clear()
    monkeypatch.setattr(zeroconf_module, "Zeroconf", _FakeZeroconf)
    monkeypatch.setattr(zeroconf_module, "ServiceInfo", _FakeServiceInfo)
    monkeypatch.setattr(bridge, "_local_ip", lambda: "192.168.1.50")

    zc, info = bridge.advertise_service(8000, log)

    assert zc is not None and info is not None
    assert info.port == 8000
    assert zc.registered == [info]


def test_advertise_service_returns_none_on_error(monkeypatch):
    def boom():
        raise RuntimeError("zeroconf init failed")
    monkeypatch.setattr(zeroconf_module, "Zeroconf", boom)

    zc, info = bridge.advertise_service(8000, log)
    assert zc is None and info is None


def test_stop_advertising_unregisters_and_closes():
    zc = _FakeZeroconf()
    info = _FakeServiceInfo("_archivio._tcp.local.", "x")
    zc.register_service(info)

    bridge.stop_advertising(zc, info, log)

    assert zc.unregistered == [info]
    assert zc.closed is True


def test_stop_advertising_noop_when_none():
    bridge.stop_advertising(None, None, log)  # darf nicht crashen


# ── discover_servers ──────────────────────────────────────────────────────────

def _make_browser(services: dict):
    """services: {name: (ip_str, port)} oder {name: (ip_str, port, hostname)} --
    simuliert Faende synchron im Konstruktor, wie es ein echter ServiceBrowser
    asynchron ueber die Zeit hinweg taete."""
    import socket as socket_module

    class _FakeInfo:
        def __init__(self, addr, port, server=None):
            self.addresses = [socket_module.inet_aton(addr)]
            self.port = port
            self.server = server

    class _FakeZeroconfWithInfo(_FakeZeroconf):
        def get_service_info(self, type_, name):
            entry = services[name]
            if len(entry) == 3:
                addr, port, server = entry
            else:
                addr, port = entry
                server = None
            return _FakeInfo(addr, port, server)

    class _FakeServiceBrowser:
        def __init__(self, zc, service_type, listener):
            for name in services:
                listener.add_service(zc, service_type, name)

    return _FakeZeroconfWithInfo, _FakeServiceBrowser


def test_discover_servers_findet_keinen(monkeypatch):
    FakeZC, FakeBrowser = _make_browser({})
    monkeypatch.setattr(zeroconf_module, "Zeroconf", FakeZC)
    monkeypatch.setattr(zeroconf_module, "ServiceBrowser", FakeBrowser)

    found = bridge.discover_servers(timeout=0.01, log=log)
    assert found == []


def test_discover_servers_findet_einen(monkeypatch):
    FakeZC, FakeBrowser = _make_browser({"srv1": ("192.168.1.10", 8000)})
    monkeypatch.setattr(zeroconf_module, "Zeroconf", FakeZC)
    monkeypatch.setattr(zeroconf_module, "ServiceBrowser", FakeBrowser)

    found = bridge.discover_servers(timeout=0.01, log=log)
    assert found == [("192.168.1.10", 8000)]


def test_discover_servers_findet_mehrere(monkeypatch):
    FakeZC, FakeBrowser = _make_browser({
        "srv1": ("192.168.1.10", 8000),
        "srv2": ("192.168.1.20", 8000),
    })
    monkeypatch.setattr(zeroconf_module, "Zeroconf", FakeZC)
    monkeypatch.setattr(zeroconf_module, "ServiceBrowser", FakeBrowser)

    found = bridge.discover_servers(timeout=0.01, log=log)
    assert sorted(found) == [("192.168.1.10", 8000), ("192.168.1.20", 8000)]


def test_discover_servers_returns_empty_on_error(monkeypatch):
    def boom():
        raise RuntimeError("zeroconf init failed")
    monkeypatch.setattr(zeroconf_module, "Zeroconf", boom)

    found = bridge.discover_servers(timeout=0.01, log=log)
    assert found == []


# ── discover_other_servers ──────────────────────────────────────────────────────

def test_discover_other_servers_filtert_sich_selbst(monkeypatch):
    """Der eigene, gerade erst angemeldete Dienst (gleiche IP + Port) darf nicht als
    'anderer Server' zurückkommen -- sonst würde jeder Server sich selbst melden."""
    FakeZC, FakeBrowser = _make_browser({
        "self": ("192.168.1.50", 8000, "MeinMac.local."),
    })
    monkeypatch.setattr(zeroconf_module, "Zeroconf", FakeZC)
    monkeypatch.setattr(zeroconf_module, "ServiceBrowser", FakeBrowser)
    monkeypatch.setattr(bridge, "_local_ip", lambda: "192.168.1.50")

    found = bridge.discover_other_servers(own_port=8000, timeout=0.01, log=log)
    assert found == []


def test_discover_other_servers_findet_anderen_mit_hostname(monkeypatch):
    FakeZC, FakeBrowser = _make_browser({
        "other": ("192.168.1.20", 8000, "Buero-iMac.local."),
    })
    monkeypatch.setattr(zeroconf_module, "Zeroconf", FakeZC)
    monkeypatch.setattr(zeroconf_module, "ServiceBrowser", FakeBrowser)
    monkeypatch.setattr(bridge, "_local_ip", lambda: "192.168.1.50")

    found = bridge.discover_other_servers(own_port=8000, timeout=0.01, log=log)
    assert found == [("192.168.1.20", 8000, "Buero-iMac")]


def test_discover_other_servers_faellt_auf_ip_zurueck_ohne_hostname(monkeypatch):
    FakeZC, FakeBrowser = _make_browser({
        "other": ("192.168.1.20", 8000),  # kein hostname (server=None)
    })
    monkeypatch.setattr(zeroconf_module, "Zeroconf", FakeZC)
    monkeypatch.setattr(zeroconf_module, "ServiceBrowser", FakeBrowser)
    monkeypatch.setattr(bridge, "_local_ip", lambda: "192.168.1.50")

    found = bridge.discover_other_servers(own_port=8000, timeout=0.01, log=log)
    assert found == [("192.168.1.20", 8000, "192.168.1.20")]


def test_discover_other_servers_unterscheidet_gleiche_ip_anderer_port(monkeypatch):
    """Gleiche IP aber anderer Port (z.B. zwei Server-Instanzen auf demselben Mac,
    theoretisch) zaehlt NICHT als der eigene Dienst."""
    FakeZC, FakeBrowser = _make_browser({
        "other": ("192.168.1.50", 9000, "MeinMac.local."),
    })
    monkeypatch.setattr(zeroconf_module, "Zeroconf", FakeZC)
    monkeypatch.setattr(zeroconf_module, "ServiceBrowser", FakeBrowser)
    monkeypatch.setattr(bridge, "_local_ip", lambda: "192.168.1.50")

    found = bridge.discover_other_servers(own_port=8000, timeout=0.01, log=log)
    assert found == [("192.168.1.50", 9000, "MeinMac")]


def test_discover_other_servers_returns_empty_on_error(monkeypatch):
    def boom():
        raise RuntimeError("zeroconf init failed")
    monkeypatch.setattr(zeroconf_module, "Zeroconf", boom)

    found = bridge.discover_other_servers(own_port=8000, timeout=0.01, log=log)
    assert found == []


# ── resolve_discovery (Entscheidungslogik, von Helper und Server gemeinsam genutzt) ──

def test_resolve_discovery_kein_treffer():
    assert bridge.resolve_discovery([]) == ("none", None)


def test_resolve_discovery_ein_treffer():
    decision, url = bridge.resolve_discovery([("192.168.1.10", 8000)])
    assert decision == "one"
    assert url == "http://192.168.1.10:8000"


def test_resolve_discovery_mehrere_treffer():
    decision, url = bridge.resolve_discovery(
        [("192.168.1.10", 8000), ("192.168.1.20", 8000)]
    )
    assert decision == "multiple"
    assert url is None
