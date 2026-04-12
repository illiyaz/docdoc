#!/usr/bin/env python3
"""Run full extraction pipeline on a PDF and report results.

Bypasses UI — calls the extraction functions directly, swapping the
LLM model. Produces a JSON report with subject count, accuracy, timing.

Usage:
    python scripts/run_full_pipeline_test.py --pdf file.pdf --model qwen2.5:14b
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def run_test(pdf_path: str, model: str, output_path: str):
    import fitz
    from app.llm.client import OllamaClient
    from app.pipeline.text_batch_extractor import extract_text_batch
    from app.rra.entity_resolver import EntityResolver
    from app.rra.deduplicator import Deduplicator

    fname = os.path.basename(pdf_path)
    print(f"  File: {fname}")
    print(f"  Model: {model}")

    t0 = time.time()

    # --- Read all pages ---
    doc = fitz.open(pdf_path)
    total_pages = doc.page_count
    page_texts = {}
    for pg in range(total_pages):
        text = doc[pg].get_text()
        if text.strip():
            page_texts[pg] = text
    doc.close()

    content_pages = len(page_texts)
    print(f"  Pages: {total_pages} total, {content_pages} with text")

    t_read = time.time() - t0

    # --- Extract ---
    t_ext_start = time.time()
    client = OllamaClient(model=model, timeout_s=180)

    records = extract_text_batch(
        page_texts=page_texts,
        ollama_client=client,
        doc_id="test",
        document_type="unknown",
        field_inventory=["PERSON", "LOCATION"],
        pages_per_batch=3,
    )
    t_ext = time.time() - t_ext_start

    primary = [r for r in records if r.entity_role == "primary_subject"]
    guardians = [r for r in records if r.entity_role == "guardian"]
    with_addr = sum(1 for r in primary if r.raw_address)

    print(f"  Extracted: {len(records)} total ({len(primary)} primary, {len(guardians)} guardians)")
    print(f"  With address: {with_addr}")
    print(f"  Extraction time: {t_ext:.1f}s")

    # --- Entity Resolution ---
    t_res_start = time.time()
    resolver = EntityResolver()
    groups = resolver.resolve(primary)
    t_res = time.time() - t_res_start

    print(f"  Groups: {len(groups)}")

    # --- Count subjects (simulated dedup) ---
    # Simple: count groups with at least a name
    subjects = sum(1 for g in groups if g.records and g.records[0].raw_name)
    subjects_with_addr = sum(
        1 for g in groups
        if g.records and g.records[0].raw_name and g.records[0].raw_address
    )

    total_time = time.time() - t0

    print(f"  Subjects: {subjects} ({subjects_with_addr} with address)")
    print(f"  Total time: {total_time:.1f}s")

    # --- Ground truth comparison (for Meadowdale files) ---
    gt_count = 0
    gt_matched = 0
    if "3733050" in fname or "3738594" in fname or "3738641" in fname:
        doc = fitz.open(pdf_path)
        gt_students = []
        for pg in range(doc.page_count):
            text = doc[pg].get_text()
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if len(lines) >= 8 and "Meadowdale" in lines[0]:
                gt_students.append(lines[6])
        doc.close()
        gt_count = len(gt_students)

        extracted_lasts = {r.raw_name.split()[-1].lower() for r in primary if r.raw_name}
        for s in gt_students:
            last = s.split()[-1].lower()
            if last in extracted_lasts:
                gt_matched += 1

        accuracy = 100 * gt_matched / gt_count if gt_count else 0
        print(f"  Ground truth: {gt_matched}/{gt_count} ({accuracy:.0f}%)")

    # --- Save results ---
    result = {
        "file": fname,
        "model": model,
        "total_pages": total_pages,
        "content_pages": content_pages,
        "records_extracted": len(records),
        "primary_subjects": len(primary),
        "guardians": len(guardians),
        "subjects": subjects,
        "subjects_with_address": subjects_with_addr,
        "ground_truth_total": gt_count,
        "ground_truth_matched": gt_matched,
        "ground_truth_accuracy": f"{100*gt_matched/gt_count:.0f}%" if gt_count else "n/a",
        "read_time": round(t_read, 1),
        "extraction_time": round(t_ext, 1),
        "resolution_time": round(t_res, 1),
        "total_time": round(total_time, 1),
    }

    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"  Saved: {output_path}")

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if not os.path.exists(args.pdf):
        print(f"PDF not found: {args.pdf}")
        sys.exit(1)

    run_test(args.pdf, args.model, args.output)


if __name__ == "__main__":
    main()
