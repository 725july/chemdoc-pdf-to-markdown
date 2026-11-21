#!/usr/bin/env python3
"""PaddleOCR-VL MCP bridge that restores page-boundary markers.

Why this exists
===============
``build-dual-mcp-book-skill`` requires a PaddleOCR MCP result that exposes a
*reliable* page boundary, either as ``page_idx`` or as a server-inserted marker
of the exact form ``<!-- PADDLE_MCP_PAGE:0001 -->`` (see
``references/mcp-contract.md`` and ``split_paddle_volume_markdown`` in
``scripts/mcp_batch.py``).

The official ``paddleocr-mcp`` package matches the skill's expectations for
server name (``PaddleOCR-VL-1.6``), tool name (``paddleocr_vl``) and argument
schema, but its local result adapter flattens every page with
``"\\n".join(markdown_parts)`` and therefore drops the page boundaries. The skill
then rejects the result with::

    paddle failed after 1 total attempts:
    Paddle multi-page result has no reliable leading page marker

This bridge runs the very same local ``PaddleOCRVL`` pipeline, keeps the
per-page Markdown that the pipeline already yields (one result object per page),
and emits one Markdown text block per page beginning with the required marker,
interleaving that page's images in place exactly like the official formatter.

The skill's own files are never touched, so a run's frozen governance digest
(``run-governance.json`` → ``skill_files``) stays valid.

Usage
=====
    python paddle_marker_bridge.py --model PaddleOCR-VL-1.6 [--device gpu:0]

Environment
===========
``USERPROFILE`` must point at a writable directory for the current user (the
pipeline creates ``~/.cache/paddle`` at import time), and the PaddleX model
cache must already contain the requested pipeline's models.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import re
import sys
from typing import Any, Dict, List, Optional, Union

from fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

# Marker layout consumed by the skill; keep the 4-digit zero padding.
PAGE_MARKER_TEMPLATE = "<!-- PADDLE_MCP_PAGE:{:04d} -->"

# Same mapping the official package uses (paddleocr_mcp/inference/paddleocr_vl/local.py).
PIPELINE_VERSION_BY_MODEL = {
    "PaddleOCR-VL": "v1",
    "PaddleOCR-VL-1.5": "v1.5",
    "PaddleOCR-VL-1.6": "v1.6",
}

_IMG_TAG_RE = re.compile(r'<img[^>]+src="([^"]+)"[^>]*>')

_pipeline: Any = None
_model_name: str = "PaddleOCR-VL-1.6"


# --------------------------------------------------------------------------- #
# image helpers
# --------------------------------------------------------------------------- #
def _encode_image_payload(value: Any) -> Optional[tuple[str, str]]:
    """Return ``(base64_data, mime_type)`` for a PIL image, bytes, or path.

    The local pipeline exposes ``markdown_images`` as ``{src: PIL.Image}``; the
    other shapes are accepted defensively so a pipeline change cannot silently
    drop figures.
    """

    raw: Optional[bytes] = None
    mime = "image/jpeg"

    try:
        if hasattr(value, "save"):  # PIL.Image.Image
            with io.BytesIO() as buffer:
                value.save(buffer, format="JPEG")
                raw = buffer.getvalue()
        elif isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
            mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        elif isinstance(value, str):
            text = value.strip()
            if text.startswith("data:") and "," in text:
                header, payload = text.split(",", 1)
                mime = header[5:].split(";")[0] or "image/jpeg"
                raw = base64.b64decode(payload)
            elif os.path.isfile(text):
                with open(text, "rb") as handle:
                    raw = handle.read()
                mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            else:
                raw = base64.b64decode(text, validate=True)
    except Exception:  # noqa: BLE001 - a bad image must not abort the whole volume
        return None

    if not raw:
        return None
    return base64.b64encode(raw).decode("ascii"), mime


# --------------------------------------------------------------------------- #
# per-page rendering
# --------------------------------------------------------------------------- #
def _page_markdown_parts(page_result: Any) -> tuple[str, Dict[str, Any]]:
    """Extract one page's Markdown text and image mapping from a pipeline result."""

    markdown = getattr(page_result, "markdown", None)
    if markdown is None and isinstance(page_result, dict):
        markdown = page_result.get("markdown")
    if isinstance(markdown, dict):
        text = markdown.get("markdown_texts") or markdown.get("text") or ""
        images = markdown.get("markdown_images") or {}
    else:  # tolerate a plain-string result
        text = str(markdown or "")
        images = {}
    return str(text), dict(images) if isinstance(images, dict) else {}


def _page_content_blocks(
    page_number: int,
    page_text: str,
    page_images: Dict[str, Any],
    return_images: bool,
) -> List[Union[TextContent, ImageContent]]:
    """Build ordered MCP content blocks for a single page.

    The first block always starts with the page marker, so the concatenation of
    all pages begins with a marker and the markers stay contiguous — exactly what
    the skill's splitter requires. Image blocks keep the position of their
    ``<img src="...">`` tag in the Markdown.
    """

    header = PAGE_MARKER_TEMPLATE.format(page_number)
    blocks: List[Union[TextContent, ImageContent]] = []

    if not (return_images and page_images):
        blocks.append(TextContent(type="text", text=f"{header}\n\n{page_text}".strip()))
        return blocks

    cursor = 0
    first_emitted = False
    for match in _IMG_TAG_RE.finditer(page_text):
        before = page_text[cursor : match.start()]
        payload = _encode_image_payload(page_images.get(match.group(1))) if match.group(1) in page_images else None

        if payload is not None:
            prefix = f"{header}\n\n{before}" if not first_emitted else before
            if prefix.strip():
                blocks.append(TextContent(type="text", text=prefix.strip()))
            image_data, mime_type = payload
            blocks.append(ImageContent(type="image", data=image_data, mimeType=mime_type))
            first_emitted = True
        else:
            # No decodable image: keep the original tag inline as text.
            before = page_text[cursor : match.end()]
            prefix = f"{header}\n\n{before}" if not first_emitted else before
            if prefix.strip():
                blocks.append(TextContent(type="text", text=prefix.strip()))
            first_emitted = True

        cursor = match.end()

    tail = page_text[cursor:]
    if tail.strip() or not first_emitted:
        prefix = f"{header}\n\n{tail}" if not first_emitted else tail
        if prefix.strip():
            blocks.append(TextContent(type="text", text=prefix.strip()))

    if not blocks:
        blocks.append(TextContent(type="text", text=header))
    return blocks


