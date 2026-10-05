"""Finding a Pixoo: the LAN sweep ("Find my Pixoo") and the opt-in cloud lookup.

Every sweep here runs against mock Pixoos on 127.0.0.1 (the network fence
refuses anything else). Addresses in the subnet tests are documentation
examples; nothing is contacted for them.
"""

from __future__ import annotations

import ipaddress
import json
import time

import pytest
import requests
from plugins.divoom_pixoo import DivoomPixoo, candidate_subnets, is_pixoo_reply, sweep

from src.plugins import OutputHttp

from .conftest import build
from .mock_pixoo import ALL_CONF, MockPixoo

ALLOW = "FIESTABOARD_OUTPUTS_ALLOW_HOSTS"


@pytest.fixture(autouse=True)
def no_local_address(monkeypatch):
    """FiestaBoard's own address reads as unknown unless a test sets one."""
    import plugins.divoom_pixoo as pixoo

    monkeypatch.setattr(pixoo, "local_ipv4", lambda: "127.0.0.1")


def test_hostnames_resolve_without_the_network_for_localhost_and_fail_soft():
    from plugins.divoom_pixoo import _resolve_ipv4

    assert _resolve_ipv4("localhost") == "127.0.0.1"
    assert _resolve_ipv4("a..b") is None


@pytest.fixture
def devices():
    started: list[MockPixoo] = []

    def make(mode: str = "ok") -> MockPixoo:
        device = MockPixoo().start()
        device.mode = mode
        started.append(device)
        return device

    yield make
    for device in started:
        device.stop()


def target(device: MockPixoo) -> tuple[str, int]:
    host, port = device.host.split(":")
    return host, int(port)


# --- what counts as a Pixoo -------------------------------------------------------------------


def test_the_pixoo_config_reply_is_recognised():
    assert is_pixoo_reply(ALL_CONF)


@pytest.mark.parametrize(
    "body",
    [{"error_code": 0}, {"error_code": 1, "Brightness": 50}, [ALL_CONF], "Brightness", None],
)
def test_other_replies_are_not_a_pixoo(body):
    assert not is_pixoo_reply(body)


# --- which subnets ----------------------------------------------------------------------------


def nets(*cidrs: str) -> list[ipaddress.IPv4Network]:
    return [ipaddress.IPv4Network(c) for c in cidrs]


def test_a_docker_bridge_address_is_not_searched_when_the_browser_named_the_lan():
    # FiestaBoard in Docker bridge mode sees 172.17.x; the browser's address is the LAN.
    assert candidate_subnets(hint_host="192.168.1.20", local_ip="172.17.0.2") == nets("192.168.1.0/24")


def test_a_docker_bridge_address_is_not_searched_when_the_user_typed_a_network():
    assert candidate_subnets(subnet="192.168.4.0/24", local_ip="172.18.0.5") == nets("192.168.4.0/24")


def test_a_docker_bridge_address_is_searched_when_it_is_all_there_is():
    assert candidate_subnets(local_ip="172.17.0.2") == nets("172.17.0.0/24")


def test_the_browser_hint_comes_before_fiestaboards_own_lan_address():
    assert candidate_subnets(hint_host="192.168.1.20", local_ip="10.0.0.7") == nets("192.168.1.0/24", "10.0.0.0/24")


def test_an_explicit_subnet_comes_first_and_duplicates_are_dropped():
    assert candidate_subnets(
        subnet="10.0.5.0/24", hint_host="10.0.5.9", local_ip="192.168.1.20"
    ) == nets("10.0.5.0/24", "192.168.1.0/24")


@pytest.mark.parametrize("hint", ["8.8.8.8", "127.0.0.1", "169.254.1.1", "localhost", "", "not an address", "::1"])
def test_public_loopback_link_local_and_unresolvable_hints_are_ignored(hint, monkeypatch):
    import plugins.divoom_pixoo as pixoo

    monkeypatch.setattr(pixoo, "_resolve_ipv4", lambda name: "127.0.0.1" if name == "localhost" else None)
    assert candidate_subnets(hint_host=hint, local_ip="192.168.1.20") == nets("192.168.1.0/24")


def test_an_unknown_local_address_adds_nothing():
    assert candidate_subnets(local_ip="127.0.0.1") == []


def test_a_hostname_hint_is_resolved(monkeypatch):
    import plugins.divoom_pixoo as pixoo

    monkeypatch.setattr(pixoo, "_resolve_ipv4", lambda name: "192.168.7.3" if name == "fiestaboard.local" else None)
    assert candidate_subnets(hint_host="fiestaboard.local") == nets("192.168.7.0/24")


@pytest.mark.parametrize("subnet", ["10.0.0.0/8", "8.8.8.0/24", "192.168.1.0/33", "nonsense"])
def test_a_subnet_that_is_too_big_public_or_malformed_is_refused(subnet):
    with pytest.raises(ValueError):
        candidate_subnets(subnet=subnet)


# --- the sweep ----------------------------------------------------------------------------------


