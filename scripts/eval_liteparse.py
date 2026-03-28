#!/usr/bin/env python3
"""LiteParse vs PyMuPDF evaluation script for Forentis AI.

Usage:
    python scripts/eval_liteparse.py --step 0   # Subprocess overhead
    python scripts/eval_liteparse.py --step 1   # Anchor simulation (make-or-break)
    python scripts/eval_liteparse.py --step 2   # Head-to-head comparison
    python scripts/eval_liteparse.py --step all  # Run everything
"""

import argparse
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Project imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # PyMuPDF

from liteparse import LiteParse

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "docs" / "testingsamples"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output" / "liteparse_eval"

TEST_PDFS = {
    "ABCNY": SAMPLES_DIR / "ABCNY_560_0001384129.pdf",
    "3666752": SAMPLES_DIR / "3666752.pdf",
    "CMG_Inc": SAMPLES_DIR / "CMG_Inc_0001352703.pdf",
    "AWIR": SAMPLES_DIR / "AWIR-DOC.00000001.00000038.00000806.pdf",
}

NON_PDF_FILES = {
    "MSG": SAMPLES_DIR / "Vamp0000066511.msg",
    "XLSX": SAMPLES_DIR / "Vamp0000069297.xlsx",
}


def lp_to_word_tuples(text_items: list) -> list[tuple]:
    """Convert LiteParse TextItems to PyMuPDF-compatible (x0,y0,x1,y1,text,blk,ln,wrd) tuples."""
    words = []
    for i, item in enumerate(text_items):
        text = item.text.rstrip()
        if not text:
            continue
        words.append((item.x, item.y, item.x + item.width, item.y + item.height, text, 0, 0, i))
    return words


# ---------------------------------------------------------------------------
# Step 0: Install verification + subprocess overhead
# ---------------------------------------------------------------------------
def step0_subprocess_overhead():
    print("=" * 70)
    print("STEP 0: SUBPROCESS OVERHEAD TEST")
    print("=" * 70)
    results = {}

    lp = LiteParse()
    pdf = str(TEST_PDFS["AWIR"])  # 50-page, small

    # 0a: Single-page sequential timing (10 calls)
    print("\n--- 0a: 10 sequential single-page parses ---")
    times = []
    for i in range(10):
        t0 = time.time()
        lp.parse(pdf, target_pages=str(i + 1), ocr_enabled=False)
        elapsed = (time.time() - t0) * 1000
        times.append(elapsed)
        print(f"  Call {i+1}: {elapsed:.0f}ms")

    avg = sum(times) / len(times)
    first = times[0]
    rest_avg = sum(times[1:]) / len(times[1:])
    print(f"\n  First call: {first:.0f}ms")
    print(f"  Subsequent avg: {rest_avg:.0f}ms")
    print(f"  Overall avg: {avg:.0f}ms")
    results["sequential_10"] = {
        "first_ms": round(first),
        "subsequent_avg_ms": round(rest_avg),
        "overall_avg_ms": round(avg),
        "all_ms": [round(t) for t in times],
    }

    # 0b: Batch parse timing
    print("\n--- 0b: batch_parse() on 10 pages ---")
    t0 = time.time()
    result = lp.parse(pdf, target_pages="1-10", ocr_enabled=False)
    batch_time = (time.time() - t0) * 1000
    print(f"  Batch (10 pages): {batch_time:.0f}ms total, {batch_time/10:.0f}ms/page")
    print(f"  Pages returned: {len(result.pages)}")
    results["batch_10"] = {
        "total_ms": round(batch_time),
        "per_page_ms": round(batch_time / 10),
        "pages_returned": len(result.pages),
    }

    # 0c: PyMuPDF comparison
    print("\n--- 0c: PyMuPDF baseline (10 pages) ---")
    t0 = time.time()
    doc = fitz.open(pdf)
    for i in range(10):
        page = doc[i]
        _ = page.get_text("words")
        doc._forget_page(page)
    doc.close()
    mu_time = (time.time() - t0) * 1000
    print(f"  PyMuPDF (10 pages): {mu_time:.0f}ms total, {mu_time/10:.1f}ms/page")
    results["pymupdf_10"] = {"total_ms": round(mu_time), "per_page_ms": round(mu_time / 10, 1)}

    ratio = avg / (mu_time / 10) if mu_time > 0 else float("inf")
    print(f"\n  Overhead ratio: LiteParse {ratio:.0f}x slower per page (sequential)")
    batch_ratio = (batch_time / 10) / (mu_time / 10) if mu_time > 0 else float("inf")
    print(f"  Overhead ratio: LiteParse {batch_ratio:.0f}x slower per page (batch)")
    results["overhead_ratio_sequential"] = round(ratio, 1)
    results["overhead_ratio_batch"] = round(batch_ratio, 1)

    # 0d: Non-PDF support
    print("\n--- 0d: Non-PDF format support ---")
    for fmt, path in NON_PDF_FILES.items():
        try:
            r = lp.parse(str(path), ocr_enabled=False, timeout=10)
            print(f"  {fmt}: PARSED ({len(r.pages)} pages, {len(r.text)} chars)")
            results[f"non_pdf_{fmt.lower()}"] = "parsed"
        except Exception as e:
            print(f"  {fmt}: FAILED ({type(e).__name__}: {e})")
            results[f"non_pdf_{fmt.lower()}"] = f"error: {type(e).__name__}"

    _save_results("step0_results.json", results)
    return results


