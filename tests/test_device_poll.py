"""poll_device: tell core when the Pixoo restarted, was overwritten by another
client, or moved to a new address.

A reboot wipes FiestaBoard's image back to the Pixoo's own clock face. Core
skips a page the board already shows, so without this an unchanged page was
never sent again. The reboot also resets the device's picture-id counter,
which is how the plugin tells. Another client's upload that displayed
pushes the counter past ours instead: reported as overwritten. A Pixoo that came back on a new DHCP address
is found again by its saved MAC through Divoom's cloud lookup.
"""

from __future__ import annotations

import dataclasses
import json
import socket
from collections.abc import Mapping
from typing import Any

import pytest
import requests
from plugins import divoom_pixoo
from plugins.divoom_pixoo import DIVOOM_LAN_LOOKUP_URL, GET_GIF_ID, RESET_GIF_ID

from src.outputs.plugin_base import CancelToken
from src.plugins import DevicePoll

from .conftest import build

MAC = "aabbccddeeff"


def frame(code: int) -> list[list[int]]:
    return [[code] * 16 for _ in range(10)]


def show(plugin, code: int = 1):
    return plugin.write(frame(code), native=None, cancel=CancelToken())


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- restarts -------------------------------------------------------------------------------


def test_a_reboot_that_reset_the_picture_ids_is_a_restart(pixoo, make_plugin):
    plugin = make_plugin()
    assert show(plugin).success
    pixoo.power_cycle()  # the device forgot every id

    assert plugin.poll_device() == DevicePoll(restarted=True)


def test_after_a_restart_the_next_write_starts_a_fresh_session(pixoo, make_plugin):
    plugin = make_plugin()
    show(plugin)
    pixoo.power_cycle()
    plugin.poll_device()
    pixoo.commands.clear()

    assert show(plugin, 2).success
    assert pixoo.names()[:2] == [RESET_GIF_ID, GET_GIF_ID]
    assert pixoo.displayed[-1]["PicID"] == 1


def test_a_device_still_showing_our_frame_has_no_news(pixoo, make_plugin):
    plugin = make_plugin()
    show(plugin)
    show(plugin, 2)

    assert plugin.poll_device() is None
    assert pixoo.names()[-1] == GET_GIF_ID


def test_before_any_upload_there_is_nothing_to_compare(pixoo, make_plugin):
    """A fresh instance (core just started) sends the page anyway."""
    plugin = make_plugin()

    assert plugin.poll_device() is None


def test_a_device_that_answers_again_after_a_failed_upload_is_a_restart(pixoo, make_plugin):
    """Writes that failed while it was booting never reached it, whatever
    picture ids it reports now."""
    plugin = make_plugin()
    show(plugin)
    pixoo.mode = "http_500"
    assert not show(plugin, 2).success
    pixoo.mode = "ok"

    assert plugin.poll_device() == DevicePoll(restarted=True)
    assert plugin.poll_device() is None


def test_a_landed_upload_clears_the_failure(pixoo, make_plugin):
    plugin = make_plugin()
    pixoo.mode = "http_500"
    show(plugin)
    pixoo.mode = "ok"
    assert show(plugin, 2).success

    assert plugin.poll_device() is None


def test_a_restart_re_applies_the_configured_brightness(pixoo, make_plugin):
    plugin = make_plugin(brightness=40)
    show(plugin)
    pixoo.power_cycle()
    plugin.poll_device()
    pixoo.commands.clear()

    show(plugin, 2)

    assert {"Command": "Channel/SetBrightness", "Brightness": 40} in pixoo.commands


def test_a_reply_that_is_not_a_pixoo_reports_nothing(pixoo, make_plugin):
    plugin = make_plugin()
    show(plugin)
    pixoo.mode = "garbage"

    assert plugin.poll_device() is None


def test_a_fenced_host_reports_nothing(pixoo, make_plugin, monkeypatch):
    plugin = make_plugin(mac=MAC)
    show(plugin)
    monkeypatch.setenv("FIESTABOARD_OUTPUTS_ALLOW_HOSTS", "fiestaboard-mock-pixoo")

    assert plugin.poll_device() is None


def test_no_address_reports_nothing():
    assert build({}).poll_device() is None


def test_a_core_without_the_hook_type_gets_nothing(pixoo, make_plugin, monkeypatch):
    plugin = make_plugin()
    show(plugin)
    pixoo.power_cycle()
    monkeypatch.setattr(divoom_pixoo, "DevicePoll", None)

    assert plugin.poll_device() is None


