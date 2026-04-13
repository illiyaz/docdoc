#!/usr/bin/env python3
"""Test Step 37b: Repeating unit detection + context marker extraction.

Standalone script — no pipeline needed. Tests the full flow:
1. Sample 9 pages from the PDF
2. Ask LLM to describe the repeating structure + context markers
3. Use those markers to extract from test pages
4. Compare against ground truth

Usage:
    python scripts/test_repeating_unit.py --pdf file.pdf
    python scripts/test_repeating_unit.py --pdf file.pdf --model qwen2.5:14b
    python scripts/test_repeating_unit.py  # runs on all phase2 files
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PDF_DIR = "docs/testingsamples/phase2_100pg"

# ──────────────────────────────────────────────────────────────
# Step 1: Sample pages
# ──────────────────────────────────────────────────────────────

def sample_pages(pdf_path: str, onset: int = 0) -> dict[str, list[int]]:
    """Pick 9 pages: 3 from onset, 3 from middle, 3 from end."""
    import fitz
    doc = fitz.open(pdf_path)
    total = doc.page_count
    doc.close()

    if total < 9:
        return {"onset": list(range(total)), "middle": [], "end": []}

    mid = total // 2
    end = total - 3

    return {
        "onset": [onset, min(onset + 1, total - 1), min(onset + 2, total - 1)],
        "middle": [mid, min(mid + 1, total - 1), min(mid + 2, total - 1)],
        "end": [max(end, 0), max(end + 1, 0), min(end + 2, total - 1)],
    }


def read_pages(pdf_path: str, page_nums: list[int]) -> dict[int, str]:
    """Read text from specific pages. Memory safe."""
    import fitz
    doc = fitz.open(pdf_path)
    texts = {}
    for pg in page_nums:
        if 0 <= pg < doc.page_count:
            texts[pg] = doc[pg].get_text()[:3000]
            doc._forget_page(pg)
    doc.close()
    return texts


# ──────────────────────────────────────────────────────────────
# Step 2: Detect repeating unit + context markers
# ──────────────────────────────────────────────────────────────

DETECT_PROMPT = """Here are 9 pages from a document — 3 from the beginning, 3 from the middle, 3 from the end.

{pages_text}

Analyze the REPEATING STRUCTURE of this document:

1. What represents ONE person's complete record?
   (a full page, a block between separators, a table row, multiple pages)

2. How do records separate?
   (page break, dashed line, blank lines, header repeat, table row boundary)

3. How many distinct person records appear on the MIDDLE pages?

4. Do any records CONTINUE across page breaks?

5. CONTEXT MARKERS — identify the fixed text that appears BEFORE and AFTER
   the primary subject's NAME and ADDRESS on each page/record:
   - What text/label appears immediately BEFORE the person's name?
   - What text/label appears immediately AFTER the person's name?
   - What text/label appears immediately BEFORE the address?
   - What text/label appears immediately AFTER the address?