def _normalize_input(input_data: str) -> str:
    """Accept an absolute path or a URL; reject relative paths (official behaviour)."""

    value = (input_data or "").strip()
    if not value:
        raise ValueError("input_data must not be empty")
    if value.startswith(("http://", "https://", "data:")):
        return value
    if value.startswith("file://"):
        value = value[7:]
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        raise ValueError(
            "input_data must be an absolute file path (relative paths are rejected)"
        )
    if not os.path.isfile(expanded):
        raise ValueError(f"input_data is not a readable file: {expanded}")
    return expanded


# --------------------------------------------------------------------------- #
# MCP tool
# --------------------------------------------------------------------------- #
async def paddleocr_vl(
    input_data: str,
    output_mode: str = "simple",
    file_type: Optional[str] = None,
    return_images: bool = True,
    runtime_params: Optional[Dict[str, Any]] = None,
) -> Union[str, List[Union[TextContent, ImageContent]]]:
    """Run PaddleOCR-VL on an image or PDF and return page-marked Markdown.

    Args:
        input_data: Absolute path to the input PDF or image (HTTP(S) URLs are also accepted).
        output_mode: ``simple`` for plain text, ``detailed`` for JSON-style output.
        file_type: ``image`` or ``pdf``; informational for local inference.
        return_images: Whether to include figures as MCP image content blocks.
        runtime_params: Optional PaddleOCR-VL pipeline parameters as a JSON object.
    """

    if _pipeline is None:  # pragma: no cover - guarded by main()
        raise RuntimeError("PaddleOCR-VL pipeline is not initialised")

    normalized = _normalize_input(input_data)
    params: Dict[str, Any] = dict(runtime_params or {})

    results = _pipeline.predict(normalized, **params)
    page_results = list(results)

    blocks: List[Union[TextContent, ImageContent]] = []
    for page_number, page_result in enumerate(page_results, start=1):
        page_text, page_images = _page_markdown_parts(page_result)
        blocks.extend(
            _page_content_blocks(page_number, page_text, page_images, bool(return_images))
        )

    if not blocks:
        raise RuntimeError("PaddleOCR-VL returned no document content")

    if str(output_mode).lower() == "detailed":
        # The skill drops a terminal service-metadata block; flag it explicitly so
        # it can be recognised instead of leaking into the last page's text.
        total = TextContent(type="text", text=f"Pages: {len(page_results)}")
        total.meta = {"service_metadata": True}  # type: ignore[attr-defined]
        blocks.append(total)

    return blocks


def main() -> None:
    global _pipeline, _model_name

    parser = argparse.ArgumentParser(description="PaddleOCR-VL page-marker MCP bridge.")
    parser.add_argument("--model", default=os.getenv("PADDLE_MCP_MODEL", "PaddleOCR-VL-1.6"))
    parser.add_argument(
        "--ppocr_source",
        default=os.getenv("PADDLE_MCP_PPOCR_SOURCE", "local"),
        help="Only 'local' is supported by this bridge.",
    )
    parser.add_argument("--device", default=os.getenv("PADDLE_MCP_DEVICE"))
    parser.add_argument("--pipeline_config", default=os.getenv("PADDLE_MCP_PIPELINE_CONFIG"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.ppocr_source != "local":
        print(
            f"Error: this bridge only supports --ppocr_source local "
            f"(got {args.ppocr_source!r}).",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        pipeline_version = PIPELINE_VERSION_BY_MODEL[args.model]
    except KeyError:
        print(
            f"Error: unsupported model {args.model!r}. "
            f"Known models: {', '.join(sorted(PIPELINE_VERSION_BY_MODEL))}",
            file=sys.stderr,
        )
        sys.exit(2)

    from paddleocr import PaddleOCRVL  # imported late so --help stays fast

    kwargs: Dict[str, Any] = {"pipeline_version": pipeline_version}
    if args.pipeline_config:
        kwargs["paddlex_config"] = args.pipeline_config
    if args.device:
        kwargs["device"] = args.device

    print(f"[bridge] loading {args.model} (pipeline {pipeline_version}) ...", file=sys.stderr)
    _pipeline = PaddleOCRVL(**kwargs)
    _model_name = args.model
    print("[bridge] pipeline ready", file=sys.stderr)

    mcp = FastMCP(
        name=f"PaddleOCR {args.model} MCP server (page-marker bridge)",
        mask_error_details=True,
    )
    mcp.tool(
        "paddleocr_vl",
        description=(
            f"PaddleOCR paddleocr_vl tool (page-marker bridge, local inference). "
            f"Returns one Markdown text block per page, each beginning with a "
            f"'<!-- PADDLE_MCP_PAGE:NNNN -->' marker, with figures interleaved as "
            f"image blocks. Input: absolute local file path."
        ),
    )(paddleocr_vl)

    mcp.run(transport="stdio", log_level="INFO" if args.verbose else "WARNING")


if __name__ == "__main__":
    main()
