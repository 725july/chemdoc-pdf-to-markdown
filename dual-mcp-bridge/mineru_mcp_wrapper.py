#!/usr/bin/env python3
"""MinerU cloud MCP wrapper exposing ``parse_documents_vlm_forced_ocr``.

Why this exists
===============
``build-dual-mcp-book-skill`` requires an MCP server named ``mineru`` exposing a
single tool, ``parse_documents_vlm_forced_ocr``, that:

* accepts ``file_sources`` (absolute volume PDF paths), ``language`` (``ch`` /
  ``en``) and ``output_dir`` (an absolute directory);
* runs **VLM** parsing with **OCR forced on**, and does *not* let the caller
  override either setting (the skill records
  ``caller_can_override_model_or_ocr: false`` in every receipt);
* persists a native MinerU ``content_list`` JSON **inside** ``output_dir`` and
  returns ``structuredContent.result`` (or a JSON text result) listing, per
  volume, its ``filename``, ``status`` and the saved ``content_list`` location.

The public MinerU API is not an MCP server, so this wrapper bridges it, using
the endpoints verified against the live service:

    POST https://mineru.net/api/v4/file-urls/batch
    PUT  <signed upload url>                      (never send Content-Type)
    GET  https://mineru.net/api/v4/extract-results/batch/{batch_id}
    GET  <full_zip_url>

Credentials
===========
The API token is read from the ``MINERU_API_TOKEN`` environment variable only.
It is never written to disk, never echoed, and signed URLs are never logged.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from pypdf import PdfReader

API_BASE = "https://mineru.net/api/v4"
TOOL_NAME = "parse_documents_vlm_forced_ocr"

# Enforced transport settings: the contract forbids caller overrides.
FORCED_MODEL_VERSION = "vlm"
FORCED_OCR = True
FORCED_FORMULA = True
FORCED_TABLE = True

DEFAULT_POLL_TIMEOUT_SEC = 1800.0
POLL_INTERVAL_SEC = 5.0
MAX_ZIP_BYTES = 4 * 1024 * 1024 * 1024

SUPPORTED_LANGUAGES = {"ch", "en"}


def _log(message: str) -> None:
    """Diagnostics go to stderr so stdout stays a clean JSON-RPC stream."""

    print(f"[mineru-bridge] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# credentials + HTTP
# --------------------------------------------------------------------------- #
def _api_token() -> str:
    token = (os.environ.get("MINERU_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(
            "MINERU_API_TOKEN is not set; export it in the environment that "
            "starts this MCP server (never store it in a config file)"
        )
    return token


def _request(
    url: str,
    *,
    token: str,
    method: str = "GET",
    payload: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        detail = error.read(400).decode("utf-8", "replace")
        raise RuntimeError(f"MinerU HTTP {error.code} for {url}: {detail}") from error
    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"MinerU returned non-JSON for {url}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"MinerU returned a non-object payload for {url}")
    return value


def _upload_file(upload_url: str, path: Path) -> None:
    """PUT the volume to the signed URL.

    The signature covers the request headers, so ``Content-Type`` must be absent;
    ``http.client`` only adds ``Content-Length``.
    """

    parts = urllib.parse.urlsplit(upload_url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    connection = http.client.HTTPSConnection(parts.netloc, timeout=600)
    try:
        with open(path, "rb") as handle:
            body = handle.read()
        connection.request("PUT", target, body=body)
        response = connection.getresponse()
        response.read()
        if response.status not in (200, 201):
            raise RuntimeError(f"MinerU upload failed with HTTP {response.status}")
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# MinerU batch flow
# --------------------------------------------------------------------------- #
def _submit(token: str, path: Path, language: str) -> tuple[str, str]:
    payload = {
        "enable_formula": FORCED_FORMULA,
        "enable_table": FORCED_TABLE,
        "language": language,
        "model_version": FORCED_MODEL_VERSION,
        "files": [
            {
                "name": path.name,
                "is_ocr": FORCED_OCR,
                "data_id": path.stem[:120],
            }
        ],
    }
    response = _request(f"{API_BASE}/file-urls/batch", token=token, method="POST", payload=payload)
    if response.get("code") != 0:
        raise RuntimeError(
            f"MinerU submit rejected: code={response.get('code')} msg={response.get('msg')}"
        )
    data = response.get("data") or {}
    batch_id = data.get("batch_id")
    urls = data.get("file_urls") or []
    if not batch_id or not urls:
        raise RuntimeError("MinerU submit returned no batch_id or upload URL")
    return str(batch_id), str(urls[0])


def _poll(token: str, batch_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_state = ""
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SEC)
        response = _request(f"{API_BASE}/extract-results/batch/{batch_id}", token=token)
        results = ((response.get("data") or {}).get("extract_result")) or []
        if not results:
            continue
        item = results[0]
        state = str(item.get("state") or "")
        if state != last_state:
            _log(f"batch {batch_id[:8]}… state={state}")
            last_state = state
        if state == "done":
            return item
        if state in {"failed", "error"}:
            raise RuntimeError(
                f"MinerU parse failed: {item.get('err_msg') or item.get('err_code') or state}"
            )
    raise RuntimeError(f"MinerU parse timed out after {int(timeout)}s (last state {last_state!r})")


def _download(token: str, zip_url: str, destination: Path) -> str:
    request = urllib.request.Request(zip_url, headers={"Accept": "*/*"})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=900) as response, open(destination, "wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ZIP_BYTES:
                raise RuntimeError("MinerU result archive exceeds the wrapper limit")
            digest.update(chunk)
            handle.write(chunk)
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise RuntimeError(f"MinerU archive contains an unsafe path: {member.filename}")
            target = (root / name).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"MinerU archive escapes the output directory: {member.filename}")
        bundle.extractall(root)


# --------------------------------------------------------------------------- #
# content_list normalisation
# --------------------------------------------------------------------------- #
def _pdf_page_count(path: Path) -> int:
    with open(path, "rb") as handle:
        return len(PdfReader(handle).pages)


def _find_content_list(extract_dir: Path) -> Optional[Path]:
    preferred = extract_dir / "content_list.json"
    if preferred.is_file():
        return preferred
    matches = sorted(extract_dir.glob("*_content_list.json"))
    if matches:
        return matches[0]
    matches = sorted(extract_dir.rglob("*_content_list.json"))
    return matches[0] if matches else None


def _normalize_content_list(extract_dir: Path, pdf_path: Path) -> tuple[Path, int]:
    """Ensure every volume page has explicit evidence, then persist the list.

    The skill's validator requires a ``page_idx`` for *every* page of the volume,
    and a page must either carry content or be explicitly declared empty. MinerU
    omits blank pages entirely, so declarations are added for the gaps.
    """

    source = _find_content_list(extract_dir)
    if source is None:
        raise RuntimeError("MinerU archive contains no content_list JSON")

    with open(source, "r", encoding="utf-8") as handle:
        blocks = json.load(handle)
    if not isinstance(blocks, list):
        raise RuntimeError("MinerU content_list root is not a list")

    page_count = _pdf_page_count(pdf_path)

    seen: set[int] = set()
    for block in blocks:
        if not isinstance(block, dict):
            raise RuntimeError("MinerU content_list contains a non-object block")
        index = block.get("page_idx")
        if not isinstance(index, int):
            raise RuntimeError("MinerU content block has no integer page_idx")
        if index < 0 or index >= page_count:
            raise RuntimeError(
                f"MinerU page_idx {index} is outside the volume page count {page_count}"
            )
        seen.add(index)

    missing = sorted(set(range(page_count)) - seen)
    for index in missing:
        blocks.append(
            {
                "type": "empty_page",
                "empty": True,
                "reason": "provider returned no content block for this page",
                "page_idx": index,
            }
        )
    if missing:
        _log(f"declared {len(missing)} page(s) empty: {missing}")

    blocks.sort(key=lambda block: int(block.get("page_idx", 0)))

    target = extract_dir / "content_list.json"
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(blocks, handle, ensure_ascii=False, indent=2)
    return target, page_count


# --------------------------------------------------------------------------- #
# MCP tool
# --------------------------------------------------------------------------- #
def parse_documents_vlm_forced_ocr(
    file_sources: List[str],
    language: str = "ch",
    output_dir: str = "",
) -> List[Dict[str, Any]]:
    """Parse PDF volumes with MinerU using the VLM model and OCR forced on.

    Args:
        file_sources: Absolute paths of the PDF transport volumes to parse.
        language: Document language, ``ch`` or ``en``.
        output_dir: Absolute directory where extraction artifacts are written.
    """

    if not isinstance(file_sources, list) or not file_sources:
        raise ValueError("file_sources must be a non-empty list of absolute PDF paths")

    normalized_language = str(language or "").strip().lower()
    if normalized_language not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"language must be one of {sorted(SUPPORTED_LANGUAGES)} "
            f"(got {language!r})"
        )

    if not output_dir or not os.path.isabs(str(output_dir)):
        raise ValueError("output_dir must be an absolute directory path")
    raw_dir = Path(output_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    token = _api_token()
    poll_timeout = float(os.environ.get("MINERU_POLL_TIMEOUT_SEC", DEFAULT_POLL_TIMEOUT_SEC))

    results: List[Dict[str, Any]] = []
    for source in file_sources:
        path = Path(str(source)).expanduser()
        entry: Dict[str, Any] = {"filename": path.name}
        try:
            if not path.is_absolute() or not path.is_file():
                raise RuntimeError(f"file source is not an absolute existing file: {source}")

            _log(f"{path.name}: submitting (model={FORCED_MODEL_VERSION}, is_ocr={FORCED_OCR})")
            batch_id, upload_url = _submit(token, path, normalized_language)
            _upload_file(upload_url, path)

            item = _poll(token, batch_id, poll_timeout)
            zip_url = item.get("full_zip_url")
            if not zip_url:
                raise RuntimeError("MinerU result has no full_zip_url")

            extract_dir = raw_dir / f"{path.stem}_{batch_id[:8]}"
            archive = raw_dir / f"{path.stem}_{batch_id[:8]}.zip"
            archive_sha256 = _download(token, str(zip_url), archive)
            _safe_extract(archive, extract_dir)

            content_list, page_count = _normalize_content_list(extract_dir, path)
            _log(f"{path.name}: {page_count} page(s) written to {extract_dir.name}")

            entry.update(
                {
                    "status": "success",
                    "extract_path": str(extract_dir),
                    "content_list_path": str(content_list),
                    "page_count": page_count,
                    "model_version": FORCED_MODEL_VERSION,
                    "enable_ocr": FORCED_OCR,
                    "language": normalized_language,
                    "zip_sha256": archive_sha256,
                }
            )
        except Exception as error:  # noqa: BLE001 - reported per volume, not raised
            _log(f"{path.name}: FAILED - {error}")
            entry.update({"status": "failed", "error": str(error)})
        results.append(entry)

    # Return the bare list: the skill accepts it either as
    # ``structuredContent.result`` or as a text block that decodes to a JSON
    # list (``mineru_items`` in scripts/mcp_batch.py).  A dict wrapper such as
    # ``{"result": [...]}`` fails its text fallback and is therefore avoided.
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="MinerU cloud MCP wrapper.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    mcp = FastMCP(name="MinerU MCP server (cloud /api/v4, VLM + forced OCR)", mask_error_details=True)
    mcp.tool(
        TOOL_NAME,
        description=(
            "Parse PDF volumes with MinerU using model_version='vlm' and OCR "
            "forced on (neither is caller-overridable). Writes a native "
            "content_list JSON per volume inside output_dir and returns a list "
            "of {'filename', 'status', 'extract_path', 'content_list_path', "
            "'page_count'} entries."
        ),
    )(parse_documents_vlm_forced_ocr)

    _log(f"starting; token present={bool((os.environ.get('MINERU_API_TOKEN') or '').strip())}")
    mcp.run(transport="stdio", log_level="INFO" if args.verbose else "WARNING")


if __name__ == "__main__":
    main()