# ---------------------------------------------------------------------------
# Step 1: Anchor simulation (make-or-break)
# ---------------------------------------------------------------------------
def step1_anchor_simulation():
    print("=" * 70)
    print("STEP 1: ANCHOR SIMULATION (MAKE-OR-BREAK)")
    print("=" * 70)

    from app.pipeline.field_map_builder import FieldMapBuilder

    results = {"tests": []}
    lp = LiteParse()

    test_cases = [
        # (pdf_key, page_num_0indexed, lp_page_1indexed, anchors_to_test, description)
        (
            "CMG_Inc", 1, "2",
            [
                ("Name", "PERSON label"),
                ("Birth:", "DOB label"),
                ("address", "ADDRESS label"),
                ("National Insurance Number:", "NI_NUMBER multi-word"),
                ("NE724362D", "NI value"),
                ("10-Aug-1959", "DOB value"),
                ("Acheampong", "PERSON value"),
            ],
            "CMG page 1 (rot=0, pension statement)",
        ),
        (
            "AWIR", 2, "3",
            [
                ("Account:", "Account anchor"),
                ("Total:", "Total anchor"),
                ("REPORT", "Header anchor"),
                ("DATE:", "Date label"),
                ("01/20/2011", "Date value"),
            ],
            "AWIR page 2 (rot=90, shareholder list)",
        ),
        (
            "3666752", 0, "1",
            [
                ("ABRAHAM,", "PERSON name (partial)"),
                ("Grade", "Grade label"),
                ("Date", "Date label"),
            ],
            "3666752 page 0 (rot=0, student grades)",
        ),
    ]

    all_pass = True
    for pdf_key, mu_page, lp_page_str, anchors, desc in test_cases:
        pdf_path = str(TEST_PDFS[pdf_key])
        print(f"\n--- {desc} ---")

        # PyMuPDF
        doc = fitz.open(pdf_path)
        page = doc[mu_page]
        mu_words = list(page.get_text("words"))
        rotation = page.rotation
        doc.close()

        # LiteParse
        r = lp.parse(pdf_path, target_pages=lp_page_str, precise_bounding_box=True, ocr_enabled=False)
        lp_words = lp_to_word_tuples(r.pages[0].textItems)

        print(f"  PyMuPDF: {len(mu_words)} words, rot={rotation}")
        print(f"  LiteParse: {len(lp_words)} words (after filter)")
        print()

        case_results = {"description": desc, "rotation": rotation, "anchors": []}
        for anchor_text, label in anchors:
            mu_result = FieldMapBuilder._find_text_in_words(mu_words, anchor_text)
            lp_result = FieldMapBuilder._find_text_in_words(lp_words, anchor_text)

            mu_found = mu_result is not None
            lp_found = lp_result is not None
            both_match = mu_found == lp_found

            mu_text = " ".join([w[4] for w in mu_result]) if mu_result else "NOT FOUND"
            lp_text = " ".join([w[4] for w in lp_result]) if lp_result else "NOT FOUND"

            status = "PASS" if both_match else "FAIL"
            if not both_match:
                all_pass = False

            print(f"  {label} (\"{anchor_text}\"): {status}")
            print(f"    PyMuPDF:   {mu_text}")
            print(f"    LiteParse: {lp_text}")

            case_results["anchors"].append({
                "anchor": anchor_text,
                "label": label,
                "pymupdf_found": mu_found,
                "liteparse_found": lp_found,
                "status": status,
            })

        results["tests"].append(case_results)

    # Spatial relationship test on CMG
    print("\n--- Spatial Relationship Resolution ---")
    pdf_path = str(TEST_PDFS["CMG_Inc"])
    doc = fitz.open(pdf_path)
    mu_words = list(doc[1].get_text("words"))
    doc.close()

    r = lp.parse(pdf_path, target_pages="2", precise_bounding_box=True, ocr_enabled=False)
    lp_words = lp_to_word_tuples(r.pages[0].textItems)

    spatial_tests = [
        ("Birth:", "10-Aug-1959", "DOB"),
        ("address", "85", "ADDRESS"),
        ("Name", "Mr", "PERSON"),
    ]

    spatial_results = []
    for anchor, value, field_type in spatial_tests:
        mu_a = FieldMapBuilder._find_text_in_words(mu_words, anchor)
        mu_v = FieldMapBuilder._find_text_in_words(mu_words, value)
        lp_a = FieldMapBuilder._find_text_in_words(lp_words, anchor)
        lp_v = FieldMapBuilder._find_text_in_words(lp_words, value)

        mu_rel = FieldMapBuilder._compute_spatial_relationship(mu_a, mu_v) if mu_a and mu_v else None
        lp_rel = FieldMapBuilder._compute_spatial_relationship(lp_a, lp_v) if lp_a and lp_v else None

        # Compare just the relationship type (not the line count which varies by ~pixels)
        mu_type = mu_rel[0].split("_")[0] if mu_rel else None
        lp_type = lp_rel[0].split("_")[0] if lp_rel else None
        match = mu_type == lp_type

        print(f"  \"{anchor}\" → \"{value}\": PyMuPDF={mu_rel}, LiteParse={lp_rel} → {'PASS' if match else 'FAIL'}")
        spatial_results.append({
            "anchor": anchor, "value": value,
            "pymupdf": str(mu_rel), "liteparse": str(lp_rel),
            "type_match": match,
        })
        if not match:
            all_pass = False

    results["spatial_tests"] = spatial_results
    results["all_pass"] = all_pass

    print(f"\n{'='*70}")
    print(f"STEP 1 VERDICT: {'PASS — Hybrid is viable' if all_pass else 'FAIL — Hybrid for coordinates is blocked'}")
    print(f"{'='*70}")

    _save_results("step1_results.json", results)
    return results


