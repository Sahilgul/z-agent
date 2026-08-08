"""_with_images: multimodal first message for vision lanes, fail-safe text
for blind ones. The backend sets IMAGES_DIR only for vision-capable lanes —
these tests pin the worker's half of that contract."""

from __future__ import annotations

import base64

import pytest


def _runner(monkeypatch: pytest.MonkeyPatch, model: str = "kimi-k2.6",
            images_dir: str | None = None):
    monkeypatch.setenv("RUN_ID", "r1")
    monkeypatch.setenv("THREAD_ID", "t1")
    monkeypatch.setenv("TASK_PROMPT", "do the thing")
    monkeypatch.setenv("REDIS_URL", "redis://localhost")
    monkeypatch.setenv("MODEL", model)
    if images_dir is None:
        monkeypatch.delenv("IMAGES_DIR", raising=False)
    else:
        monkeypatch.setenv("IMAGES_DIR", images_dir)
    from worker.engine.runner import EngineRunner
    return EngineRunner()


def test_no_images_dir_returns_plain_text(monkeypatch):
    r = _runner(monkeypatch)
    assert r._with_images("hello") == "hello"


def test_vision_model_gets_multimodal_blocks(monkeypatch, tmp_path):
    (tmp_path / "image-1.png").write_bytes(b"\x89PNG-fake")
    (tmp_path / "image-2.jpg").write_bytes(b"\xff\xd8fake")
    r = _runner(monkeypatch, model="kimi-k2.6", images_dir=str(tmp_path))
    blocks = r._with_images("describe these")
    assert isinstance(blocks, list)
    assert blocks[0] == {"type": "text", "text": "describe these"}
    imgs = [b for b in blocks[1:]]
    assert len(imgs) == 2
    assert imgs[0]["type"] == "image_url"
    # Sorted by filename: image-1.png before image-2.jpg.
    assert imgs[0]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(b"\x89PNG-fake").decode())
    assert imgs[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_blind_model_fails_safe_to_text(monkeypatch, tmp_path):
    """Backend contract says IMAGES_DIR is never set for a blind lane; if it
    somehow is, the worker must NOT send image blocks to a model that would
    400 or hallucinate — the pre-pass description is already in the prompt."""
    (tmp_path / "image-1.png").write_bytes(b"\x89PNG-fake")
    r = _runner(monkeypatch, model="deepseek-v4-pro", images_dir=str(tmp_path))
    assert r._with_images("hello") == "hello"


def test_unreadable_dir_falls_back_to_text(monkeypatch):
    r = _runner(monkeypatch, model="kimi-k3", images_dir="/nonexistent/dir")
    assert r._with_images("hello") == "hello"


def test_non_image_files_are_skipped(monkeypatch, tmp_path):
    (tmp_path / "notes.txt").write_text("not an image")
    r = _runner(monkeypatch, model="kimi-k2.6", images_dir=str(tmp_path))
    assert r._with_images("hello") == "hello"
