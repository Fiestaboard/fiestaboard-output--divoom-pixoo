"""The manifest, the vendored device data, and the npm package agree."""

from __future__ import annotations

import hashlib
import json

from .conftest import _ERRORS, MANIFEST, PLUGIN_DIR

#: output/device-models.json is FiestaUI's divoom_pixoo64 model, byte for byte
#: (README "Provenance"). Changing it is a deliberate re-vendor, not an edit.
DEVICE_MODELS_SHA256 = "e7cbfff32e91afd3d7b3ce39fa27cd7335cba76a326698221fc14100d8607d6f"


def test_the_manifest_loads_as_an_output_plugin():
    assert _ERRORS == []
    assert MANIFEST.id == "divoom_pixoo"
    assert MANIFEST.plugin_type == "output"


def test_the_capabilities_come_from_the_pixoo_model():
    caps = MANIFEST.output.capabilities
    assert caps.technology == "led_matrix"
    assert caps.delivery == "push"
    assert caps.animation == "sequence"
    assert caps.max_frames == 32
    assert caps.min_interval_ms == 1000
    assert caps.read_back.supported is False
    assert caps.native_transitions == frozenset()
    assert caps.device_models == ("divoom_pixoo64",)


def test_no_board_setting_is_secret():
    props = MANIFEST.output.settings_schema["properties"]
    assert set(props) == {"host", "brightness"}
    assert not any(p.get("secret") or p.get("ui:widget") == "password" for p in props.values())


def test_the_device_data_is_byte_identical_to_the_fiestaui_model():
    digest = hashlib.sha256((PLUGIN_DIR / "output" / "device-models.json").read_bytes()).hexdigest()
    assert digest == DEVICE_MODELS_SHA256


def test_the_address_field_is_a_device_picker_bound_to_find_my_pixoo():
    host = MANIFEST.output.settings_schema["properties"]["host"]
    assert host["ui:widget"] == "device-picker"
    assert host["ui:options"] == {"action": "find_pixoo", "value_key": "host", "label_key": "label"}


def test_the_actions_are_find_my_pixoo_cloud_lookup_and_test():
    actions = {a.id: a for a in MANIFEST.output.actions}
    assert list(actions) == ["find_pixoo", "cloud_lookup", "test_connection"]
    assert actions["find_pixoo"].label == "Find my Pixoo"
    assert set(actions["find_pixoo"].input_schema["properties"]) == {"subnet", "hint_host"}
    assert not actions["find_pixoo"].input_schema.get("required")
    assert actions["cloud_lookup"].label == "Ask Divoom's servers which Pixoos are on your network"
    assert actions["cloud_lookup"].input_schema is None


def test_package_json_carries_the_manifest_version():
    package = json.loads((PLUGIN_DIR / "package.json").read_text())
    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text())
    assert package["version"] == manifest["version"]
