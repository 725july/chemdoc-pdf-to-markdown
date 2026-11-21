#!/usr/bin/env python3
"""MinerU 引擎直出：调 API，把返回的 zip **原样**解压落盘，不做任何加工。

与 skill 的区别：skill 的 `mcp_batch.save_mineru_volume` 会把 content_list 归一化后
改写成 `content_list.json`（补空页、排序、重写），并把每个块压成 page.md，丢掉
`table_caption` / `chart_caption` / `image_caption` 等字段。本脚本**一个字节都不改**。

用法：
    python dump_mineru_raw.py <pdf> <out-dir> [--language ch]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import paths  # B2：工作根唯一出口（SPEC §7.10）

BRIDGE = Path(paths.TOOLS_ROOT) / "dual-mcp-bridge" / "mineru_mcp_wrapper.py"


def load_wrapper():
    spec = importlib.util.spec_from_file_location("mineru_wrapper", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mineru_wrapper"] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf")
    parser.add_argument("out_dir")
    parser.add_argument("--language", default="ch")
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()

    pdf = Path(args.pdf).resolve()
    out = Path(args.out_dir).resolve()
    extracted = out / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)

    wrapper = load_wrapper()
    record: dict = {
        "engine": "mineru",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_pdf": str(pdf),
        "source_pdf_bytes": pdf.stat().st_size,
        "source_pdf_sha256": sha256_file(pdf),
        "request": {
            "endpoint_batch": f"{wrapper.API_BASE}/file-urls/batch",
            "endpoint_results": f"{wrapper.API_BASE}/extract-results/batch/{{batch_id}}",
            "enable_formula": wrapper.FORCED_FORMULA,
            "enable_table": wrapper.FORCED_TABLE,
            "model_version": wrapper.FORCED_MODEL_VERSION,
            "is_ocr": wrapper.FORCED_OCR,
            "language": args.language,
        },
    }

    token = wrapper._api_token()
    print(f"[mineru] submit {pdf.name} ({record['source_pdf_bytes']} bytes)")
    batch_id, upload_url = wrapper._submit(token, pdf, args.language)
    record["batch_id"] = batch_id
    print(f"[mineru] batch {batch_id}")
    wrapper._upload_file(upload_url, pdf)
    print("[mineru] uploaded, polling ...")
    item = wrapper._poll(token, batch_id, args.timeout)
    record["poll_result"] = item
    (out / "api-poll-result.json").write_text(
        json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    zip_url = item.get("full_zip_url")
    if not zip_url:
        raise SystemExit("poll result has no full_zip_url")
    archive = out / f"{pdf.stem}_{batch_id[:8]}.zip"
    record["zip_sha256"] = wrapper._download(token, str(zip_url), archive)
    record["zip_bytes"] = archive.stat().st_size
    print(f"[mineru] zip {archive.name} {record['zip_bytes']} bytes")

    # 逐字解压（不做任何改名/加工），并记录每个成员的哈希
    members = []
    with zipfile.ZipFile(archive) as bundle:
        for info in sorted(bundle.infolist(), key=lambda i: i.filename):
            if info.is_dir():
                continue
            target = extracted / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as handle:
                handle.write(source.read())
            members.append(
                {
                    "name": info.filename,
                    "bytes": target.stat().st_size,
                    "sha256": sha256_file(target),
                }
            )
    record["zip_members"] = members
    record["member_count"] = len(members)

    (out / "engine-record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[mineru] extracted {len(members)} members verbatim -> {extracted}")
    for member in members:
        print(f"    {member['name']:<72} {member['bytes']:>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
