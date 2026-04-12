#!/usr/bin/env python3
"""Test extraction accuracy across different LLM models.

Runs the same 5-page extraction on multiple models and compares results
against ground truth.

Usage:
    python scripts/test_extraction_models.py
    python scripts/test_extraction_models.py --models qwen2.5:7b,qwen2.5:14b,llama3:8b
    python scripts/test_extraction_models.py --pages 10
"""
import sys, os, json, time, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Ground truth for 3733050.pdf (Meadowdale school report)
PDF_PATH = "uploads/b55fc19d-219d-4a6a-8539-f633264567a4/3733050.pdf"
ALT_PDF = "docs/testingsamples/phase2_large_pdfs_mini/3733050.pdf"

# Llama-safe prompt (avoids "PII", "extract", "breach" — words that trigger refusal)
LLAMA_PROMPT = (
    "Read these {n} pages from a school grade report document.\n\n"
    "For EACH page, find the STUDENT whose grades are being reported.\n"
    "The student's name is listed AFTER the parent names and BEFORE the address.\n"
    "The structure on each page is:\n"
    "  - School header (Meadowdale High School, address, phone)\n"
    "  - Parent/guardian names\n"
    "  - STUDENT NAME (this is who you want)\n"
    "  - Student's home address (street, city, state, zip)\n"
    "  - Grade information and teacher names (IGNORE these)\n\n"
    "Return a JSON array with one object per page:\n"
    '[{{"page": 1, "student_name": "First Last", "parent_name": "Parent Names", '
    '"home_address": "123 Street, City, ST 12345"}}]\n\n'
    "Rules:\n"
    "- student_name: The student, NOT a teacher or parent\n"
    "- home_address: A street address with a number. NOT the school address.\n"
    "  The school address (6002 168th St SW) appears on EVERY page — ignore it.\n"
    "- parent_name: The parent/guardian listed above the student name\n"
    "- If a page has no student data, omit it from the array\n"
    "- Return ONLY the JSON array, no other text\n\n"
    "{pages_text}"
)

# Generic prompt (works with qwen, gemma, phi)
GENERIC_PROMPT = (
    "Extract personal information from these {n} pages of a school grade report.\n\n"
    "For EACH page, extract ONLY the PRIMARY SUBJECT — the STUDENT whose grades "
    "are shown. NOT the teachers, NOT the school staff.\n\n"
    "Also extract the student's PARENT/GUARDIAN name and HOME ADDRESS.\n\n"
    "Return a JSON array:\n"
    '[{{"page": 1, "student_name": "First Last", "parent_name": "Parent Names", '
    '"home_address": "123 Street, City, ST 12345"}}]\n\n'
    "CRITICAL:\n"
    "- student_name: The student (listed after parents, before address)\n"
    "- home_address: Must be a STREET address with a number, NOT the school address\n"
    "- Ignore ALL teacher names (they appear in the grades section)\n"
    "- Ignore the school phone number and school address\n"
    "- Return ONLY JSON\n\n"
    "{pages_text}"
)

# Model-specific prompt selection
MODEL_PROMPTS = {
    "llama3:8b": LLAMA_PROMPT,
    "llama3:latest": LLAMA_PROMPT,
    "llama3.2:latest": LLAMA_PROMPT,
}


def get_ground_truth(pdf_path, pages):
    """Get ground truth student data for specific pages."""
    import fitz
    doc = fitz.open(pdf_path)
    truth = {}
    for pg in pages:
        if pg >= doc.page_count:
            continue
        text = doc[pg].get_text()
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) >= 8 and "Meadowdale" in lines[0]:
            truth[pg] = {
                "student": lines[6],
                "parent": lines[5],
                "address": lines[7],
            }
    doc.close()
    return truth


def run_model(model_name, pdf_path, pages):
    """Run extraction with a specific model."""
    import fitz
    from app.llm.client import OllamaClient

    doc = fitz.open(pdf_path)
    pages_text = ""
    for pg in pages:
        if pg < doc.page_count:
            text = doc[pg].get_text()[:3000]
            pages_text += f"\n--- PAGE {pg + 1} ---\n{text}\n"
    doc.close()

    # Select prompt
    prompt_template = MODEL_PROMPTS.get(model_name, GENERIC_PROMPT)
    prompt = prompt_template.format(n=len(pages), pages_text=pages_text)

    client = OllamaClient(model=model_name, timeout_s=180)

    t0 = time.time()
    try:
        response = client.generate(
            prompt=prompt,
            system="You are a data transcription assistant. Return only JSON.",
            use_case="model_comparison",
        )
        elapsed = time.time() - t0
    except Exception as e:
        return {"error": str(e), "time": time.time() - t0, "records": []}

    # Parse response
    import re
    cleaned = response.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except:
                return {"error": "JSON parse failed", "time": elapsed, "records": [], "raw": cleaned[:500]}
        else:
            return {"error": "No JSON found", "time": elapsed, "records": [], "raw": cleaned[:500]}

    if not isinstance(data, list):
        data = [data]

    records = []
    for entry in data:
        if isinstance(entry, dict):
            records.append({
                "page": entry.get("page"),
                "student": entry.get("student_name") or entry.get("name") or "",
                "parent": entry.get("parent_name") or entry.get("parent_or_guardian") or "",
                "address": entry.get("home_address") or entry.get("address") or "",
            })

    return {"time": elapsed, "records": records, "error": None}