# ---------------------------------------------------------------------------
# Step 2: Head-to-head comparison
# ---------------------------------------------------------------------------
def step2_comparison():
    print("=" * 70)
    print("STEP 2: HEAD-TO-HEAD COMPARISON")
    print("=" * 70)

    lp = LiteParse()
    results = {}

    for pdf_key, pdf_path in TEST_PDFS.items():
        print(f"\n--- {pdf_key} ({pdf_path.name}) ---")
        pdf_results = {}

        # 2.1 & 2.2: Text accuracy + textItem granularity
        doc = fitz.open(str(pdf_path))
        total_pages = doc.page_count

        # Test pages 0-2
        test_pages = list(range(min(3, total_pages)))
        lp_target = ",".join(str(p + 1) for p in test_pages)

        try:
            lp_result = lp.parse(str(pdf_path), target_pages=lp_target, precise_bounding_box=True, ocr_enabled=True, timeout=120)
        except Exception as e:
            print(f"  LiteParse FAILED: {e}")
            pdf_results["error"] = str(e)
            results[pdf_key] = pdf_results
            doc.close()
            continue

        page_comparisons = []
        for i, mu_page_idx in enumerate(test_pages):
            mu_page = doc[mu_page_idx]
            mu_words = list(mu_page.get_text("words"))
            mu_text = mu_page.get_text()
            mu_word_set = {w[4].lower().strip(".,;:!?") for w in mu_words if len(w[4].strip()) > 0}

            if i < len(lp_result.pages):
                lp_page = lp_result.pages[i]
                lp_word_tuples = lp_to_word_tuples(lp_page.textItems)
                lp_word_set = {w[4].lower().strip(".,;:!?") for w in lp_word_tuples if len(w[4].strip()) > 0}

                # Categorize textItems
                single_word = sum(1 for t in lp_page.textItems if len(t.text.split()) <= 1 and t.text.strip())
                multi_word = sum(1 for t in lp_page.textItems if len(t.text.split()) > 1)
                whitespace_only = sum(1 for t in lp_page.textItems if not t.text.strip())

                overlap = mu_word_set & lp_word_set
                only_mu = mu_word_set - lp_word_set
                only_lp = lp_word_set - mu_word_set
                overlap_pct = len(overlap) / len(mu_word_set) * 100 if mu_word_set else 0

                comp = {
                    "page": mu_page_idx,
                    "pymupdf_words": len(mu_words),
                    "liteparse_items": len(lp_page.textItems),
                    "liteparse_words": len(lp_word_tuples),
                    "single_word_items": single_word,
                    "multi_word_items": multi_word,
                    "whitespace_items": whitespace_only,
                    "unique_words_pymupdf": len(mu_word_set),
                    "unique_words_liteparse": len(lp_word_set),
                    "overlap_words": len(overlap),
                    "only_pymupdf": len(only_mu),
                    "only_liteparse": len(only_lp),
                    "overlap_pct": round(overlap_pct, 1),
                    "rotation": mu_page.rotation,
                }
                page_comparisons.append(comp)

                print(f"  Page {mu_page_idx}: MuPDF={len(mu_words)} words, LP={len(lp_word_tuples)} words, "
                      f"overlap={overlap_pct:.0f}%, rot={mu_page.rotation}")
                if only_mu and len(only_mu) < 10:
                    print(f"    Only in PyMuPDF: {sorted(only_mu)[:10]}")
                if only_lp and len(only_lp) < 10:
                    print(f"    Only in LiteParse: {sorted(only_lp)[:10]}")
            else:
                print(f"  Page {mu_page_idx}: LiteParse returned no data")

            doc._forget_page(mu_page)

        pdf_results["page_comparisons"] = page_comparisons

        # 2.5: Speed comparison (all pages)
        print(f"\n  Speed test ({total_pages} pages)...")

        # PyMuPDF all pages
        t0 = time.time()
        for pn in range(total_pages):
            p = doc[pn]
            _ = p.get_text("words")
            doc._forget_page(p)
        mu_total = (time.time() - t0) * 1000
        doc.close()

        # LiteParse all pages (with timeout for large docs)
        timeout = max(300, total_pages * 2)
        t0 = time.time()
        try:
            lp_all = lp.parse(str(pdf_path), precise_bounding_box=True, ocr_enabled=False, timeout=timeout)
            lp_total = (time.time() - t0) * 1000
            lp_page_count = len(lp_all.pages)
        except Exception as e:
            lp_total = (time.time() - t0) * 1000
            lp_page_count = 0
            print(f"  LiteParse full parse FAILED after {lp_total:.0f}ms: {e}")

        mu_per_page = mu_total / total_pages
        lp_per_page = lp_total / total_pages if lp_page_count > 0 else float("inf")
        ratio = lp_per_page / mu_per_page if mu_per_page > 0 else float("inf")

        print(f"  PyMuPDF:   {mu_total:.0f}ms total ({mu_per_page:.1f}ms/page)")
        print(f"  LiteParse: {lp_total:.0f}ms total ({lp_per_page:.1f}ms/page) [{lp_page_count} pages]")
        print(f"  Ratio: LiteParse {ratio:.1f}x {'slower' if ratio > 1 else 'faster'}")

        pdf_results["speed"] = {
            "total_pages": total_pages,
            "pymupdf_total_ms": round(mu_total),
            "pymupdf_per_page_ms": round(mu_per_page, 1),
            "liteparse_total_ms": round(lp_total),
            "liteparse_per_page_ms": round(lp_per_page, 1),
            "liteparse_pages_returned": lp_page_count,
            "ratio": round(ratio, 1),
        }

        # 2.3: Layout preservation (capture spatial text for manual review)
        if lp_result.pages:
            spatial_sample = lp_result.pages[0].text[:1000]
            pdf_results["spatial_text_sample"] = spatial_sample

        results[pdf_key] = pdf_results

    _save_results("step2_results.json", results)
    return results