def test_the_sweep_finds_the_pixoo_and_ignores_everything_else(devices):
    pixoo = devices("ok")
    others = [devices("other_json"), devices("garbage"), devices("http_404"), devices("hang")]
    found = sweep(OutputHttp(), [target(d) for d in [*others, pixoo]], per_host_s=0.3, total_s=3.0)
    host, port = target(pixoo)
    assert found == [{"ip": host, "port": port, "host": f"{host}:{port}", "label": "Pixoo 64 at 127.0.0.1", "hostname": "Pixoo 64"}]
    assert pixoo.commands == [{"Command": "Channel/GetAllConf"}]


def test_a_device_on_port_80_is_named_by_its_address_alone():
    from plugins.divoom_pixoo import _device

    assert _device("192.168.1.50", 80)["host"] == "192.168.1.50"


def test_the_sweep_stops_at_its_total_budget(devices):
    hang = devices("hang")
    started = time.monotonic()
    found = sweep(OutputHttp(), [target(hang)] * 40, per_host_s=5.0, total_s=0.5, concurrency=4)
    assert found == []
    assert time.monotonic() - started < 1.5


def test_the_sweep_honours_the_host_fence(devices, monkeypatch):
    pixoo = devices("ok")
    monkeypatch.setenv(ALLOW, "fiestaboard-mock-pixoo")
    assert sweep(OutputHttp(), [target(pixoo)], per_host_s=0.3, total_s=2.0) == []
    assert pixoo.commands == []


# --- "Find my Pixoo" (the action) ---------------------------------------------------------------


def finder(port: int) -> DivoomPixoo:
    plugin = build({})
    plugin.DISCOVERY_PORT = port
    plugin.DISCOVERY_PER_HOST_S = 0.3
    return plugin


def test_find_my_pixoo_sweeps_a_given_subnet_and_reports_the_device(devices):
    pixoo = devices("ok")
    host, port = target(pixoo)
    outcome = finder(port).action_find_pixoo({"subnet": "127.0.0.0/30"})
    assert outcome.status == "ok"
    assert [d["ip"] for d in outcome.devices] == [host]
    assert "1" in outcome.message


def test_find_my_pixoo_sweeps_the_hint_subnet_before_the_local_one(monkeypatch):
    import plugins.divoom_pixoo as pixoo

    swept: list[list[tuple[str, int]]] = []
    monkeypatch.setattr(pixoo, "local_ipv4", lambda: "10.0.0.7")
    monkeypatch.setattr(pixoo, "sweep", lambda http, targets, **kw: swept.append(targets) or [])
    outcome = build({}).action_find_pixoo({"hint_host": "192.168.1.20"})
    targets = swept[0]
    assert targets[0] == ("192.168.1.1", 80)
    assert targets[253] == ("192.168.1.254", 80)
    assert targets[254] == ("10.0.0.1", 80)
    assert len(targets) == 2 * 254
    assert outcome.status == "warning"
    assert outcome.devices == ()
    assert any("Device Settings" in step or "address" in step for step in outcome.guidance)


def test_find_my_pixoo_explains_a_refused_subnet():
    outcome = build({}).action_find_pixoo({"subnet": "10.0.0.0/8"})
    assert outcome.status == "error"
    assert "/22" in outcome.message


def test_find_my_pixoo_with_nothing_to_sweep_says_so(monkeypatch):
    import plugins.divoom_pixoo as pixoo

    monkeypatch.setattr(pixoo, "local_ipv4", lambda: "127.0.0.1")
    outcome = build({}).action_find_pixoo({})
    assert outcome.status == "warning"
    assert outcome.devices == ()


def test_find_my_pixoo_mentions_the_host_fence_when_it_is_set(monkeypatch):
    import plugins.divoom_pixoo as pixoo

    monkeypatch.setenv(ALLOW, "fiestaboard-mock-pixoo")
    monkeypatch.setattr(pixoo, "sweep", lambda http, targets, **kw: [])
    outcome = build({}).action_find_pixoo({"subnet": "192.168.1.0/30"})
    assert any(ALLOW in step for step in outcome.guidance)


def test_the_core_discover_hook_sweeps_the_local_subnet_with_its_own_helper(monkeypatch):
    import plugins.divoom_pixoo as pixoo

    seen = {}

    def fake_sweep(http, targets, **kw):
        seen.update(http=http, targets=targets, **kw)
        return [{"ip": "192.168.1.50", "port": 80}]

    monkeypatch.setattr(pixoo, "local_ipv4", lambda: "192.168.1.20")
    monkeypatch.setattr(pixoo, "sweep", fake_sweep)
    assert DivoomPixoo.discover(4.0) == [{"ip": "192.168.1.50", "port": 80}]
    assert isinstance(seen["http"], OutputHttp)
    assert seen["total_s"] == 4.0
    assert len(seen["targets"]) == 254


