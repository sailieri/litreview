"""
Module 1: PDF → Raw Text
Uses pymupdf to extract text content from PDF files.
"""

import argparse
import json
import os
from pathlib import Path

import fitz  # pymupdf


def extract_pdf(pdf_path: str, output_dir: str | None = None) -> dict:
    """Extract raw text from a PDF file.

    Args:
        pdf_path: Path to the PDF file.
        output_dir: If provided, saves the raw text to this directory.

    Returns:
        dict with {title, pages: [{page_num, text}, ...], full_text}
    """
    doc = fitz.open(pdf_path)
    result = {
        "title": Path(pdf_path).stem,
        "source": pdf_path,
        "pages": [],
        "full_text": "",
    }

    for i, page in enumerate(doc):
        text = page.get_text().strip()
        result["pages"].append({"page_num": i + 1, "text": text})
        result["full_text"] += f"\n\n--- Page {i + 1} ---\n\n{text}"

    doc.close()
    result["full_text"] = result["full_text"].strip()

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{result['title']}_raw.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result["full_text"])

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract text from PDF")
    parser.add_argument("--input", "-i", required=True, help="PDF file path")
    parser.add_argument("--output", "-o", help="Output directory")
    args = parser.parse_args()

    result = extract_pdf(args.input, args.output)
    print(json.dumps({"title": result["title"], "pages": len(result["pages"]), "chars": len(result["full_text"])}))
