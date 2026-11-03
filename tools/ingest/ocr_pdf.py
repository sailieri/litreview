# -*- coding: utf-8 -*-
"""扫描件 / 无文字层 PDF → 文本（RapidOCR）。

用途：pymupdf get_text() 返回 0 字符的 PDF（扫描件、文字转路径的矢量图），
用「渲染成图 + OCR」拿回正文。

用法（需已安装 rapidocr_onnxruntime）：
  python3 tools/ingest/ocr_pdf.py <pdf> --stem <stem> [--dpi 250] [--pages 0-13]
产物：
  data/extracted/<stem>_raw.md    OCR 全文（带页标记）
  data/normalized/<stem>.md       段落化文本（供索引；不跑 LLM 规范化）
"""
import sys, os, argparse
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "modules" / "paths.py").exists())
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import fitz  # pymupdf
import numpy as np
from modules.paths import extracted_dir, normalized_dir  # noqa: E402
try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:  # 可选依赖：只有扫描件 OCR 才需要，导入本模块不应因此中断
    RapidOCR = None

_OCR_HINT = ("缺少依赖 rapidocr_onnxruntime（可选，仅扫描件 OCR 用）：\n"
             "  .venv\\Scripts\\python.exe -m pip install rapidocr_onnxruntime")


def ocr_page(engine, page, dpi):
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = img[:, :, :3]
    res, _ = engine(img)
    if not res:
        return ''
    items = []
    for box, text, score in res:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append((min(ys), min(xs), text))
    items.sort(key=lambda x: (round(x[0] / 8), x[1]))   # 按行（y 聚类）再按 x
    return '\n'.join(t for _, _, t in items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pdf')
    ap.add_argument('--stem', default=None)
    ap.add_argument('--dpi', type=int, default=250)
    ap.add_argument('--pages', default='')
    ap.add_argument('--start-page', type=int, default=0)
    ap.add_argument('--end-page', type=int, default=0)
    a = ap.parse_args()

    if RapidOCR is None:
        sys.exit(_OCR_HINT)

    pdf = Path(a.pdf)
    stem = a.stem or pdf.stem
    d = fitz.open(str(pdf))
    p0, p1 = a.start_page, a.end_page or d.page_count
    p1 = min(p1, d.page_count)
    print(f'{pdf.name}: {d.page_count} 页，处理 {p0}-{p1-1}，dpi={a.dpi}', flush=True)

    eng = RapidOCR()
    chunks = []
    for i in range(p0, p1):
        txt = ocr_page(eng, d[i], a.dpi)
        chunks.append(f'--- Page {i+1} ---\n{txt}')
        print(f'  page {i+1}: {len(txt)} 字符', flush=True)

    raw = '\n\n'.join(chunks)
    EXT = extracted_dir()
    NORM = normalized_dir()
    EXT.mkdir(parents=True, exist_ok=True)
    NORM.mkdir(parents=True, exist_ok=True)
    (EXT / f'{stem}_raw.md').write_text(raw, encoding='utf-8')

    # 段落化：合并被 OCR 切断的行（中文按句末标点，英文按行长）
    body = []
    buf = ''
    for line in raw.splitlines():
        if line.startswith('--- Page'):
            if buf.strip():
                body.append(buf.strip()); buf = ''
            body.append('\n' + line + '\n'); continue
        s = line.strip()
        if not s:
            if buf.strip():
                body.append(buf.strip()); buf = ''
            continue
        if buf:
            # 英文：上一行不以句号结束且短；中文：不以句末标点结束
            if len(buf) > 0 and (buf[-1] in '。！？.!?])' ):
                body.append(buf.strip()); buf = s
            else:
                buf = buf + (' ' if buf[-1].isascii() or s[0].isascii() else '') + s
        else:
            buf = s
    if buf.strip():
        body.append(buf.strip())
    text = '\n\n'.join(body)
    (NORM / f'{stem}.md').write_text(text, encoding='utf-8')
    print(f'\n完成：raw {len(raw)} 字符，normalized {len(text)} 字符', flush=True)
    print(f'  {EXT/(stem+"_raw.md")}\n  {NORM/(stem+".md")}', flush=True)


if __name__ == '__main__':
    main()