Return ONLY this JSON:
{{
  "record_unit": "page | block | row | multi_page",
  "separator": "page_break | dashed_line | blank_lines | header_repeat | table_row | none",
  "separator_pattern": "exact text of separator if applicable, or null",
  "records_per_page": 1,
  "has_continuation": false,
  "continuation_marker": null,
  "context_markers": {{
    "name_after": "text/label that appears BEFORE the person name",
    "name_before": "text/label that appears AFTER the person name",
    "address_after": "text/label that appears BEFORE the address",
    "address_before": "text/label that appears AFTER the address"
  }},
  "sample_name": "an actual person name you found on these pages",
  "sample_address": "their actual address"
}}"""


def detect_repeating_unit(pdf_path: str, model: str) -> dict:
    """Run repeating unit detection on a PDF."""
    from app.llm.client import OllamaClient

    samples = sample_pages(pdf_path)
    all_pages = samples["onset"] + samples["middle"] + samples["end"]
    all_pages = sorted(set(all_pages))

    texts = read_pages(pdf_path, all_pages)

    pages_text = ""
    for section, pages in samples.items():
        pages_text += f"\n=== {section.upper()} PAGES ===\n"
        for pg in pages:
            if pg in texts:
                pages_text += f"\n--- PAGE {pg + 1} ---\n{texts[pg]}\n"

    prompt = DETECT_PROMPT.format(pages_text=pages_text)

    client = OllamaClient(model=model, timeout_s=180)
    t0 = time.time()
    response = client.generate(
        prompt=prompt,
        system="You are a document structure analyst. Return only JSON.",
        use_case="repeating_unit_detection",
    )
    elapsed = time.time() - t0

    # Parse JSON
    cleaned = response.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
            except:
                result = {"error": "JSON parse failed", "raw": cleaned[:500]}
        else:
            result = {"error": "No JSON found", "raw": cleaned[:500]}

    result["detection_time"] = round(elapsed, 1)
    return result


# ──────────────────────────────────────────────────────────────
# Step 3: Extract using context markers
# ──────────────────────────────────────────────────────────────

def extract_with_markers(pdf_path: str, model: str, detection: dict, test_pages: list[int]) -> list[dict]:
    """Extract subjects from test pages using context markers."""
    from app.llm.client import OllamaClient

    markers = detection.get("context_markers", {})
    name_after = markers.get("name_after", "")
    name_before = markers.get("name_before", "")
    record_unit = detection.get("record_unit", "page")
    records_per_page = detection.get("records_per_page", 1)
    separator = detection.get("separator_pattern", "")

    texts = read_pages(pdf_path, test_pages)

    pages_text = ""
    for pg in test_pages:
        if pg in texts:
            pages_text += f"\n--- PAGE {pg + 1} ---\n{texts[pg]}\n"

    if record_unit == "block" and records_per_page > 1:
        multi_hint = (
            f"This page has MULTIPLE person records ({records_per_page} per page), "
            f"separated by: {separator or 'unknown separator'}.\n"
            f"Extract ALL persons on each page.\n"
        )
    else:
        multi_hint = ""

    if name_after and name_before:
        marker_hint = (
            f"IMPORTANT: The person's name appears AFTER '{name_after}' "
            f"and BEFORE '{name_before}'. "
            f"Focus on that area — ignore everything else on the page.\n"
        )
    else:
        marker_hint = ""

    prompt = (
        f"Extract personal information from these {len(test_pages)} pages.\n\n"
        f"{multi_hint}"
        f"{marker_hint}"
        f"For each person found, return:\n"
        f'[{{"page": 1, "name": "Full Name", "address": "Street Address, City, ST ZIP"}}]\n\n'
        f"Rules:\n"
        f"- Extract the PRIMARY SUBJECT only (not staff, providers, institutional names)\n"
        f"- Address must be a real street address with a number\n"
        f"- Return ONLY JSON array\n\n"
        f"{pages_text}"
    )

    client = OllamaClient(model=model, timeout_s=180)
    t0 = time.time()
    try:
        response = client.generate(
            prompt=prompt,
            system="You are a data extraction assistant. Return only JSON.",
            use_case="marker_extraction",
        )
    except Exception as e:
        return [{"error": str(e)}]

    elapsed = time.time() - t0

    # Parse
    cleaned = response.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        data = json.loads(cleaned)
    except:
        match = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except:
                return [{"error": "parse failed", "time": elapsed}]
        else:
            return [{"error": "no JSON", "time": elapsed}]

    if not isinstance(data, list):
        data = [data]

    results = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("name"):
            results.append({
                "page": entry.get("page"),
                "name": entry.get("name", ""),
                "address": entry.get("address", ""),
            })

    return results


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def test_one_pdf(pdf_path: str, model: str):
    fname = os.path.basename(pdf_path)
    print(f"\n{'='*60}")
    print(f"FILE: {fname}")
    print(f"MODEL: {model}")
    print(f"{'='*60}")

    # Step 1: Detect
    print("\n--- Step 1: Repeating Unit Detection ---")
    detection = detect_repeating_unit(pdf_path, model)

    if detection.get("error"):
        print(f"  ERROR: {detection['error']}")
        if detection.get("raw"):
            print(f"  Raw: {detection['raw'][:200]}")
        return detection

    print(f"  record_unit: {detection.get('record_unit')}")
    print(f"  separator: {detection.get('separator')}")
    print(f"  records_per_page: {detection.get('records_per_page')}")
    print(f"  has_continuation: {detection.get('has_continuation')}")
    markers = detection.get("context_markers", {})
    print(f"  name_after: '{markers.get('name_after', '')}'")
    print(f"  name_before: '{markers.get('name_before', '')}'")
    print(f"  sample_name: {detection.get('sample_name')}")
    print(f"  sample_address: {detection.get('sample_address')}")
    print(f"  detection_time: {detection.get('detection_time')}s")

    # Step 2: Extract with markers on 5 test pages
    print("\n--- Step 2: Extract with Context Markers ---")
    import fitz
    doc = fitz.open(pdf_path)
    total = doc.page_count
    doc.close()

    # Pick 5 evenly spaced content pages
    step = max(1, total // 6)
    test_pages = [i * step for i in range(5) if i * step < total]

    t0 = time.time()
    records = extract_with_markers(pdf_path, model, detection, test_pages)
    extract_time = time.time() - t0

    print(f"  Test pages: {[p+1 for p in test_pages]}")
    print(f"  Records extracted: {len(records)}")
    print(f"  Extract time: {extract_time:.1f}s")

    for rec in records[:10]:
        if rec.get("error"):
            print(f"    ERROR: {rec['error']}")
        else:
            print(f"    Page {rec.get('page','?')}: {rec.get('name','')[:30]} | {rec.get('address','')[:35]}")

    # Return summary
    return {
        "file": fname,
        "model": model,
        "detection": detection,
        "records": len(records),
        "extract_time": round(extract_time, 1),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", default=None, help="Single PDF to test")
    parser.add_argument("--model", default="qwen2.5:7b", help="Model to use")
    args = parser.parse_args()

    if args.pdf:
        files = [args.pdf]
    else:
        files = sorted(str(f) for f in Path(PDF_DIR).glob("*.pdf"))

    results = []
    for pdf in files:
        if not os.path.exists(pdf):
            print(f"Not found: {pdf}")
            continue
        try:
            result = test_one_pdf(pdf, args.model)
            results.append(result)
        except Exception as e:
            print(f"  FAILED: {e}")
            results.append({"file": os.path.basename(pdf), "error": str(e)})

    # Save
    os.makedirs("output", exist_ok=True)
    with open("output/repeating_unit_test.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to output/repeating_unit_test.json")


if __name__ == "__main__":
    main()