# --- overwritten by another client -----------------------------------------------------------
#
# The ``overwritten`` field is new in core (FiestaBoard#2354). These stand-ins
# pin both shapes, whichever one the core under test carries.


@dataclasses.dataclass(frozen=True)
class NewCorePoll:
    restarted: bool = False
    config: Mapping[str, Any] | None = None
    overwritten: bool = False


@dataclasses.dataclass(frozen=True)
class OldCorePoll:
    restarted: bool = False
    config: Mapping[str, Any] | None = None


@pytest.fixture
def new_core(monkeypatch):
    monkeypatch.setattr(divoom_pixoo, "DevicePoll", NewCorePoll)


@pytest.fixture
def old_core(monkeypatch):
    monkeypatch.setattr(divoom_pixoo, "DevicePoll", OldCorePoll)


def test_another_clients_upload_is_reported_as_overwritten(pixoo, make_plugin, new_core):
    plugin = make_plugin()
    assert show(plugin).success
    assert pixoo.foreign_upload()  # it displayed: the device's next id passed ours

    assert plugin.poll_device() == NewCorePoll(overwritten=True)


def test_after_an_overwrite_the_next_upload_displays_over_the_foreign_image(pixoo, make_plugin, new_core):
    """The foreign upload raised the device's last id past ours: re-sending
    with our old counter would be silently ignored."""
    plugin = make_plugin()
    show(plugin)
    assert pixoo.foreign_upload(pic_id=50)
    plugin.poll_device()
    shown = len(pixoo.displayed)

    assert show(plugin, 2).success
    assert len(pixoo.displayed) == shown + 1
    assert pixoo.ignored == []


def test_an_overwrite_is_reported_once(pixoo, make_plugin, new_core):
    plugin = make_plugin()
    show(plugin)
    pixoo.foreign_upload()
    plugin.poll_device()

    assert plugin.poll_device() is None


def test_an_unchanged_counter_is_no_news_on_a_core_that_knows_overwrites(pixoo, make_plugin, new_core):
    """Equal ids: no foreign upload displayed, or none the Pixoo can show us."""
    plugin = make_plugin()
    show(plugin)
    show(plugin, 2)

    assert plugin.poll_device() is None


def test_a_lower_counter_is_still_a_restart_on_a_core_that_knows_overwrites(pixoo, make_plugin, new_core):
    plugin = make_plugin()
    show(plugin)
    pixoo.power_cycle()

    assert plugin.poll_device() == NewCorePoll(restarted=True)


def test_a_failed_upload_is_still_a_restart_on_a_core_that_knows_overwrites(pixoo, make_plugin, new_core):
    plugin = make_plugin()
    show(plugin)
    pixoo.mode = "http_500"
    show(plugin, 2)
    pixoo.mode = "ok"

    assert plugin.poll_device() == NewCorePoll(restarted=True)


def test_an_older_core_hears_an_overwrite_as_a_restart(pixoo, make_plugin, old_core):
    """A core without ``overwritten`` still re-sends after a restart."""
    plugin = make_plugin()
    show(plugin)
    pixoo.foreign_upload()

    assert plugin.poll_device() == OldCorePoll(restarted=True)


def test_an_older_core_also_gets_the_next_upload_displayed(pixoo, make_plugin, old_core):
    plugin = make_plugin()
    show(plugin)
    pixoo.foreign_upload(pic_id=50)
    plugin.poll_device()

    assert show(plugin, 2).success
    assert pixoo.ignored == []


def test_a_core_without_the_hook_type_hears_nothing_of_an_overwrite(pixoo, make_plugin, monkeypatch):
    plugin = make_plugin()
    show(plugin)
    pixoo.foreign_upload()
    monkeypatch.setattr(divoom_pixoo, "DevicePoll", None)

    assert plugin.poll_device() is None


def test_the_core_under_test_gets_an_overwrite_in_the_shape_it_knows(pixoo, make_plugin):
    plugin = make_plugin()
    show(plugin)
    pixoo.foreign_upload()

    knows = "overwritten" in {f.name for f in dataclasses.fields(DevicePoll)}
    expected = DevicePoll(overwritten=True) if knows else DevicePoll(restarted=True)
    assert plugin.poll_device() == expected


# --- moved to a new address -----------------------------------------------------------------


