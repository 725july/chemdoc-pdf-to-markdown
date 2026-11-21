#!/usr/bin/env python3
"""PaddleOCR-VL 引擎直出：直接调 PaddleOCRVL，把结果对象里的**结构化字段原样落盘**。

与现状的区别：`dual-mcp-bridge/paddle_marker_bridge.py` 只取 `markdown.markdown_texts`
与 `markdown.markdown_images`，把 `parsing_res_list`（块标签/内容/bbox/order/多边形）
与 `layout_det_res` 全部丢弃、从不落盘。本脚本一个字段都不丢。

用法：
    python dump_paddle_raw.py <pdf> <out-dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


def _unshadow_stdlib() -> list:
    """把**本目录**从 `sys.path` 摘掉，避免 `_mcp-test/secrets.py` 顶掉**标准库 `secrets`**。

    为什么：`python _mcp-test\\dump_paddle_raw.py` 会把**脚本目录**放进 `sys.path[0]`；本目录里的
    `secrets.py`（工作区自己的密钥工具）于是抢在标准库之前 → `numpy.random` 报
    `ImportError: cannot import name randbits`（本机实测：PaddleOCR-VL 整个起不来）。
    `-P` 与清空 PYTHONPATH 都救不了（Python 3.12 实测：脚本目录/PYTHONPATH 都排在标准库之前）。

    本目录只在**显式传 `--book`** 时才需要（要 import `books`/`render`），所以默认摘掉、
    需要时再用 `_with_here()` 临时加回。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    removed = [p for p in list(sys.path) if p and os.path.abspath(p) == here]
    for p in removed:
        try:
            sys.path.remove(p)
        except ValueError:
            pass
    return removed


def _with_here(removed: list):
    """上下文管理器：把摘掉的本目录临时加回（供 `--book` 分支 import `books`/`render`）。"""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        sys.path[:0] = removed
        try:
            yield
        finally:
            for p in removed:
                try:
                    sys.path.remove(p)
                except ValueError:
                    pass
    return _cm()


_REMOVED_HERE = _unshadow_stdlib()

