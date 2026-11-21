# -*- coding: utf-8 -*-
"""**渲染与切片后端（唯一出口）**：pypdfium2（PDFium）负责光栅化，pypdf 负责搬页。

**为什么是它**（B1-4 之后的新决定）：PyMuPDF 是 **AGPL-3.0 / 商业双许可**，而本项目**从不使用**它的
OCR、表格抽取与文本结构能力——实测它只被用来做四件事：① 灰度光栅化 ② 页切片/合并 ③ 文本层字符数
④ 页内嵌图计数。前两件 pypdfium2 + pypdf 完全胜任，后两件在管线里**没有任何消费方**（孤儿字段）。
换成 **Apache-2.0 / BSD-3** 的组合后，发布时不再受 AGPL 传染。

**口径（A/B 实验实测，2026-10-13）**：两库的灰度约定**完全一致**（单通道、0=黑 / 255=白、同尺寸），
dpi=100 时平均差 0.2–2.3 灰阶（`>8` 的像素 0.8–9.6%，最大 40–62）；但 **PDFium 略"重"**：
`<128` 的墨量比 MuPDF 高约 **0.1–0.7 个百分点**（相对 +20~25%），因此 **S0 的块/行段数在个别页会变**
（实测 5 页里 p4、p5 变），整页墨密度在 dpi=300 时几乎相同（0.04085 vs 0.04100）。
→ **这是"重标定"的依据**：S0 的像素计数阈值与画像需按 PDFium 重算（见 `画像版本化` 待办）。

用法（其它模块只许用这些函数，不得再直接 import PDF 库）:
    doc = render.open_doc(pdf)          # 用完 render.close(doc)
    a = render.render_gray(doc, i, dpi) # np.uint8 (H, W)，0=黑
    render.slice_pages(pdf, 1, 100, out)      # 1-based 闭区间
    render.slice_ranges(pdf, [1, 3, 5], out)  # 任意（跳页）页码，1-based
"""
from __future__ import annotations

import hashlib
import os
from typing import Iterable

import numpy as np
import pypdfium2 as pdfium
import paths  # B2：工作根唯一出口（SPEC §7.10）

try:                                                             # pypdfium2 5.x 用 raw 常量
    _PAGEOBJ_IMAGE = pdfium.raw.FPDF_PAGEOBJ_IMAGE
except Exception:                                                # pragma: no cover
    _PAGEOBJ_IMAGE = 3

BACKEND = "pypdfium2+pypdf"


def _info_version(info, fallback: str = "unknown") -> str:
    """pypdfium2 5.x 的版本在 `pypdfium2.version.PYPDFIUM_INFO / PDFIUM_INFO`；
    4.x 的 `V_PYPDFIUM2 / V_LIBPDFIUM` 已被移除 —— **这里曾写错，导致标识退化成 `?`**。"""
    v = getattr(info, "version", None)
    return str(v) if v else fallback


def _pkg_version(name: str) -> str:
    try:
        import importlib.metadata as _md
        return _md.version(name)
    except Exception:                                            # noqa: BLE001
        return "unknown"


# 版本标识：写进画像（`profile.render_versions`）与产物（`report.json.render.versions`）
BACKEND_VERSIONS = {
    "pypdfium2": _info_version(getattr(getattr(pdfium, "version", None), "PYPDFIUM_INFO", None)),
    "pdfium": _info_version(getattr(getattr(pdfium, "version", None), "PDFIUM_INFO", None)),
    "pypdf": _pkg_version("pypdf"),
}


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 打开 / 页信息
def open_doc(path: str):
    return pdfium.PdfDocument(path)


def close(doc) -> None:
    try:
        doc.close()
    except Exception:                                            # noqa: BLE001
        pass


def page_count(doc) -> int:
    return len(doc)


def page_size_pt(doc, idx: int) -> tuple:
    """页宽高（pt）。idx 从 0 起。"""
    return tuple(doc[idx].get_size())


def page_rotation(doc, idx: int) -> int:
    return int(doc[idx].get_rotation())


# ---------------------------------------------------------------- 光栅化（灰度）
def render_gray(doc, idx: int, dpi: int) -> np.ndarray:
    """把第 idx 页（0 起）渲染成灰度数组：**单通道 uint8，(H, W)，0=黑 / 255=白**。"""
    page = doc[idx]
    bmp = page.render(scale=float(dpi) / 72.0, grayscale=True, rev_byteorder=False)
    try:
        arr = np.asarray(bmp.to_numpy()).copy()
    finally:
        try:
            bmp.close()
        except Exception:                                        # noqa: BLE001
            pass
    if arr.ndim == 3:                                            # 保险：某些版本给 (H,W,1)
        arr = arr[:, :, 0]
    return arr.astype(np.uint8, copy=False)


