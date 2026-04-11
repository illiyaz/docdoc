#!/usr/bin/env python3
"""Test LLM document understanding judgment against code-derived ground truth.

Usage:
    python scripts/test_llm_judgment.py <file_or_folder> [--pages N] [--verbose]

Examples:
    python scripts/test_llm_judgment.py uploads/abc123/3733050.pdf
    python scripts/test_llm_judgment.py docs/testingsamples/ --pages 5
    python scripts/test_llm_judgment.py docs/testingsamples/ --verbose

For each PDF, the script:
1. Analyzes the document structure using code (PyMuPDF text + heuristics)
2. Runs the LLM document understanding pipeline
3. Compares the two judgments and reports drift

The code-derived "ground truth" checks:
- Text similarity across pages (fixed vs variable layout)
- Records per page (SSN/name pattern counts)
- Structural consistency (word positions, headers)
- Pages per instance (repeating template detection)
- Whether the document has embedded text (text vs scanned)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ──────────────────────────────────────────────────────────────
# Code-derived ground truth analysis
# ──────────────────────────────────────────────────────────────

@dataclass
class CodeJudgment:
    """Ground truth derived from code analysis of the PDF."""
    file_name: str
    total_pages: int
    has_text: bool  # embedded text layer
    text_ratio: float  # fraction of pages with text
    avg_chars_per_page: float

    # Layout analysis
    layout_type: str  # "fixed", "template_with_drift", "variable"
    layout_confidence: float
    layout_evidence: list[str] = field(default_factory=list)

    # Template detection
    pages_per_instance: int = 1
    records_per_page: int = 1
    is_tabular: bool = False

    # Content analysis
    header_consistent: bool = False  # same header on every page
    common_header: str = ""
    field_types_detected: list[str] = field(default_factory=list)
    sample_fields: dict = field(default_factory=dict)  # field_type → sample values

    analysis_time_s: float = 0.0


def analyze_pdf_structure(pdf_path: str, max_pages: int = 20) -> CodeJudgment:
    """Analyze a PDF's structure using code heuristics (no LLM)."""
    import fitz

    t0 = time.time()
    doc = fitz.open(pdf_path)
    total_pages = doc.page_count
    file_name = os.path.basename(pdf_path)

    # Sample pages
    if total_pages <= max_pages:
        sample_pages = list(range(total_pages))
    else:
        # First 5, last 2, some middle
        sample_pages = list(range(min(5, total_pages)))
        sample_pages += list(range(max(0, total_pages - 2), total_pages))
        step = max(1, total_pages // (max_pages - 7))
        sample_pages += list(range(5, total_pages - 2, step))
        sample_pages = sorted(set(sample_pages))[:max_pages]

    # Extract text and words for each sampled page
    page_texts: dict[int, str] = {}
    page_words: dict[int, list] = {}
    page_first_lines: dict[int, str] = {}
    page_char_counts: list[int] = []

    for pg in sample_pages:
        page = doc[pg]
        text = page.get_text()
        words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_idx)
        page_texts[pg] = text
        page_words[pg] = words
        page_char_counts.append(len(text.strip()))

        # First non-empty line
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        page_first_lines[pg] = lines[0] if lines else ""

    doc.close()

    # ── Text presence ──
    text_pages = sum(1 for c in page_char_counts if c > 50)
    text_ratio = text_pages / len(sample_pages) if sample_pages else 0
    has_text = text_ratio > 0.5
    avg_chars = sum(page_char_counts) / len(page_char_counts) if page_char_counts else 0

    # ── Header consistency ──
    first_lines = [page_first_lines.get(pg, "") for pg in sample_pages if page_char_counts[sample_pages.index(pg)] > 50]
    if first_lines:
        most_common_header = Counter(first_lines).most_common(1)[0]
        header_count = most_common_header[1]
        header_consistent = header_count / len(first_lines) > 0.6
        common_header = most_common_header[0] if header_consistent else ""
    else:
        header_consistent = False
        common_header = ""

    # ── Page similarity (layout detection) ──
    content_pages = [pg for pg in sample_pages if len(page_texts.get(pg, "").strip()) > 50]
    similarities = []
    if len(content_pages) >= 2:
        # Compare structure of consecutive content pages
        for i in range(len(content_pages) - 1):
            t1 = page_texts[content_pages[i]]
            t2 = page_texts[content_pages[i + 1]]
            # Structural similarity: compare line patterns, not content
            lines1 = [l.strip()[:30] for l in t1.split("\n") if l.strip()]
            lines2 = [l.strip()[:30] for l in t2.split("\n") if l.strip()]
            # Length similarity
            len_sim = 1.0 - abs(len(lines1) - len(lines2)) / max(len(lines1), len(lines2), 1)
            # First-line similarity (header)
            head_sim = 1.0 if lines1 and lines2 and lines1[0] == lines2[0] else 0.0
            # Overall structure: number of lines, char distribution
            char_sim = 1.0 - abs(len(t1) - len(t2)) / max(len(t1), len(t2), 1)
            similarities.append((len_sim + head_sim + char_sim) / 3)

    avg_similarity = sum(similarities) / len(similarities) if similarities else 0

    # ── Layout type determination ──
    layout_evidence = []
    if avg_similarity > 0.85:
        layout_type = "fixed"
        layout_confidence = min(avg_similarity, 0.95)
        layout_evidence.append(f"Page structure similarity: {avg_similarity:.2f} (>0.85 = fixed)")
    elif avg_similarity > 0.6:
        layout_type = "template_with_drift"
        layout_confidence = avg_similarity
        layout_evidence.append(f"Page structure similarity: {avg_similarity:.2f} (0.6-0.85 = template_with_drift)")
    else:
        layout_type = "variable"
        layout_confidence = 1.0 - avg_similarity
        layout_evidence.append(f"Page structure similarity: {avg_similarity:.2f} (<0.6 = variable)")

    if header_consistent:
        layout_evidence.append(f"Consistent header: '{common_header[:50]}'")
        if layout_type == "variable":
            layout_type = "template_with_drift"
            layout_confidence = 0.7

    # ── Records per page ──
    ssn_pattern = re.compile(r'\d{3}-\d{2}-\d{4}')
    name_pattern = re.compile(r'(?:^|\n)\s*[A-Z][a-z]+[, ]+[A-Z][a-z]+')
    phone_pattern = re.compile(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}')
    address_pattern = re.compile(r'\d+\s+[A-Z][A-Za-z\s]+(?:ST|AVE|DR|RD|BLVD|LN|CT|WAY|CIR|PL)\b', re.IGNORECASE)

    records_per_page_samples = []
    field_types = set()
    sample_fields: dict[str, list[str]] = defaultdict(list)

    for pg in content_pages[:10]:
        text = page_texts[pg]
        ssns = ssn_pattern.findall(text)
        names = name_pattern.findall(text)
        phones = phone_pattern.findall(text)
        addresses = address_pattern.findall(text)

        # Estimate records per page from the max entity count
        rpp = max(len(ssns), len(names) // 2, 1)  # names often have first+last
        records_per_page_samples.append(rpp)

        if ssns:
            field_types.add("US_SSN")
            sample_fields["US_SSN"].extend(ssns[:2])
        if names:
            field_types.add("PERSON")
            sample_fields["PERSON"].extend([n.strip() for n in names[:3]])
        if phones:
            field_types.add("PHONE_NUMBER")
            sample_fields["PHONE_NUMBER"].extend(phones[:2])
        if addresses:
            field_types.add("LOCATION")
            sample_fields["LOCATION"].extend([a.strip() for a in addresses[:2]])

        # Check for dates
        dates = re.findall(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', text)
        if dates:
            field_types.add("DATE")
            sample_fields["DATE"].extend(dates[:2])

        # Check for email
        emails = re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', text)
        if emails:
            field_types.add("EMAIL")
            sample_fields["EMAIL"].extend(emails[:2])

    avg_rpp = sum(records_per_page_samples) / len(records_per_page_samples) if records_per_page_samples else 1
    is_tabular = avg_rpp > 2

    # ── Pages per instance ──
    # If layout is fixed/template and not tabular, check if content changes every N pages
    pages_per_instance = 1
    if layout_type in ("fixed", "template_with_drift") and not is_tabular and len(content_pages) >= 4:
        # Check if alternate pages have different content (e.g., page 0 = data, page 1 = grades)
        # Simple heuristic: do consecutive content pages have the SAME entity names?
        name_sets = []
        for pg in content_pages[:10]:
            pg_names = set(name_pattern.findall(page_texts[pg]))
            name_sets.append(pg_names)

        # If names change every page → 1 page per instance
        # If names change every 2 pages → 2 pages per instance
        changes = []
        for i in range(1, len(name_sets)):
            overlap = len(name_sets[i] & name_sets[i-1])
            total = max(len(name_sets[i] | name_sets[i-1]), 1)
            changes.append(overlap / total)

        # If names change on every page (overlap < 0.3), it's 1 page per instance
        avg_overlap = sum(changes) / len(changes) if changes else 0
        if avg_overlap < 0.3:
            pages_per_instance = 1
        elif avg_overlap < 0.6:
            pages_per_instance = 2
        else:
            pages_per_instance = 3  # or more — names persist across pages

    analysis_time = time.time() - t0

    return CodeJudgment(
        file_name=file_name,
        total_pages=total_pages,
        has_text=has_text,
        text_ratio=text_ratio,
        avg_chars_per_page=avg_chars,
        layout_type=layout_type,
        layout_confidence=layout_confidence,
        layout_evidence=layout_evidence,
        pages_per_instance=pages_per_instance,
        records_per_page=round(avg_rpp),
        is_tabular=is_tabular,
        header_consistent=header_consistent,
        common_header=common_header,
        field_types_detected=sorted(field_types),
        sample_fields={k: v[:3] for k, v in sample_fields.items()},
        analysis_time_s=analysis_time,
    )


# ──────────────────────────────────────────────────────────────
# LLM judgment (uses the actual pipeline code)
# ──────────────────────────────────────────────────────────────

@dataclass
class LLMJudgment:
    """Judgment from the LLM document understanding pipeline."""
    model: str = ""
    document_type: str = "unknown"
    document_subtype: str | None = None
    layout_type: str = "variable"
    layout_confidence: float = 0.0
    layout_field_map_count: int = 0
    layout_field_map_types: list[str] = field(default_factory=list)
    is_tabular: bool = False
    records_per_page: int = 1
    pages_per_instance: int = 1
    template_name: str | None = None
    field_map_types: list[str] = field(default_factory=list)
    people_count: int = 0
    schema_confidence: float = 0.0
    raw_layout_type: str = ""  # what LLM actually said (before parser downgrade)
    raw_response: str = ""     # raw JSON from LLM
    llm_time_s: float = 0.0
    error: str | None = None


# Models suitable for text-based document understanding (no vision needed)
TEXT_MODELS = [
    "qwen2.5:7b",
    "gemma3:12b",
    "gemma3:27b",
    "phi4:latest",
    "mistral:instruct",
    "llama3.2-vision:90b",  # current default (overkill for text)
]


def run_llm_judgment(
    pdf_path: str,
    max_pages: int = 5,
    model: str | None = None,
) -> LLMJudgment:
    """Run the LLM document understanding on a PDF.

    Parameters
    ----------
    model:
        Ollama model to use. If None, uses the default from settings.
        This allows A/B testing different models on the same document.
    """
    import fitz
    from app.readers.pdf_reader import PDFReader

    t0 = time.time()
    result = LLMJudgment()

    try:
        # Read blocks
        doc = fitz.open(pdf_path)
        total_pages = doc.page_count
        doc.close()

        reader = PDFReader(pdf_path)
        pages_to_read = list(range(min(max_pages, total_pages)))
        blocks = reader.read_pages(pages_to_read)

        if not blocks:
            result.error = "No text blocks extracted"
            return result

        # Build the LLM client with the specified model
        from app.llm.client import OllamaClient
        from app.llm.prompts import PROMPT_TEMPLATES, SYSTEM_PROMPT
        from app.structure.llm_document_understanding import LLMDocumentUnderstanding

        if model:
            # Override the model for this run
            client = OllamaClient(model=model)
            result.model = model
        else:
            client = OllamaClient()
            result.model = client.model

        # Build the understanding engine with custom client
        understanding = LLMDocumentUnderstanding(llm_client=client)

        schema = understanding.understand(
            blocks,
            heuristic_doc_type="unknown",
            file_name=os.path.basename(pdf_path),
            file_type="pdf",
            structure_class="unstructured",
            onset_page=0,
            total_pages=total_pages,
        )

        if schema is None:
            result.error = "LLM returned None schema"
            result.llm_time_s = time.time() - t0
            return result

        result.document_type = schema.document_type or "unknown"
        result.document_subtype = schema.document_subtype
        result.layout_type = schema.layout_type or "variable"
        result.layout_confidence = schema.layout_confidence or 0.0
        result.layout_field_map_count = len(schema.layout_field_map) if schema.layout_field_map else 0
        result.layout_field_map_types = [
            fm.field_type for fm in (schema.layout_field_map or [])
        ]
        result.is_tabular = schema.is_tabular or False
        result.records_per_page = schema.records_per_page_estimate or 1
        result.schema_confidence = schema.schema_confidence or 0.0
        result.field_map_types = [f.semantic_type or f.label for f in (schema.field_map or [])]
        result.people_count = len(schema.people) if schema.people else 0

        if schema.template:
            result.pages_per_instance = schema.template.pages_per_instance or 1
            result.template_name = schema.template.template_name

        # Capture what the LLM actually said (before parser downgrade)
        result.raw_layout_type = getattr(schema, "_raw_layout_type", result.layout_type)

    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"

    result.llm_time_s = time.time() - t0
    return result


# ──────────────────────────────────────────────────────────────
# Comparison and drift analysis
# ──────────────────────────────────────────────────────────────

@dataclass
class DriftItem:
    field: str
    code_value: str
    llm_value: str
    severity: str  # "CRITICAL", "WARNING", "INFO"
    note: str = ""


def compare_judgments(code: CodeJudgment, llm: LLMJudgment) -> list[DriftItem]:
    """Compare code-derived and LLM judgments, return list of drifts."""
    drifts: list[DriftItem] = []

    # Layout type
    if code.layout_type != llm.layout_type:
        sev = "CRITICAL" if code.layout_type == "fixed" and llm.layout_type == "variable" else "WARNING"
        note = ""
        if code.layout_type == "fixed" and llm.layout_type == "variable":
            note = "LLM missed fixed layout → coordinate extraction unavailable"
        elif code.layout_type == "variable" and llm.layout_type != "variable":
            note = "LLM over-classified as structured"
        drifts.append(DriftItem("layout_type", code.layout_type, llm.layout_type, sev, note))

    # Layout field map
    if code.layout_type in ("fixed", "template_with_drift") and llm.layout_field_map_count == 0:
        drifts.append(DriftItem(
            "layout_field_map", f"expected (layout={code.layout_type})", "null/empty",
            "CRITICAL", "No spatial field map → coordinate extraction impossible"
        ))

    # Pages per instance
    if code.pages_per_instance != llm.pages_per_instance:
        sev = "CRITICAL" if abs(code.pages_per_instance - llm.pages_per_instance) >= 2 else "WARNING"
        note = ""
        if llm.pages_per_instance > code.pages_per_instance:
            note = f"LLM thinks {llm.pages_per_instance} pages/instance, code sees {code.pages_per_instance} → wrong template boundaries"
        drifts.append(DriftItem(
            "pages_per_instance", str(code.pages_per_instance), str(llm.pages_per_instance),
            sev, note,
        ))

    # Records per page
    if code.records_per_page != llm.records_per_page:
        sev = "WARNING"
        if code.is_tabular != llm.is_tabular:
            sev = "CRITICAL"
        drifts.append(DriftItem(
            "records_per_page", str(code.records_per_page), str(llm.records_per_page), sev,
        ))

    # Tabular
    if code.is_tabular != llm.is_tabular:
        drifts.append(DriftItem(
            "is_tabular", str(code.is_tabular), str(llm.is_tabular), "WARNING",
        ))

    # Text detection
    if code.has_text and llm.layout_type == "variable" and code.layout_type != "variable":
        drifts.append(DriftItem(
            "text_utilization", "has_text=True, layout=fixed", f"llm_layout={llm.layout_type}",
            "WARNING", "Text PDF with fixed layout not recognized → will use slow vision/presidio"
        ))

    # Field types detected by code but not by LLM
    llm_field_types = set(llm.field_map_types)
    code_field_types = set(code.field_types_detected)
    missing = code_field_types - llm_field_types
    extra = llm_field_types - code_field_types
    if missing:
        drifts.append(DriftItem(
            "field_types_missing", ", ".join(sorted(missing)), "(not in LLM)",
            "INFO", "Code found these PII types but LLM didn't mention them"
        ))
    if extra:
        drifts.append(DriftItem(
            "field_types_extra", "(not in code)", ", ".join(sorted(extra)),
            "INFO", "LLM found these but code heuristics didn't"
        ))

    return drifts


# ──────────────────────────────────────────────────────────────
# Output formatting
# ──────────────────────────────────────────────────────────────

def print_judgment(label: str, data: dict, indent: int = 4):
    prefix = " " * indent
    print(f"  {label}:")
    for k, v in data.items():
        if isinstance(v, list) and len(v) > 5:
            print(f"{prefix}{k}: [{', '.join(str(x) for x in v[:5])}... ({len(v)} total)]")
        elif isinstance(v, dict):
            print(f"{prefix}{k}: {json.dumps(v, default=str)[:120]}")
        else:
            print(f"{prefix}{k}: {v}")


def print_drifts(drifts: list[DriftItem]):
    if not drifts:
        print("  No drift detected ✓")
        return

    critical = [d for d in drifts if d.severity == "CRITICAL"]
    warnings = [d for d in drifts if d.severity == "WARNING"]
    info = [d for d in drifts if d.severity == "INFO"]

    for d in critical:
        print(f"  *** CRITICAL: {d.field}: code={d.code_value} vs llm={d.llm_value}")
        if d.note:
            print(f"      → {d.note}")

    for d in warnings:
        print(f"  ** WARNING: {d.field}: code={d.code_value} vs llm={d.llm_value}")
        if d.note:
            print(f"      → {d.note}")

    for d in info:
        print(f"  * INFO: {d.field}: code={d.code_value} vs llm={d.llm_value}")
        if d.note:
            print(f"      → {d.note}")


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test LLM document understanding judgment")
    parser.add_argument("path", help="PDF file or folder of PDFs")
    parser.add_argument("--pages", type=int, default=5, help="Max pages to send to LLM (default: 5)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    parser.add_argument("--code-only", action="store_true", help="Only run code analysis (no LLM)")
    parser.add_argument("--max-files", type=int, default=50, help="Max files to process from folder")
    parser.add_argument("--model", type=str, default=None,
                       help="Specific Ollama model to use (e.g., qwen2.5:7b)")
    parser.add_argument("--compare-models", type=str, default=None,
                       help="Comma-separated list of models to compare (e.g., qwen2.5:7b,gemma3:12b)")
    args = parser.parse_args()

    # Collect files
    target = Path(args.path)
    if target.is_file():
        files = [str(target)]
    elif target.is_dir():
        files = sorted(str(f) for f in target.rglob("*.pdf"))[:args.max_files]
    else:
        print(f"Error: {args.path} not found")
        sys.exit(1)

    if not files:
        print("No PDF files found")
        sys.exit(1)

    print("=" * 80)
    print(f"LLM JUDGMENT TEST — {len(files)} file(s)")
    print("=" * 80)

    all_results = []
    total_drifts = {"CRITICAL": 0, "WARNING": 0, "INFO": 0}

    for i, pdf_path in enumerate(files, 1):
        fname = os.path.basename(pdf_path)
        print(f"\n{'─' * 70}")
        print(f"[{i}/{len(files)}] {fname}")
        print(f"{'─' * 70}")

        # Code analysis
        print("\n  Analyzing with code heuristics...")
        code = analyze_pdf_structure(pdf_path, max_pages=20)
        print(f"  Code analysis: {code.analysis_time_s:.1f}s")

        if args.verbose:
            print_judgment("CODE JUDGMENT", {
                "total_pages": code.total_pages,
                "has_text": code.has_text,
                "text_ratio": f"{code.text_ratio:.2f}",
                "avg_chars/page": f"{code.avg_chars_per_page:.0f}",
                "layout_type": code.layout_type,
                "layout_confidence": f"{code.layout_confidence:.2f}",
                "pages_per_instance": code.pages_per_instance,
                "records_per_page": code.records_per_page,
                "is_tabular": code.is_tabular,
                "header_consistent": code.header_consistent,
                "common_header": code.common_header[:60] if code.common_header else "(none)",
                "field_types": code.field_types_detected,
                "sample_fields": code.sample_fields,
                "evidence": code.layout_evidence,
            })

        if args.code_only:
            print(f"\n  Summary: layout={code.layout_type} pages_per_instance={code.pages_per_instance} "
                  f"rpp={code.records_per_page} tabular={code.is_tabular} fields={code.field_types_detected}")
            all_results.append({"file": fname, "code": code, "llm": None, "drifts": []})
            continue

        # Determine models to test
        if args.compare_models:
            models_to_test = [m.strip() for m in args.compare_models.split(",")]
        else:
            models_to_test = [args.model]  # None = default model

        for model in models_to_test:
            model_label = model or "default"
            print(f"\n  Running LLM [{model_label}] ({args.pages} pages)...")
            llm = run_llm_judgment(pdf_path, max_pages=args.pages, model=model)
            print(f"  LLM [{model_label}]: {llm.llm_time_s:.1f}s")

            if llm.error:
                print(f"  LLM ERROR: {llm.error}")

            if args.verbose:
                print_judgment(f"LLM JUDGMENT [{model_label}]", {
                    "model": llm.model,
                    "document_type": llm.document_type,
                    "document_subtype": llm.document_subtype,
                    "layout_type": llm.layout_type,
                    "layout_confidence": f"{llm.layout_confidence:.2f}",
                    "layout_field_map_count": llm.layout_field_map_count,
                    "layout_field_map_types": llm.layout_field_map_types,
                    "is_tabular": llm.is_tabular,
                    "records_per_page": llm.records_per_page,
                    "pages_per_instance": llm.pages_per_instance,
                    "template_name": llm.template_name,
                    "schema_confidence": f"{llm.schema_confidence:.2f}",
                    "field_map_types": llm.field_map_types,
                    "people_count": llm.people_count,
                })

            # Compare
            print(f"\n  DRIFT ANALYSIS [{model_label}]:")
            drifts = compare_judgments(code, llm)
            print_drifts(drifts)

            for d in drifts:
                total_drifts[d.severity] += 1

            # Summary line
            drift_summary = f"{len([d for d in drifts if d.severity == 'CRITICAL'])} critical, " \
                           f"{len([d for d in drifts if d.severity == 'WARNING'])} warnings"
            print(f"\n  [{model_label}] code={code.layout_type}({code.layout_confidence:.2f}) "
                  f"llm={llm.layout_type}({llm.layout_confidence:.2f}) "
                  f"ppi_code={code.pages_per_instance} ppi_llm={llm.pages_per_instance} "
                  f"| {drift_summary}")

            all_results.append({"file": fname, "model": model_label, "code": code, "llm": llm, "drifts": drifts})

    # ── Final Summary ──
    print(f"\n{'=' * 80}")
    print("OVERALL SUMMARY")
    print(f"{'=' * 80}")
    print(f"Files tested: {len(files)}")
    print(f"Critical drifts: {total_drifts['CRITICAL']}")
    print(f"Warnings: {total_drifts['WARNING']}")
    print(f"Info: {total_drifts['INFO']}")

    if not args.code_only:
        # Layout type agreement
        agree = sum(1 for r in all_results if r["llm"] and r["code"].layout_type == r["llm"].layout_type)
        tested = sum(1 for r in all_results if r["llm"] is not None)
        print(f"\nLayout type agreement: {agree}/{tested} ({100*agree/tested:.0f}%)" if tested else "")

        # Pages per instance agreement
        ppi_agree = sum(1 for r in all_results if r["llm"] and r["code"].pages_per_instance == r["llm"].pages_per_instance)
        print(f"Pages/instance agreement: {ppi_agree}/{tested} ({100*ppi_agree/tested:.0f}%)" if tested else "")

        # Field map coverage
        with_fm = sum(1 for r in all_results if r["llm"] and r["llm"].layout_field_map_count > 0)
        fixed_docs = sum(1 for r in all_results if r["code"].layout_type in ("fixed", "template_with_drift"))
        print(f"Field map produced: {with_fm}/{tested}")
        if fixed_docs:
            print(f"Fixed/template docs needing field map: {fixed_docs} (got field map: {with_fm})")

    # Per-file table
    print(f"\n{'File':<35} {'Model':<20} {'Pages':>5} {'Code Layout':>12} {'LLM Layout':>12} {'PPI_C':>5} {'PPI_L':>5} {'FM':>3} {'Time':>6} {'Drifts':>7}")
    print("─" * 115)
    for r in all_results:
        c = r["code"]
        l = r["llm"]
        drift_count = len([d for d in r["drifts"] if d.severity in ("CRITICAL", "WARNING")])
        fm = l.layout_field_map_count if l else "-"
        model_name = r.get("model", "-")[:20]
        llm_time = f"{l.llm_time_s:.0f}s" if l else "-"
        print(f"{r['file'][:35]:<35} {model_name:<20} {c.total_pages:>5} {c.layout_type[:12]:>12} "
              f"{l.layout_type[:12] if l else 'N/A':>12} {c.pages_per_instance:>5} "
              f"{l.pages_per_instance if l else '-':>5} {fm:>3} {llm_time:>6} "
              f"{'***' if drift_count > 0 else 'OK':>7}")

    # Save results — grouped by file, with model comparison
    output_path = "output/llm_judgment_results.json"
    os.makedirs("output", exist_ok=True)

    # Group results by file for easy comparison
    by_file: dict[str, dict] = {}
    for r in all_results:
        fname = r["file"]
        if fname not in by_file:
            c = r["code"]
            by_file[fname] = {
                "file": fname,
                "code_judgment": {
                    "total_pages": c.total_pages,
                    "has_text": c.has_text,
                    "text_ratio": round(c.text_ratio, 2),
                    "avg_chars_per_page": round(c.avg_chars_per_page),
                    "layout_type": c.layout_type,
                    "layout_confidence": round(c.layout_confidence, 2),
                    "pages_per_instance": c.pages_per_instance,
                    "records_per_page": c.records_per_page,
                    "is_tabular": c.is_tabular,
                    "header_consistent": c.header_consistent,
                    "common_header": c.common_header[:80] if c.common_header else None,
                    "field_types_detected": c.field_types_detected,
                    "sample_fields": c.sample_fields,
                    "analysis_time_s": round(c.analysis_time_s, 2),
                },
                "llm_judgments": {},
            }

        l = r.get("llm")
        if l is not None:
            model_key = r.get("model", "default")
            by_file[fname]["llm_judgments"][model_key] = {
                "model": l.model,
                "document_type": l.document_type,
                "document_subtype": l.document_subtype,
                "layout_type": l.layout_type,
                "layout_confidence": round(l.layout_confidence, 2),
                "layout_field_map_count": l.layout_field_map_count,
                "layout_field_map_types": l.layout_field_map_types,
                "is_tabular": l.is_tabular,
                "records_per_page": l.records_per_page,
                "pages_per_instance": l.pages_per_instance,
                "template_name": l.template_name,
                "schema_confidence": round(l.schema_confidence, 2),
                "field_map_types": l.field_map_types,
                "people_count": l.people_count,
                "llm_time_s": round(l.llm_time_s, 1),
                "error": l.error,
                "drifts": [
                    {"field": d.field, "code": d.code_value, "llm": d.llm_value,
                     "severity": d.severity, "note": d.note}
                    for d in r["drifts"]
                ],
                "drift_summary": {
                    "critical": len([d for d in r["drifts"] if d.severity == "CRITICAL"]),
                    "warning": len([d for d in r["drifts"] if d.severity == "WARNING"]),
                    "info": len([d for d in r["drifts"] if d.severity == "INFO"]),
                },
            }

    # Build model leaderboard (when comparing models)
    model_scores: dict[str, dict] = defaultdict(lambda: {"files": 0, "critical": 0, "warning": 0, "time_s": 0})
    for r in all_results:
        l = r.get("llm")
        if l is None:
            continue
        model_key = r.get("model", "default")
        model_scores[model_key]["files"] += 1
        model_scores[model_key]["time_s"] += l.llm_time_s
        for d in r["drifts"]:
            if d.severity == "CRITICAL":
                model_scores[model_key]["critical"] += 1
            elif d.severity == "WARNING":
                model_scores[model_key]["warning"] += 1

    if len(model_scores) > 1:
        print(f"\n{'─' * 70}")
        print("MODEL LEADERBOARD")
        print(f"{'─' * 70}")
        print(f"{'Model':<30} {'Files':>5} {'Critical':>8} {'Warning':>8} {'Avg Time':>10}")
        print("─" * 65)
        for model_key in sorted(model_scores, key=lambda m: (model_scores[m]["critical"], model_scores[m]["warning"])):
            ms = model_scores[model_key]
            avg_time = ms["time_s"] / ms["files"] if ms["files"] else 0
            print(f"{model_key:<30} {ms['files']:>5} {ms['critical']:>8} {ms['warning']:>8} {avg_time:>9.1f}s")

    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files_tested": len(files),
        "models_tested": list(model_scores.keys()) if model_scores else [],
        "total_drifts": total_drifts,
        "model_leaderboard": {
            k: {"files": v["files"], "critical": v["critical"], "warning": v["warning"],
                "avg_time_s": round(v["time_s"] / v["files"], 1) if v["files"] else 0}
            for k, v in model_scores.items()
        },
        "files": list(by_file.values()),
    }
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults saved to: {output_path}")

    # Also save CSV for quick spreadsheet analysis
    csv_path = "output/llm_judgment_results.csv"
    with open(csv_path, "w") as f:
        f.write("file,model,total_pages,code_layout,llm_layout,code_ppi,llm_ppi,code_rpp,llm_rpp,"
                "code_tabular,llm_tabular,field_map_count,llm_time_s,critical_drifts,warning_drifts\n")
        for r in all_results:
            c = r["code"]
            l = r.get("llm")
            if l is None:
                continue
            crit = len([d for d in r["drifts"] if d.severity == "CRITICAL"])
            warn = len([d for d in r["drifts"] if d.severity == "WARNING"])
            f.write(f"{r['file']},{r.get('model','default')},{c.total_pages},"
                    f"{c.layout_type},{l.layout_type},{c.pages_per_instance},{l.pages_per_instance},"
                    f"{c.records_per_page},{l.records_per_page},{c.is_tabular},{l.is_tabular},"
                    f"{l.layout_field_map_count},{l.llm_time_s:.1f},{crit},{warn}\n")
    print(f"CSV saved to: {csv_path}")


if __name__ == "__main__":
    main()