# ---------------------------------------------------------------------------
# Step 2.7: Spatial text → LLM test
# ---------------------------------------------------------------------------
def step2_7_spatial_text_llm():
    """Test if LLM can identify PII from spatial text alone (no vision model needed)."""
    print("=" * 70)
    print("STEP 2.7: SPATIAL TEXT → LLM (VISION BOTTLENECK BYPASS)")
    print("=" * 70)

    lp = LiteParse()
    results = {}

    # Test on CMG page 1 (pension statement with clear PII)
    test_cases = [
        ("CMG_Inc", "2", "CMG page 1 — pension statement"),
        ("AWIR", "3", "AWIR page 2 — shareholder list (rotated)"),
        ("3666752", "1", "3666752 page 0 — student grade report"),
    ]

    for pdf_key, lp_page, desc in test_cases:
        pdf_path = str(TEST_PDFS[pdf_key])
        print(f"\n--- {desc} ---")

        r = lp.parse(pdf_path, target_pages=lp_page, precise_bounding_box=True, ocr_enabled=True, timeout=60)
        if not r.pages:
            print("  No pages returned")
            continue

        spatial_text = r.pages[0].text

        # Build the prompt that would replace vision routing
        prompt = f"""Analyze this document page and identify all PII (personally identifiable information) fields present.

For each PII field found, provide:
- field_type: one of PERSON, DOB, US_SSN, NI_NUMBER, EMAIL, PHONE, LOCATION, GOVERNMENT_ID
- label: the label text next to the field (e.g., "Date of Birth:", "Name:")
- value: the actual PII value found

Document page content (spatial layout preserved):
---
{spatial_text[:2000]}
---

Respond as JSON array: [{{"field_type": "...", "label": "...", "value": "..."}}]"""

        print(f"  Spatial text length: {len(spatial_text)} chars")
        print(f"  Prompt length: {len(prompt)} chars")
        print(f"\n  Spatial text preview (first 500 chars):")
        for line in spatial_text[:500].split("\n"):
            if line.strip():
                print(f"    {line}")

        # Try to call Ollama if available
        try:
            from app.llm.client import OllamaClient
            client = OllamaClient()
            t0 = time.time()
            response = client.generate(prompt, timeout=120)
            llm_time = (time.time() - t0) * 1000
            print(f"\n  LLM response ({llm_time:.0f}ms):")
            print(f"  {response[:500]}")
            results[pdf_key] = {
                "desc": desc,
                "spatial_text_len": len(spatial_text),
                "prompt_len": len(prompt),
                "llm_response": response[:1000],
                "llm_time_ms": round(llm_time),
            }
        except Exception as e:
            print(f"\n  LLM not available: {e}")
            print("  Saving prompt for manual testing.")
            results[pdf_key] = {
                "desc": desc,
                "spatial_text_len": len(spatial_text),
                "prompt_len": len(prompt),
                "prompt_saved": True,
                "error": str(e),
            }
            # Save prompt for manual testing
            prompt_file = OUTPUT_DIR / f"llm_prompt_{pdf_key}.txt"
            prompt_file.write_text(prompt)
            print(f"  Prompt saved to: {prompt_file}")

    _save_results("step2_7_results.json", results)
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _save_results(filename: str, data: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"\n  Results saved to: {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="LiteParse vs PyMuPDF evaluation")
    parser.add_argument("--step", default="all", help="Step to run: 0, 1, 2, 2.7, all")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.step in ("0", "all"):
        step0_subprocess_overhead()
    if args.step in ("1", "all"):
        step1_anchor_simulation()
    if args.step in ("2", "all"):
        step2_comparison()
    if args.step in ("2.7", "all"):
        step2_7_spatial_text_llm()

    if args.step == "all":
        print("\n" + "=" * 70)
        print("ALL STEPS COMPLETE. Results in: output/liteparse_eval/")
        print("=" * 70)


if __name__ == "__main__":
    main()