def render_gray_page(path: str, page_1based: int, dpi: int) -> np.ndarray:
    doc = open_doc(path)
    try:
        return render_gray(doc, page_1based - 1, dpi)
    finally:
        close(doc)


def render_rgb(doc, idx: int, dpi: int) -> np.ndarray:
    """第 idx 页渲染成 **RGB** 数组：uint8，(H, W, 3)，通道顺序已校验（见 selftest 的红色页用例）。"""
    page = doc[idx]
    bmp = page.render(scale=float(dpi) / 72.0, rev_byteorder=True)   # rev_byteorder=True → RGB
    try:
        arr = np.asarray(bmp.to_numpy()).copy()
    finally:
        try:
            bmp.close()
        except Exception:                                        # noqa: BLE001
            pass
    if arr.ndim == 2:                                            # 保险：灰度输出时补成三通道
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    return arr[:, :, :3].astype(np.uint8, copy=False)


def save_png(doc, idx: int, out_path: str, dpi: int) -> tuple:
    """渲染第 idx 页并存成 PNG，返回 (width, height)。用 PIL 写盘（不引入额外图像库）。"""
    from PIL import Image
    arr = render_rgb(doc, idx, dpi)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    Image.fromarray(arr).save(out_path)
    return int(arr.shape[1]), int(arr.shape[0])


def load_png_size(path: str) -> tuple:
    """读已存在 PNG 的 (width, height)——替代原先"用 PDF 库打开 PNG"的写法。"""
    from PIL import Image
    with Image.open(path) as im:
        return int(im.size[0]), int(im.size[1])


def measure_page_bytes(pdf: str, pages: Iterable[int] | None = None) -> dict:
    """一次读源、逐页写出"单页 PDF"并量字节（守卷体积上限用）。返回 {页号(1起): 字节}。

    为什么不用"打开源 N 次"：`slice_ranges` 每次都会重新解析源 PDF，300 页会慢一个量级。
    """
    import tempfile
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(pdf)
    total = len(reader.pages)
    ps = _check_pages(list(pages) if pages is not None else list(range(1, total + 1)), total)
    out = {}
    for p in ps:
        fd, tmp = tempfile.mkstemp(prefix="pgbytes-", suffix=".pdf")
        os.close(fd)
        try:
            w = PdfWriter()
            w.add_page(reader.pages[p - 1])
            with open(tmp, "wb") as fh:
                w.write(fh)
            out[p] = os.path.getsize(tmp)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return out


def slice_bytes(src_path: str, pages: Iterable[int]) -> dict:
    """把若干页搬成一份**临时** PDF，只返回 `{count, bytes, sha256}`（不落盘到产物区）。"""
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="slicebytes-", suffix=".pdf")
    os.close(fd)
    try:
        info = slice_ranges(src_path, pages, tmp)
        return {"count": info["count"], "bytes": info["bytes"], "sha256": info["sha256"]}
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------- 文本层与对象（孤儿字段用）
def page_text(doc, idx: int) -> str:
    try:
        tp = doc[idx].get_textpage()
        return tp.get_text_range() or ""
    except Exception:                                            # noqa: BLE001
        return ""


def page_image_count(doc, idx: int) -> int:
    n = 0
    try:
        for obj in doc[idx].get_objects():
            try:
                if obj.type == _PAGEOBJ_IMAGE:
                    n += 1
            finally:
                try:
                    obj.close()
                except Exception:                                # noqa: BLE001
                    pass
    except Exception:                                            # noqa: BLE001
        return 0
    return n


# ---------------------------------------------------------------- 切片 / 搬页
def _check_pages(pages: Iterable[int], total: int) -> list:
    ps = list(pages)
    if not ps:
        raise ValueError("页码列表为空")
    if any((not isinstance(p, int)) or isinstance(p, bool) for p in ps):
        raise ValueError("页码必须是整数")
    if any(p < 1 or p > total for p in ps):
        raise ValueError("页码越界（1..%d）：%s" % (total, [p for p in ps if p < 1 or p > total]))
    if len(set(ps)) != len(ps):
        raise ValueError("页码重复：%s" % sorted({p for p in ps if ps.count(p) > 1}))
    return ps