def lan_reply(*devices: tuple[str, str]) -> dict:
    return {
        "ReturnCode": 0,
        "ReturnMessage": "",
        "DeviceList": [
            {"DeviceName": "Pixoo64", "DeviceId": 1, "DevicePrivateIP": ip, "DeviceMac": mac, "Hardware": 92}
            for ip, mac in devices
        ],
    }


class Network:
    """A fake LAN: the old address is gone, Divoom's cloud lists *listed*,
    and the Pixoo answers at *alive*."""

    def __init__(self, listed: list[tuple[str, str]], alive: set[str]) -> None:
        self.listed = listed
        self.alive = alive
        self.seen: list[str] = []

    def __call__(self, request):
        self.seen.append(request.url)
        if request.url == DIVOOM_LAN_LOOKUP_URL:
            return self._reply(lan_reply(*self.listed))
        host = request.url.split("/")[2]
        if host not in self.alive:
            raise requests.exceptions.ConnectTimeout(f"{host} is not answering")
        return self._reply({"error_code": 0, "PicId": 0})

    @staticmethod
    def _reply(body: dict) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        return response

    def cloud_calls(self) -> int:
        return self.seen.count(DIVOOM_LAN_LOOKUP_URL)


def moved(listed, alive, **config):
    plugin = build({"host": "192.0.2.24", "mac": MAC, **config})
    network = Network(listed, alive)
    plugin.http.use_transport(network)
    return plugin, network


def test_a_pixoo_that_moved_is_found_by_its_mac_and_its_new_address_reported():
    plugin, network = moved([("192.0.2.50", "a1b2c3d4e5f6"), ("192.0.2.23", MAC.upper())], {"192.0.2.23"})

    assert plugin.poll_device() == DevicePoll(restarted=True, config={"host": "192.0.2.23"})
    assert plugin.host == "192.0.2.23"
    assert plugin.config["host"] == "192.0.2.23"
    assert plugin.device_key() == "192.0.2.23"
    assert network.seen[-1] == "http://192.0.2.23/post"


def test_without_a_saved_mac_the_cloud_is_never_asked():
    plugin, network = moved([("192.0.2.23", MAC)], {"192.0.2.23"}, mac=None)

    assert plugin.poll_device() is None
    assert network.cloud_calls() == 0
    assert plugin.host == "192.0.2.24"


def test_the_cloud_is_asked_at_most_once_per_interval():
    plugin, network = moved([], set())

    assert plugin.poll_device() is None
    assert plugin.poll_device() is None
    assert network.cloud_calls() == 1

    plugin._next_refind_at = 0.0  # the interval passed
    plugin.poll_device()
    assert network.cloud_calls() == 2


def test_a_mac_the_cloud_does_not_list_leaves_the_address_alone():
    plugin, _ = moved([("192.0.2.50", "a1b2c3d4e5f6")], {"192.0.2.50"})

    assert plugin.poll_device() is None
    assert plugin.host == "192.0.2.24"


def test_the_cloud_listing_the_same_address_is_no_move():
    """The device is just off: nothing to save, nothing to re-send, and no
    second request to the address that just failed to answer."""
    plugin, network = moved([("192.0.2.24", MAC)], set())

    assert plugin.poll_device() is None
    assert plugin.host == "192.0.2.24"
    assert network.seen == ["http://192.0.2.24/post", DIVOOM_LAN_LOOKUP_URL]


def test_a_listed_address_that_does_not_answer_is_not_adopted():
    plugin, _ = moved([("192.0.2.23", MAC)], set())

    assert plugin.poll_device() is None
    assert plugin.host == "192.0.2.24"
    assert "host" not in plugin.config or plugin.config["host"] == "192.0.2.24"


def test_an_unreachable_cloud_reports_nothing():
    plugin = build({"host": f"127.0.0.1:{closed_port()}", "mac": MAC})

    def transport(request):
        raise requests.exceptions.ConnectionError("no route")

    plugin.http.use_transport(transport)

    assert plugin.poll_device() is None


@pytest.mark.parametrize("reply", [{"ReturnCode": 1}, {"ReturnCode": 0}])
def test_a_cloud_reply_without_devices_reports_nothing(reply):
    plugin = build({"host": "192.0.2.24", "mac": MAC})

    def transport(request):
        if request.url == DIVOOM_LAN_LOOKUP_URL:
            return Network._reply(reply)
        raise requests.exceptions.ConnectTimeout("gone")

    plugin.http.use_transport(transport)

    assert plugin.poll_device() is None
