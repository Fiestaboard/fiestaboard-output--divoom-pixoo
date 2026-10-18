"""Mounting correction belongs at upload, never in logical rendering.

Missing rotation, reversed direction, double rotation or rotating only the last
transition frame must change the decoded bytes these tests observe.
"""

import base64

import pytest
from jsonschema import Draft7Validator
from src.canvas.layer import CanvasLayer
from src.led.transitions import LedTransitionSpec
from src.outputs.cells import RichCells
from src.plugins import CancelToken, ResolvedLedTransition, TimedFrame

from .conftest import MANIFEST
from .test_canvas_layers import rows

# Source corners TL, TR, BL, BR, plus an off-axis interior pixel.
MARKS = [
    (0, 0, (11, 22, 33)),
    (63, 0, (44, 55, 66)),
    (0, 63, (77, 88, 99)),
    (63, 63, (111, 122, 133)),
    (7, 13, (144, 155, 166)),
]
# Hand-derived clockwise destinations, in the same color order.
DESTINATIONS = {
    0: [(0, 0), (63, 0), (0, 63), (63, 63), (7, 13)],
    90: [(63, 0), (63, 63), (0, 0), (0, 63), (50, 7)],
    180: [(63, 63), (0, 63), (63, 0), (0, 0), (56, 50)],
    270: [(0, 63), (0, 0), (63, 63), (63, 0), (13, 56)],
}


def fixture_frame():
    pixels = bytearray(64 * 64 * 4)
    for i in range(3, len(pixels), 4):
        pixels[i] = 255
    for x, y, color in MARKS:
        i = (y * 64 + x) * 4
        pixels[i : i + 4] = bytes((*color, 255))
    return RichCells(rows(""), (CanvasLayer(0, 0, 64, 64, bytes(pixels)),))


def expected_marks(angle):
    image = bytearray(64 * 64 * 3)
    for (x, y), (_, _, color) in zip(DESTINATIONS[angle], MARKS):
        i = (y * 64 + x) * 3
        image[i : i + 3] = bytes(color)
    return bytes(image)


def pushed(pixoo):
    pushes = pixoo.pushes()
    assert all(
        (p["PicWidth"], p["PicNum"], p["PicOffset"]) == (64, 1, 0) for p in pushes
    )
    return [base64.b64decode(p["PicData"]) for p in pushes]


@pytest.mark.parametrize("angle", [None, 0, 90, 180, 270])
def test_canvas_upload_has_hand_checked_clockwise_coordinates(
    pixoo, make_plugin, angle
):
    plugin = make_plugin(**({} if angle is None else {"orientation": angle}))
    frame = fixture_frame()
    assert plugin.render(frame) == expected_marks(0)
    result = plugin.write_cells(frame, native=None, cancel=CancelToken())
    assert result.success and result.was_sent
    assert pushed(pixoo) == [expected_marks(angle or 0)]
    assert plugin.render(frame) == expected_marks(0)
    assert (plugin.layout(frame).grid.rows, plugin.layout(frame).grid.cols) == (10, 16)


def rotate_reference(pixels, angle):
    # Independent matrix transpose, checked against the literal fixture above.
    matrix = [
        [pixels[(y * 64 + x) * 3 : (y * 64 + x + 1) * 3] for x in range(64)]
        for y in range(64)
    ]
    for _ in range(angle // 90):
        matrix = list(zip(*matrix[::-1]))
    return b"".join(pixel for row in matrix for pixel in row)


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize(
    "mode",
    [
        "still",
        "rich",
        "sequence",
        "none",
        "black",
        "flip",
        "slide",
        "wipe",
        "cascade",
        "dissolve",
        "blend",
    ],
)
def test_every_uploaded_frame_rotates_once(pixoo, make_plugin, angle, mode):
    assert rotate_reference(expected_marks(0), angle) == expected_marks(angle)
    before = rows("{red:OLD}")
    after = fixture_frame()

    def send(plugin):
        cancel = CancelToken()
        if mode == "still":
            return plugin.write([[1, 2, 3], [4, 5]], native=None, cancel=cancel)
        if mode == "rich":
            return plugin.write_cells(before, native=None, cancel=cancel)
        if mode == "sequence":
            return plugin.write_sequence(
                [TimedFrame(before, 0), TimedFrame(after, 0)], cancel=cancel
            )
        kind = "fade" if mode in ("black", "blend") else mode
        spec = "none" if kind == "none" else LedTransitionSpec(kind, duration_ms=800)
        return plugin.write_transition(
            before, after, ResolvedLedTransition(kind, spec, "explicit"), cancel=cancel
        )

    baseline = make_plugin(fade_style="blend" if mode == "blend" else "black")
    assert send(baseline).success
    logical = pushed(pixoo)
    pixoo.clear()
    rotated = make_plugin(
        orientation=angle, fade_style="blend" if mode == "blend" else "black"
    )
    result = send(rotated)
    assert result.success and result.was_sent
    actual = pushed(pixoo)
    assert actual == [rotate_reference(frame, angle) for frame in logical]
    if mode in ("flip", "slide", "wipe", "cascade", "dissolve", "blend"):
        assert len(set(logical)) > 1


@pytest.mark.parametrize("angle", [-90, 45, 360, "90", True, None, 90.5])
def test_invalid_orientation_is_rejected_by_board_settings(angle):
    validator = Draft7Validator(MANIFEST.output.settings_schema)
    assert list(validator.iter_errors({"host": "127.0.0.1", "orientation": angle}))


def test_orientation_settings_accept_supported_angles_and_default_to_zero():
    schema = MANIFEST.output.settings_schema
    assert schema["properties"]["orientation"]["default"] == 0
    validator = Draft7Validator(schema)
    for config in [{"host": "127.0.0.1"}] + [
        {"host": "127.0.0.1", "orientation": angle} for angle in DESTINATIONS
    ]:
        assert not list(validator.iter_errors(config))