def slice_ranges(src_path: str, pages: Iterable[int], out_path: str, compress: bool = True) -> dict:
    """按**任意页码**（1-based，可跳页）裁出新的 PDF；片内页 = 1..N 连续。"""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(src_path)
    total = len(reader.pages)
    ps = _check_pages(pages, total)
    writer = PdfWriter()
    for p in ps:
        writer.add_page(reader.pages[p - 1])
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as fh:
        writer.write(fh)
    if compress:                                                 # pypdf 无 deflate 开关，保持原流
        pass
    return {"out": os.path.abspath(out_path), "pages": ps, "count": len(ps),
            "bytes": os.path.getsize(out_path), "sha256": sha256_file(out_path),
            "source": {"path": os.path.abspath(src_path), "page_count": total,
                       "sha256": sha256_file(src_path)}}


def slice_pages(src_path: str, start: int, end: int, out_path: str) -> dict:
    """连续闭区间 [start, end]（1-based）。等价于原先的 `insert_pdf(from_page=start-1, to_page=end-1)`。"""
    if not (isinstance(start, int) and isinstance(end, int)) or start > end:
        raise ValueError("区间非法：start=%r end=%r" % (start, end))
    return slice_ranges(src_path, list(range(start, end + 1)), out_path)


def extract_pdf_pages(src_path: str, out_path: str, pages: Iterable[int] | None = None) -> dict:
    """`pages=None` → 整本复制（用于"单卷 = 全书"的场景）。"""
    from pypdf import PdfReader
    total = len(PdfReader(src_path).pages)
    return slice_ranges(src_path, list(pages) if pages is not None else list(range(1, total + 1)), out_path)


