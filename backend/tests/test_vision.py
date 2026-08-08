"""Image attachments: API-boundary validation + the pre-pass prompt embed.

The routing itself (vision lane stages files, blind lane gets the notes) is
covered in test_orchestrator_thread_manager.py.
"""

from __future__ import annotations

import base64

import pytest

from app.services import vision
from app.services.vision import ImageValidationError, notes_block, validate_images

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG-fake").decode()


def test_validate_accepts_good_data_uri():
    [(ext, raw)] = validate_images([PNG])
    assert ext == "png"
    assert raw == b"\x89PNG-fake"


def test_validate_rejects_non_data_uri():
    with pytest.raises(ImageValidationError, match="not a data URI"):
        validate_images(["https://evil.example/x.png"])


def test_validate_rejects_unsupported_mime():
    uri = "data:image/tiff;base64," + base64.b64encode(b"x").decode()
    with pytest.raises(ImageValidationError, match="not supported"):
        validate_images([uri])


def test_validate_rejects_bad_base64():
    with pytest.raises(ImageValidationError, match="valid base64"):
        validate_images(["data:image/png;base64,!!!not-b64!!!"])


def test_validate_rejects_empty_image():
    uri = "data:image/png;base64," + base64.b64encode(b"").decode()
    with pytest.raises(ImageValidationError, match="empty"):
        validate_images([uri])


def test_validate_enforces_count_cap():
    with pytest.raises(ImageValidationError, match="at most"):
        validate_images([PNG] * (vision.MAX_IMAGES + 1))


def test_validate_enforces_size_cap():
    big = "data:image/png;base64," + base64.b64encode(
        b"x" * (vision.MAX_IMAGE_BYTES + 1)).decode()
    with pytest.raises(ImageValidationError, match="over the"):
        validate_images([big])


def test_notes_block_fences_every_description():
    block = notes_block(["a red square", "a blue circle"])
    assert "<attached-images>" in block
    assert block.count("<image index=") == 2
    assert "a red square" in block and "a blue circle" in block
    # The embed must say the words a blind model needs to hear: these are
    # another model's descriptions, not direct perception.
    assert "cannot see images" in block


def test_describe_prompt_conditioned_on_user_task():
    """"see the image, here is the bug" must reach the vision model — an
    unconditional description would miss the stack trace the user means."""
    plain = vision._describe_prompt(None)
    assert "user-request" not in plain
    conditioned = vision._describe_prompt("see the image, here is the bug")
    assert "<user-request>\nsee the image, here is the bug\n</user-request>" in conditioned
    assert "CANNOT see it" in conditioned
    assert vision._describe_prompt("   ") == plain  # blank task = unconditional


async def test_prepare_images_skips_prepass_when_all_lanes_vision(
        monkeypatch, tmp_path):
    """An all-Kimi selection never pays for the pre-pass: vision lanes read
    the images natively, so there is no blind lane to describe for."""
    from app.core.config import get_settings
    from app.orchestrator.run_manager import RunManager

    settings = get_settings()
    settings.sessions_dir = tmp_path
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    calls: list = []
    async def fake_describe(images, task=None):
        calls.append((images, task))
        return ["desc"]
    monkeypatch.setattr(vision, "describe_images", fake_describe)

    rm = RunManager(None, None, None, None)
    out = await rm._prepare_images("r1", [PNG], "task", ["kimi-k2.6", "kimi-k3"])
    assert calls == []                       # no pre-pass spend
    assert "image_notes" not in out
    assert len(out["image_paths"]) == 1      # files still staged for native vision
    assert (tmp_path / "r1" / "_attachments" / "image-1.png").exists()


async def test_prepare_images_prepass_for_blind_lane_with_task(
        monkeypatch, tmp_path):
    """A blind lane in the selection triggers the pre-pass, conditioned on
    the user's message."""
    from app.core.config import get_settings
    from app.orchestrator.run_manager import RunManager

    settings = get_settings()
    settings.sessions_dir = tmp_path
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    calls: list = []
    async def fake_describe(images, task=None):
        calls.append(task)
        return ["a red square with an error dialog"]
    monkeypatch.setattr(vision, "describe_images", fake_describe)

    rm = RunManager(None, None, None, None)
    out = await rm._prepare_images("r1", [PNG], "here is the bug",
                                   ["kimi-k2.6", "deepseek-v4-pro"])
    assert calls == ["here is the bug"]      # task reached the vision model
    assert "a red square with an error dialog" in out["image_notes"]
