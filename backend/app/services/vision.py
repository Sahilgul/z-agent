"""Image attachments: validation at the API boundary + the vision pre-pass.

The fleet's vision reality (probed live through the gateway 2026-08-08):
only the Kimi deployments truly read images — GLM 400s on image input and
both DeepSeeks return 200 while hallucinating ("no image visible"). So:

- Vision lanes (kimi-k2.6 / kimi-k3) get the images natively, staged into
  the thread's session volume (thread_manager.spawn).
- Blind lanes get this service's textual description embedded in their
  prompt — the user attaches once, every lane "sees" the image the best
  it can.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re

import httpx

from app.core.config import get_settings

# 10 per message: matches the composer cap. Each image costs one pre-pass
# call per blind lane selection, all parallel — the cap bounds that fan-out.
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # decoded, per image
_ALLOWED_MIME = {"image/png": "png", "image/jpeg": "jpg",
                 "image/webp": "webp", "image/gif": "gif"}
_DATA_URI = re.compile(r"^data:(image/[a-z0-9+.-]+);base64,(.*)$", re.DOTALL)

# The pre-pass model: cheapest TRUE-vision deployment in the fleet. Deliberately
# NOT the registry default — the pre-pass is infrastructure, not a user choice.
PREPASS_MODEL = "kimi-k2.6"

_DESCRIBE_PROMPT = (
    "Describe this image in precise, complete detail for another AI model that "
    "CANNOT see it and must reason about it from your words alone. Cover: what "
    "it depicts, all visible text (verbatim), UI elements/code/diagrams and "
    "their structure, colors and layout where relevant, and anything anomalous. "
    "Be exhaustive but factual — do not speculate beyond what is visible."
)


def _describe_prompt(task: str | None) -> str:
    """The pre-pass prompt, conditioned on the user's own words when present.

    "see the image, here is the bug" only yields a useful description if the
    vision model knows the user is reporting a BUG — an unconditional
    description would lavish tokens on layout and miss the stack trace."""
    if not task or not task.strip():
        return _DESCRIBE_PROMPT
    return (
        f"The user attached this image together with this request:\n\n"
        f"<user-request>\n{task.strip()}\n</user-request>\n\n"
        "Describe the image for another AI model that CANNOT see it and must "
        "fulfil that request from your words alone. Focus on whatever the "
        "request points at (e.g. for a bug report: the exact error text, "
        "stack traces, highlighted code, UI state), then cover the rest "
        "briefly. Transcribe relevant text verbatim. Be factual — do not "
        "speculate beyond what is visible."
    )


class ImageValidationError(ValueError):
    """Raised for malformed/oversized attachments; the API maps it to 422."""


def validate_images(images: list[str]) -> list[tuple[str, bytes]]:
    """Parse data URIs -> (extension, decoded bytes), enforcing count/size/type.

    Fail-closed on anything malformed: a silently dropped attachment would
    let the agent answer about an image it never received.
    """
    if len(images) > MAX_IMAGES:
        raise ImageValidationError(f"at most {MAX_IMAGES} images per run")
    out: list[tuple[str, bytes]] = []
    for i, uri in enumerate(images):
        m = _DATA_URI.match(uri or "")
        if not m:
            raise ImageValidationError(
                f"image {i + 1} is not a data URI (data:image/...;base64,...)")
        mime = m.group(1).lower()
        if mime not in _ALLOWED_MIME:
            raise ImageValidationError(
                f"image {i + 1} type '{mime}' not supported "
                f"(allowed: {', '.join(sorted(_ALLOWED_MIME))})")
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageValidationError(f"image {i + 1} is not valid base64") from exc
        if len(raw) > MAX_IMAGE_BYTES:
            raise ImageValidationError(
                f"image {i + 1} is {len(raw)} bytes decoded, over the "
                f"{MAX_IMAGE_BYTES}-byte limit")
        if not raw:
            raise ImageValidationError(f"image {i + 1} is empty")
        out.append((_ALLOWED_MIME[mime], raw))
    return out


async def describe_images(images: list[str], task: str | None = None) -> list[str]:
    """The vision pre-pass: one kimi-k2.6 call per image, in parallel.

    ``task`` is the user's own message — the description is conditioned on
    it so the vision model extracts what the request actually needs.

    Rides the gateway with the master key (threads don't exist yet at
    create_run time, so no virtual key can scope it); spend lands in the
    gateway log attributed to the prepass model.
    """
    settings = get_settings()
    prompt = _describe_prompt(task)

    async def _one(client: httpx.AsyncClient, uri: str) -> str:
        resp = await client.post(
            "/chat/completions",
            json={
                "model": PREPASS_MODEL,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": uri}},
                ]}],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async with httpx.AsyncClient(
        base_url=settings.gateway_url,
        headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
        timeout=120,
    ) as client:
        results = await asyncio.gather(*(_one(client, uri) for uri in images))
    return list(results)


def notes_block(descriptions: list[str]) -> str:
    """The prompt embed for blind lanes: descriptions, clearly fenced as
    another model's perception so the lane can weigh confidence honestly."""
    parts = "\n\n".join(
        f"<image index={i + 1}>\n{d}\n</image>" for i, d in enumerate(descriptions))
    return (
        "\n\n<attached-images>\nThe user attached "
        f"{len(descriptions)} image(s). This model cannot see images directly; "
        "below is a detailed description of each, produced by a vision-capable "
        "model (Kimi K2.6). Reason from these descriptions as if you had seen "
        "the images; if a detail is ambiguous or missing, say so rather than "
        f"inventing it.\n\n{parts}\n</attached-images>"
    )