# ---------------------------------------------------------------- 自测
def selftest() -> int:
    import io
    import tempfile
    from pypdf import PdfReader, PdfWriter

    cases = []
    cases.append(("版本标识非空且像版本号（曾经退化成问号，故加断言）",
                  all(v and v != "?" and any(ch.isdigit() for ch in v)
                      for v in BACKEND_VERSIONS.values()), BACKEND_VERSIONS))

    # 合成一份 5 页空白 PDF（纯 pypdf，不经任何 PDF 渲染库）
    blank = io.BytesIO()
    w = PdfWriter()
    for _ in range(5):
        w.add_blank_page(width=595, height=842)
    w.write(blank)
    fd, tmp = tempfile.mkstemp(prefix="render-", suffix=".pdf")
    os.close(fd)
    with open(tmp, "wb") as fh:
        fh.write(blank.getvalue())

    try:
        doc = open_doc(tmp)
        cases.append(("页数正确（5）", page_count(doc) == 5, page_count(doc)))
        wpt, hpt = page_size_pt(doc, 0)
        cases.append(("页尺寸≈A4（595×842 pt）", abs(wpt - 595) < 1 and abs(hpt - 842) < 1, (wpt, hpt)))
        cases.append(("旋转为 0", page_rotation(doc, 0) == 0, page_rotation(doc, 0)))

        a = render_gray(doc, 0, 100)
        cases.append(("灰度数组：单通道 uint8、尺寸≈A4@100dpi",
                      a.ndim == 2 and a.dtype == np.uint8
                      and abs(a.shape[0] - round(842 / 72.0 * 100)) <= 2
                      and abs(a.shape[1] - round(595 / 72.0 * 100)) <= 2, (a.shape, str(a.dtype))))
        cases.append(("空白页渲染为白（>=250）", int(a.min()) >= 250, int(a.min())))
        cases.append(("文本层为空", page_text(doc, 0) == "", repr(page_text(doc, 0)[:20])))
        cases.append(("嵌图计数为 0", page_image_count(doc, 0) == 0, page_image_count(doc, 0)))

        # 切片：跳页 / 连续 / 整本
        o1 = tmp + ".ranges.pdf"
        r1 = slice_ranges(tmp, [1, 3, 5], o1)
        cases.append(("跳页切片 [1,3,5] → 3 页",
                      r1["count"] == 3 and len(PdfReader(o1).pages) == 3, r1["count"]))
        o2 = tmp + ".slice.pdf"
        r2 = slice_pages(tmp, 2, 4, o2)
        cases.append(("连续切片 [2,4] → 3 页", r2["count"] == 3 and len(PdfReader(o2).pages) == 3, r2["count"]))
        o3 = tmp + ".whole.pdf"
        r3 = extract_pdf_pages(tmp, o3)
        cases.append(("整本复制 → 5 页", r3["count"] == 5, r3["count"]))
        cases.append(("切片元数据带源 sha256 与页数",
                      len(r1["source"]["sha256"]) == 64 and r1["source"]["page_count"] == 5, None))

        # 负例：参数校验必须真的会拦
        def bad(fn):
            try:
                fn()
                return False
            except ValueError:
                return True
        cases.append(("负例：页码越界 → 报错", bad(lambda: slice_ranges(tmp, [0, 1], tmp + ".x")), None))
        cases.append(("负例：页码重复 → 报错", bad(lambda: slice_ranges(tmp, [1, 1], tmp + ".x")), None))
        cases.append(("负例：空页码 → 报错", bad(lambda: slice_ranges(tmp, [], tmp + ".x")), None))
        cases.append(("负例：非整数页码 → 报错", bad(lambda: slice_ranges(tmp, ["1"], tmp + ".x")), None))
        cases.append(("负例：区间反了 → 报错", bad(lambda: slice_pages(tmp, 4, 2, tmp + ".x")), None))
        close(doc)

        # 颜色通道顺序：合成一页**纯红**，若 BGR/RGB 弄反就会测出来
        red = io.BytesIO()
        wr = PdfWriter()
        pg = wr.add_blank_page(width=200, height=200)
        from pypdf.generic import DecodedStreamObject, NameObject
        st = DecodedStreamObject()
        st.set_data(b"1 0 0 rg 0 0 200 200 re f")
        pg[NameObject("/Contents")] = wr._add_object(st)
        wr.write(red)
        fd2, tmp2 = tempfile.mkstemp(prefix="render-rgb-", suffix=".pdf")
        os.close(fd2)
        with open(tmp2, "wb") as fh:
            fh.write(red.getvalue())
        d2 = open_doc(tmp2)
        rgb = render_rgb(d2, 0, 72)
        mean = rgb.reshape(-1, 3).mean(axis=0)
        cases.append(("颜色通道顺序为 RGB（纯红页：R 高、G/B 低）",
                      rgb.shape[2] == 3 and mean[0] > 200 and mean[1] < 60 and mean[2] < 60,
                      [round(float(x), 1) for x in mean]))
        png_out = tmp2 + ".png"
        wpx, hpx = save_png(d2, 0, png_out, 72)
        cases.append(("save_png 写盘且尺寸自洽",
                      os.path.exists(png_out) and load_png_size(png_out) == (wpx, hpx) and wpx == 200,
                      (wpx, hpx, load_png_size(png_out) if os.path.exists(png_out) else None)))
        close(d2)
        pb = measure_page_bytes(tmp)
        cases.append(("批量量页字节：返回 5 页且每页 >0",
                      sorted(pb) == [1, 2, 3, 4, 5] and all(v > 0 for v in pb.values()), pb))
        sb = slice_bytes(tmp, [1, 2])
        cases.append(("临时切片字节：count=2 且带 sha256",
                      sb["count"] == 2 and len(sb["sha256"]) == 64 and sb["bytes"] > 0, None))
        os.remove(tmp2)
        if os.path.exists(png_out):
            os.remove(png_out)

        # 真实样本（有就测）：几何与墨量落在合理带内
        real = os.path.join(paths.data_root(),
                            "_test-inputs", "普通化学原理 第4版-22-25,29.pdf")
        if os.path.exists(real):
            d = open_doc(real)
            g = render_gray(d, 0, 100)
            ink = float((g < 128).mean())
            cases.append(("真实样本 p1@100dpi：形状与墨量合理（0.005<墨<0.10）",
                          g.shape == (1016, 707) and 0.005 < ink < 0.10, (g.shape, round(ink, 5))))
            cases.append(("真实样本：扫描页文本层为空（→ 走 OCR 路由）", page_text(d, 0).strip() == "", None))
            cases.append(("真实样本：页内嵌图计数 > 0", page_image_count(d, 0) > 0, page_image_count(d, 0)))
            close(d)
        else:
            cases.append(("真实样本不存在 → 跳过（不算失败）", True, None))
    finally:
        for p in (tmp, tmp + ".ranges.pdf", tmp + ".slice.pdf", tmp + ".whole.pdf"):
            if os.path.exists(p):
                os.remove(p)

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:120]))
        passed += bool(ok)
    print("\n%d/%d render tests passed  （后端 %s，PDFium %s）"
          % (passed, len(cases), BACKEND, BACKEND_VERSIONS["pdfium"]))
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