def compare_results(truth, result, model_name):
    """Compare model output against ground truth."""
    records = result.get("records", [])

    correct_student = 0
    correct_address = 0
    wrong_student = 0
    missing = 0

    matched_pages = set()
    for rec in records:
        pg = rec.get("page")
        if pg is None:
            continue
        pg_0 = pg - 1  # convert to 0-indexed

        if pg_0 in truth:
            matched_pages.add(pg_0)
            gt = truth[pg_0]

            # Check student name (last name match)
            gt_last = gt["student"].split()[-1].lower()
            rec_last = rec["student"].split()[-1].lower() if rec["student"] else ""

            if gt_last == rec_last:
                correct_student += 1
            else:
                wrong_student += 1

            # Check address (street number match)
            gt_addr_num = gt["address"].split()[0] if gt["address"] else ""
            rec_addr_num = rec["address"].split()[0] if rec["address"] else ""

            if gt_addr_num and rec_addr_num and gt_addr_num == rec_addr_num:
                correct_address += 1

    missing = len(truth) - len(matched_pages)
    total = len(truth)

    return {
        "model": model_name,
        "total_pages": total,
        "correct_student": correct_student,
        "correct_address": correct_address,
        "wrong_student": wrong_student,
        "missing": missing,
        "student_accuracy": f"{100*correct_student/total:.0f}%" if total else "n/a",
        "address_accuracy": f"{100*correct_address/total:.0f}%" if total else "n/a",
        "time": f"{result['time']:.1f}s",
        "error": result.get("error"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="qwen2.5:7b,llama3:8b",
                       help="Comma-separated models to test")
    parser.add_argument("--pages", type=int, default=5, help="Number of pages to test")
    parser.add_argument("--pdf", default=None, help="Path to PDF file to test")
    args = parser.parse_args()

    # Find PDF
    if args.pdf and os.path.exists(args.pdf):
        pdf_path = args.pdf
    elif os.path.exists(PDF_PATH):
        pdf_path = PDF_PATH
    elif os.path.exists(ALT_PDF):
        pdf_path = ALT_PDF
    else:
        print(f"PDF not found. Use --pdf to specify path.")
        sys.exit(1)

    models = [m.strip() for m in args.models.split(",")]
    test_pages = list(range(0, args.pages * 2, 2))[:args.pages]  # even pages only (content pages)

    print(f"{'='*70}")
    print(f"EXTRACTION MODEL COMPARISON")
    print(f"{'='*70}")
    print(f"PDF: {os.path.basename(pdf_path)}")
    print(f"Pages: {[p+1 for p in test_pages]} ({len(test_pages)} pages)")
    print(f"Models: {models}")

    # Ground truth
    truth = get_ground_truth(pdf_path, test_pages)
    print(f"\nGround truth ({len(truth)} students):")
    for pg, gt in sorted(truth.items()):
        print(f"  Page {pg+1}: {gt['student']:25} addr={gt['address'][:35]}")

    # Run each model
    results = []
    for model in models:
        print(f"\n{'─'*50}")
        print(f"Testing: {model}")
        print(f"{'─'*50}")

        result = run_model(model, pdf_path, test_pages)

        if result.get("error"):
            print(f"  ERROR: {result['error']}")
            if result.get("raw"):
                print(f"  Raw: {result['raw'][:200]}")

        print(f"  Time: {result['time']:.1f}s")
        print(f"  Records: {len(result.get('records', []))}")
        for rec in result.get("records", []):
            print(f"    Page {rec['page']}: student={rec['student'][:25]} addr={rec['address'][:35]}")

        comparison = compare_results(truth, result, model)
        results.append(comparison)

        print(f"\n  Student accuracy: {comparison['student_accuracy']}")
        print(f"  Address accuracy: {comparison['address_accuracy']}")
        print(f"  Wrong students: {comparison['wrong_student']}")
        print(f"  Missing pages: {comparison['missing']}")

    # Summary table
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"{'Model':<25} {'Students':>10} {'Addresses':>10} {'Wrong':>7} {'Miss':>6} {'Time':>8}")
    print("─" * 70)
    for r in results:
        print(f"{r['model']:<25} {r['student_accuracy']:>10} {r['address_accuracy']:>10} "
              f"{r['wrong_student']:>7} {r['missing']:>6} {r['time']:>8}")

    # Save results
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pdf": os.path.basename(pdf_path),
        "pages": [p+1 for p in test_pages],
        "ground_truth": {str(k): v for k, v in truth.items()},
        "results": results,
    }
    os.makedirs("output", exist_ok=True)
    with open("output/extraction_model_comparison.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to output/extraction_model_comparison.json")


if __name__ == "__main__":
    main()