STRUCTURED_KEYS = (
    "parsing_res_list",
    "layout_det_res",
    "imgs_in_doc",
    "page_index",
    "input_path",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def jsonable(value):
    """尽力转成 JSON 可序列化；不可序列化的退回 repr。

    注意：numpy 数组/标量必须走 `.tolist()`。早先只判断 dict/list/tuple，
    于是 `polygon_points`（ndarray）被写成 `"<ndarray: array([[...]])>"` 这种**repr 字符串**，
    机器再也读不回来（S2 侦察发现，见 docs/new/S2-IR适配器-结果.md §2）。
    """

    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        if hasattr(value, "tolist"):          # numpy.ndarray / 标量 / 张量
            try:
                return value.tolist()
            except Exception:  # noqa: BLE001
                pass
        if isinstance(value, dict):
            return {str(k): jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [jsonable(v) for v in value]
        return f"<{type(value).__name__}: {value!r}>"


def block_to_dict(block) -> dict:
    """block 可能是对象也可能已是 dict。"""

    if isinstance(block, dict):
        return jsonable(block)
    out = {}
    for attr in ("label", "content", "bbox", "polygon_points", "score"):
        if hasattr(block, attr):
            out[f"block_{attr}" if attr in {"label", "content", "bbox"} else attr] = jsonable(
                getattr(block, attr)
            )
    return out


def save_images(items, directory: Path) -> list[dict]:
    directory.mkdir(parents=True, exist_ok=True)
    saved = []
    for index, item in enumerate(items):
        image = None
        name = None
        if isinstance(item, dict):
            image = item.get("img")
            name = item.get("path") or item.get("name")
        if image is None:
            continue
        target = directory / (Path(str(name)).name if name else f"image_{index:03d}.png")
        try:
            image.save(target)
            saved.append(
                {
                    "name": str(name),
                    "file": target.name,
                    "bytes": target.stat().st_size,
                    "sha256": sha256_file(target),
                }
            )
        except Exception as exc:  # noqa: BLE001
            saved.append({"name": str(name), "error": repr(exc)})
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", help="源 PDF（位置参数；用 --book 时可省）")
    parser.add_argument("out_dir", nargs="?", help="产出目录（位置参数；用 --book 时可省）")
    parser.add_argument("--pipeline-version", default="v1.6")
    # A3 runner 契约：同一个直出实现也能按 `--book <key>` 调用（书级 runner = runners/paddle_local.py
    # 会逐页切片后调本脚本；这里给一条**等价的书级入口**，便于手工重跑，语义与位置参数完全一致）
    parser.add_argument("--book", help="书注册表 key（books.json）：pdf/out_dir 由注册表给（A3 契约）")
    # A3：逐页切片跑时，`page_index` 永远是本页 PDF 的 0（每页一份单页 PDF）→ 必须显式给出**书内页号**，
    # 否则 S2 会把所有见证块都标成第 1 页（实测 50 页全 page_index=0）
    parser.add_argument("--book-page", type=int, default=0,
                        help="本次输入是书内第几页（1-based）；>0 时把 page_index 记成该书内页号（0-based）")
    args = parser.parse_args()

    if args.book and not (args.pdf and args.out_dir):
        with _with_here(_REMOVED_HERE):                  # `books`/`paths` 在本目录，只在书级入口才需要
            import books as _books
            import paths as _paths
            cfg = _books.load()[args.book]
        # 注册表里的路径是**相对数据根**的 POSIX 风格（与 engines/pipeline 同一口径；B2 起走 paths）
        root = Path(_paths.data_root())
        args.pdf = str(Path(cfg["pdf"]) if Path(cfg["pdf"]).is_absolute() else root / cfg["pdf"])
        args.out_dir = str(Path(cfg["paddle_dir"]) if Path(cfg["paddle_dir"]).is_absolute()
                           else root / cfg["paddle_dir"])
    if not (args.pdf and args.out_dir):
        parser.error("要么给 pdf + out_dir 两个位置参数，要么给 --book <key>")

    pdf = Path(args.pdf).resolve()
    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    record: dict = {
        "engine": "paddleocr-vl",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_pdf": str(pdf),
        "source_pdf_bytes": pdf.stat().st_size,
        "source_pdf_sha256": sha256_file(pdf),
        "pipeline_version": args.pipeline_version,
        "book_page": int(args.book_page or 0),
        "request": {"pipeline": "PaddleOCRVL", "predict_args": {}},
    }

    from paddleocr import PaddleOCRVL

    print(f"[paddle] loading PaddleOCR-VL {args.pipeline_version} ...", flush=True)
    pipeline = PaddleOCRVL(pipeline_version=args.pipeline_version)
    print("[paddle] pipeline ready; predicting ...", flush=True)
    results = list(pipeline.predict(str(pdf)))
    print(f"[paddle] {len(results)} page result(s)", flush=True)

    pages = []
    for index, result in enumerate(results):
        page_index = None
        try:
            page_index = result["page_index"]
        except Exception:  # noqa: BLE001
            page_index = getattr(result, "page_index", None)
        # 逐页切片跑（`--book-page N`）：本页 PDF 的 page_index 恒 0 → 落盘前**改写成书内页号**，
        # 让 `page_XXXX/` 目录名与 `result.json.page_index` 在**书内页号空间**自洽（S2 直接读它）。
        if args.book_page:
            page_index = int(args.book_page) - 1
            try:
                result["page_index"] = page_index
            except Exception:  # noqa: BLE001
                pass
        label = f"page_{int(page_index) + 1:04d}" if isinstance(page_index, int) else f"result_{index:04d}"
        page_dir = out / label
        page_dir.mkdir(parents=True, exist_ok=True)
        entry: dict = {"result_index": index, "page_index": page_index, "dir": label}

        # 1) 整体 JSON（若引擎提供）
        try:
            whole = result.json
            payload = jsonable(whole)
            (page_dir / "result.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            entry["result_json_saved"] = True
            if isinstance(payload, dict):
                entry["result_json_top_keys"] = sorted(payload.keys())
        except Exception as exc:  # noqa: BLE001
            entry["result_json_saved"] = False
            entry["result_json_error"] = repr(exc)

        # 2) 逐个结构化字段（无论有没有整体 JSON 都单独落盘）
        for key in STRUCTURED_KEYS:
            try:
                value = result[key]
            except Exception:  # noqa: BLE001
                value = getattr(result, key, None)
            if value is None:
                continue
            if key == "parsing_res_list":
                payload = [block_to_dict(b) for b in value]
                (page_dir / "parsing_res_list.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                entry["parsing_res_list_blocks"] = len(payload)
            elif key == "imgs_in_doc":
                entry["images"] = save_images(value, page_dir / "images")
            else:
                (page_dir / f"{key}.json").write_text(
                    json.dumps(jsonable(value), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        # 3) markdown：默认 + 打开公式编号（后者用于验证公式编号是否只是默认参数问题）
        try:
            markdown = result.markdown
            text = markdown.get("markdown_texts") or markdown.get("text") or ""
        except Exception as exc:  # noqa: BLE001
            text = ""
            entry["markdown_error"] = repr(exc)
        (page_dir / "markdown.default.md").write_text(str(text), encoding="utf-8")
        entry["markdown_default_chars"] = len(str(text))
        try:
            with_numbers = result._to_markdown(pretty=True, show_formula_number=True)
            text_numbers = with_numbers.get("markdown_texts") or ""
            (page_dir / "markdown.formula_numbers.md").write_text(
                str(text_numbers), encoding="utf-8"
            )
            entry["markdown_numbers_chars"] = len(str(text_numbers))
            # 顺便把该次调用的图片也存下来（与默认可能不同）
            entry["images_with_numbers"] = save_images(
                list((with_numbers.get("markdown_images") or {}).items())
                if isinstance(with_numbers.get("markdown_images"), dict)
                else [],
                page_dir / "images_numbers",
            )
        except Exception as exc:  # noqa: BLE001
            entry["markdown_numbers_error"] = repr(exc)

        # 4) 记录"引擎提供了但我们没取"的字段，便于事后审计
        try:
            entry["result_keys"] = sorted(str(k) for k in result.keys())
        except Exception:  # noqa: BLE001
            entry["result_keys"] = sorted(
                a for a in dir(result) if not a.startswith("_")
            )

        pages.append(entry)
        print(
            f"[paddle] {label}: blocks={entry.get('parsing_res_list_blocks')} "
            f"md_default={entry.get('markdown_default_chars')} "
            f"md_numbers={entry.get('markdown_numbers_chars')} "
            f"images={len(entry.get('images') or [])}",
            flush=True,
        )

    record["pages"] = pages
    (out / "engine-record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[paddle] engine-direct dump -> {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)