def test_the_core_discover_hook_takes_a_hint(monkeypatch):
    import plugins.divoom_pixoo as pixoo

    seen = {}
    monkeypatch.setattr(pixoo, "local_ipv4", lambda: "127.0.0.1")
    monkeypatch.setattr(pixoo, "sweep", lambda http, targets, **kw: seen.update(targets=targets) or [])
    DivoomPixoo.discover(30.0, hint="192.168.1.20")
    assert seen["targets"][0] == ("192.168.1.1", 80)


# --- the opt-in cloud lookup ----------------------------------------------------------------------


def cloud(plugin: DivoomPixoo, status: int, body) -> list:
    seen = []

    def transport(request):
        seen.append(request)
        response = requests.Response()
        response.status_code = status
        response._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return response

    plugin.http.use_transport(transport)
    return seen


#: The lab-verified reply shape (values here are made up).
def lan_reply(*devices: dict) -> dict:
    return {"ReturnCode": 0, "ReturnMessage": "", "DeviceList": list(devices)}


def device(ip: str, mac: str, name: str = "Pixoo64") -> dict:
    return {"DeviceName": name, "DeviceId": 300000001, "DevicePrivateIP": ip, "DeviceMac": mac, "Hardware": 92}


def test_the_cloud_lookup_posts_an_empty_body_and_lists_address_name_and_mac():
    plugin = build({})
    seen = cloud(
        plugin,
        200,
        lan_reply(device("192.168.1.50", "AABBCCDDEEFF"), device("192.168.1.51", "a1b2c3d4e5f6", "Kitchen"), {"DeviceName": "No address", "DeviceId": 2}),
    )
    outcome = plugin.action_cloud_lookup({})
    assert [(r.method, r.url, r.json) for r in seen] == [("POST", "https://app.divoom-gz.com/Device/ReturnSameLANDevice", {})]
    assert outcome.status == "ok"
    assert outcome.devices == (
        {"ip": "192.168.1.50", "port": 80, "host": "192.168.1.50", "label": "Pixoo64 (192.168.1.50)", "hostname": "Pixoo64", "mac": "aabbccddeeff"},
        {"ip": "192.168.1.51", "port": 80, "host": "192.168.1.51", "label": "Kitchen (192.168.1.51)", "hostname": "Kitchen", "mac": "a1b2c3d4e5f6"},
    )
    # Two devices: the user picks; nothing is filled in on their behalf.
    assert outcome.fields == {}


def test_a_single_device_fills_in_its_address_and_mac():
    plugin = build({})
    cloud(plugin, 200, lan_reply(device("192.168.1.50", "AA:BB:CC:DD:EE:FF")))
    outcome = plugin.action_cloud_lookup({})
    assert {k: v.value for k, v in outcome.fields.items()} == {"host": "192.168.1.50", "mac": "aabbccddeeff"}
    assert not any(v.secret for v in outcome.fields.values())


def test_with_a_saved_mac_the_lookup_re_finds_the_device_at_its_new_address():
    plugin = build({"host": "192.168.1.50", "mac": "aabbccddeeff"})
    cloud(plugin, 200, lan_reply(device("192.168.1.51", "a1b2c3d4e5f6"), device("192.168.1.77", "AABBCCDDEEFF")))
    outcome = plugin.action_cloud_lookup({})
    assert outcome.status == "ok"
    assert {k: v.value for k, v in outcome.fields.items()} == {"host": "192.168.1.77", "mac": "aabbccddeeff"}
    assert [d["ip"] for d in outcome.devices] == ["192.168.1.77"]
    assert "192.168.1.77" in outcome.message


def test_a_saved_mac_that_divoom_does_not_list_is_reported_with_the_others():
    plugin = build({"mac": "aabbccddeeff"})
    cloud(plugin, 200, lan_reply(device("192.168.1.51", "a1b2c3d4e5f6")))
    outcome = plugin.action_cloud_lookup({})
    assert outcome.status == "warning"
    assert outcome.fields == {}
    assert [d["ip"] for d in outcome.devices] == ["192.168.1.51"]


@pytest.mark.parametrize(
    ("status", "body"),
    [(200, {"ReturnCode": 1, "ReturnMessage": "error"}), (500, {}), (200, b"<html>"), (200, {"ReturnCode": 0})],
)
def test_a_cloud_lookup_that_fails_or_finds_nothing_is_reported(status, body):
    plugin = build({})
    cloud(plugin, status, body)
    outcome = plugin.action_cloud_lookup({})
    assert outcome.status in ("warning", "error")
    assert not outcome.devices


def test_the_cloud_lookup_is_fenced_like_any_request(monkeypatch):
    monkeypatch.setenv(ALLOW, "fiestaboard-mock-pixoo")
    outcome = build({}).action_cloud_lookup({})
    assert outcome.status == "error"
    assert any(ALLOW in step for step in outcome.guidance)


def test_the_cloud_lookup_reports_an_unreachable_service():
    plugin = build({})

    def transport(request):
        raise requests.exceptions.ConnectionError("no route")

    plugin.http.use_transport(transport)
    outcome = plugin.action_cloud_lookup({})
    assert outcome.status == "error"
