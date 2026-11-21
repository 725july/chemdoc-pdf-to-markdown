#!/usr/bin/env python3
"""Operator-side page renderer for the dual-MCP book skill (Phase 4).

The skill contract requires `review/page-render-manifest.json` with one
full-resolution render per PDF page, but the skill ships no renderer for it
(`workflow.py` only renders low-volume language samples).  This script renders
each canonical single-page PDF at review resolution and writes the manifest in
the exact shape `review_queue.py:render_manifest_context` and
`validate_run.py` require:

  * `source_sha256` / `page_count` equal to `page-manifest.json`
  * `renders[]` in ascending `pdf_page` order, one row per page 1..N
  * per row: `path` (run-root-relative), `sha256`, `width`, `height`, `dpi`
    (all plain ints)

It lives outside the skill checkout on purpose: adding or editing any file
inside `<skill-dir>` changes the frozen contract-v2 governance hashes and
makes the current run stale.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import render


DEFAULT_DPI = 300
SCHEMA_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument(
        "--render-subdir", default="renders/pages", help="run-root-relative output dir"
    )
    parser.add_argument(
        "--manifest",
        default="review/page-render-manifest.json",
        help="run-root-relative manifest path",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="re-render pages whose PNG already exists",
    )
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        print(json.dumps({"error": f"run dir not found: {run_dir}"}))
        return 2

    page_manifest = json.loads(
        (run_dir / "page-manifest.json").read_text(encoding="utf-8")
    )
    source_sha = str(page_manifest.get("source_sha256", ""))
    page_count = int(page_manifest.get("page_count", 0) or 0)
    pages = page_manifest.get("pages") or []
    if len(source_sha) != 64 or page_count <= 0 or len(pages) != page_count:
        print(json.dumps({"error": "invalid page-manifest.json"}))
        return 2

    render_dir = run_dir / args.render_subdir
    render_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / args.manifest
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    renders: list[dict] = []
    for page in sorted(pages, key=lambda item: int(item["pdf_page"])):
        page_number = int(page["pdf_page"])
        page_pdf = run_dir / str(page["relative_path"]).replace("\\", "/")
        if not page_pdf.is_file():
            print(json.dumps({"error": f"missing canonical page: {page_pdf}"}))
            return 2
        actual_page_sha = sha256_file(page_pdf)
        if actual_page_sha != page.get("sha256"):
            print(
                json.dumps(
                    {
                        "error": f"page {page_number} hash mismatch",
                        "expected": page.get("sha256"),
                        "observed": actual_page_sha,
                    }
                )
            )
            return 2

        png_path = render_dir / f"page_{page_number:04d}.png"
        if args.overwrite or not png_path.is_file():
            _doc = render.open_doc(page_pdf)
            try:
                if render.page_count(_doc) != 1:
                    print(
                        json.dumps(
                            {"error": f"page {page_number}: expected 1 page in page PDF"}
                        )
                    )
                    return 2
                width, height = render.save_png(_doc, 0, png_path, args.dpi)
            finally:
                render.close(_doc)
        else:
            width, height = render.load_png_size(png_path)

        renders.append(
            {
                "pdf_page": page_number,
                "path": png_path.relative_to(run_dir).as_posix(),
                "sha256": sha256_file(png_path),
                "width": int(width),
                "height": int(height),
                "dpi": int(args.dpi),
                "source_page_sha256": str(page.get("sha256")),
                "media_box": page.get("media_box"),
            }
        )

    if [row["pdf_page"] for row in renders] != list(range(1, page_count + 1)):
        print(json.dumps({"error": "render coverage is not 1..page_count"}))
        return 2

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": utc_now(),
        "source_sha256": source_sha,
        "page_count": page_count,
        "dpi": int(args.dpi),
        "renderer": "operator-render_review_pages.py/pypdfium2",
        "renders": renders,
    }
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)

    print(
        json.dumps(
            {
                "manifest": manifest_path.relative_to(run_dir).as_posix(),
                "pages": page_count,
                "dpi": int(args.dpi),
                "renders": [
                    {
                        "pdf_page": row["pdf_page"],
                        "path": row["path"],
                        "width": row["width"],
                        "height": row["height"],
                        "sha256": row["sha256"][:16] + "...",
                    }
                    for row in renders
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
