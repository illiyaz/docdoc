"""E3: End-to-end Pipeline Benchmark Script.

Runs the full extraction pipeline on a directory of test files and
reports accuracy, speed, and quality metrics against expected manifests.

Usage:
    python scripts/benchmark.py [--input-dir tests/fixtures] [--manifest tests/fixtures/manifest.json]
    python scripts/benchmark.py --input-dir /path/to/real/files  # no manifest = speed-only mode

Modes:
    1. With manifest: accuracy + speed metrics (compares against ground truth)
    2. Without manifest: speed + coverage metrics only

Output: JSON report + console summary
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("benchmark")


@dataclass
class FileResult:
    filename: str
    format: str
    block_count: int = 0
    detection_count: int = 0
    person_count: int = 0
    ssn_count: int = 0
    email_count: int = 0
    phone_count: int = 0
    read_time_ms: float = 0.0
    detect_time_ms: float = 0.0
    total_time_ms: float = 0.0
    error: str | None = None
    # Accuracy (manifest mode only)
    expected_records: int | None = None
    names_found: list[str] = field(default_factory=list)
    names_missed: list[str] = field(default_factory=list)
    recall: float | None = None


@dataclass
class BenchmarkReport:
    timestamp: str = ""
    input_dir: str = ""
    total_files: int = 0
    files_succeeded: int = 0
    files_failed: int = 0
    total_blocks: int = 0
    total_detections: int = 0
    total_persons: int = 0
    total_read_time_ms: float = 0.0
    total_detect_time_ms: float = 0.0
    total_time_ms: float = 0.0
    avg_read_time_ms: float = 0.0
    avg_detect_time_ms: float = 0.0
    # Accuracy (manifest mode)
    has_manifest: bool = False
    overall_name_recall: float | None = None
    files: list[FileResult] = field(default_factory=list)


def benchmark_file(
    filepath: str,
    engine: object | None = None,
    manifest_entry: dict | None = None,
) -> FileResult:
    """Benchmark a single file: read + detect."""
    from app.readers.registry import get_reader

    path = Path(filepath)
    result = FileResult(
        filename=path.name,
        format=path.suffix.lstrip(".").lower(),
    )

    # --- Read phase ---
    t0 = time.time()
    try:
        reader = get_reader(str(path))
        blocks = reader.read()
        result.block_count = len(blocks)
    except Exception as e:
        result.error = f"Read failed: {type(e).__name__}: {e}"
        result.total_time_ms = (time.time() - t0) * 1000
        return result
    result.read_time_ms = (time.time() - t0) * 1000

    # --- Detect phase (if Presidio available) ---
    if engine is not None and blocks:
        t1 = time.time()
        try:
            full_text = "\n".join(b.text for b in blocks)
            detections = engine.analyze(full_text)
            result.detection_count = len(detections)
            result.person_count = sum(1 for d in detections if d.entity_type == "PERSON")
            result.ssn_count = sum(1 for d in detections if d.entity_type in ("US_SSN", "SSN"))
            result.email_count = sum(1 for d in detections if d.entity_type == "EMAIL_ADDRESS")
            result.phone_count = sum(1 for d in detections if d.entity_type == "PHONE_NUMBER")
        except Exception as e:
            result.error = f"Detection failed: {type(e).__name__}: {e}"
        result.detect_time_ms = (time.time() - t1) * 1000

    # --- Accuracy (if manifest available) ---
    if manifest_entry:
        result.expected_records = manifest_entry.get("expected_records", 0)
        expected_names = [p["name"] for p in manifest_entry.get("persons", [])]
        full_text = "\n".join(b.text for b in blocks) if blocks else ""

        result.names_found = [n for n in expected_names if n in full_text]
        result.names_missed = [n for n in expected_names if n not in full_text]
        if expected_names:
            result.recall = len(result.names_found) / len(expected_names)

    result.total_time_ms = result.read_time_ms + result.detect_time_ms
    return result


def run_benchmark(
    input_dir: str,
    manifest_path: str | None = None,
) -> BenchmarkReport:
    """Run the full benchmark on a directory of files."""
    from datetime import datetime

    report = BenchmarkReport(
        timestamp=datetime.now().isoformat(),
        input_dir=input_dir,
    )

    # Load manifest if provided
    manifest_lookup: dict[str, dict] = {}
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
        for entry in manifest:
            manifest_lookup[entry["filename"]] = entry
        report.has_manifest = True

    # Try to initialize Presidio
    engine = None
    try:
        from app.pii.presidio_engine import PresidioEngine
        engine = PresidioEngine()
        logger.info("Presidio engine loaded")
    except ImportError:
        logger.warning("Presidio not available — running speed-only benchmark")

    # Discover files
    input_path = Path(input_dir)
    if not input_path.exists():
        logger.error("Input directory does not exist: %s", input_dir)
        return report

    supported = {
        ".pdf", ".xlsx", ".xls", ".csv", ".docx", ".doc",
        ".html", ".htm", ".xml", ".eml", ".msg",
        ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
        ".txt",
    }
    files = sorted(
        f for f in input_path.iterdir()
        if f.is_file() and f.suffix.lower() in supported
        and f.name != "manifest.json"
    )

    report.total_files = len(files)
    logger.info("Found %d files to benchmark in %s", len(files), input_dir)

    # Benchmark each file
    for filepath in files:
        manifest_entry = manifest_lookup.get(filepath.name)
        result = benchmark_file(str(filepath), engine, manifest_entry)

        if result.error:
            report.files_failed += 1
            logger.warning("  FAIL: %s — %s", result.filename, result.error)
        else:
            report.files_succeeded += 1
            logger.info(
                "  OK: %s | %d blocks, %d detections | %.0fms read, %.0fms detect%s",
                result.filename, result.block_count, result.detection_count,
                result.read_time_ms, result.detect_time_ms,
                f" | recall={result.recall:.0%}" if result.recall is not None else "",
            )

        report.total_blocks += result.block_count
        report.total_detections += result.detection_count
        report.total_persons += result.person_count
        report.total_read_time_ms += result.read_time_ms
        report.total_detect_time_ms += result.detect_time_ms
        report.total_time_ms += result.total_time_ms
        report.files.append(result)

    # Compute averages
    if report.files_succeeded > 0:
        report.avg_read_time_ms = report.total_read_time_ms / report.files_succeeded
        report.avg_detect_time_ms = report.total_detect_time_ms / report.files_succeeded

    # Compute overall recall
    if report.has_manifest:
        total_expected = sum(
            r.expected_records or 0 for r in report.files if r.recall is not None
        )
        total_found = sum(
            len(r.names_found) for r in report.files if r.recall is not None
        )
        report.overall_name_recall = total_found / total_expected if total_expected > 0 else None

    return report


def print_summary(report: BenchmarkReport) -> None:
    """Print a human-readable summary."""
    print("\n" + "=" * 60)
    print("BENCHMARK SUMMARY")
    print("=" * 60)
    print(f"  Input: {report.input_dir}")
    print(f"  Files: {report.files_succeeded}/{report.total_files} succeeded")
    print(f"  Blocks: {report.total_blocks}")
    print(f"  Detections: {report.total_detections}")
    print(f"  Persons found: {report.total_persons}")
    print(f"  Total time: {report.total_time_ms:.0f}ms")
    print(f"  Avg read: {report.avg_read_time_ms:.0f}ms/file")
    print(f"  Avg detect: {report.avg_detect_time_ms:.0f}ms/file")

    if report.overall_name_recall is not None:
        print(f"  Name recall: {report.overall_name_recall:.0%}")

    if report.files_failed > 0:
        print(f"\n  FAILURES ({report.files_failed}):")
        for r in report.files:
            if r.error:
                print(f"    {r.filename}: {r.error}")

    # Per-format breakdown
    by_format: dict[str, list[FileResult]] = {}
    for r in report.files:
        by_format.setdefault(r.format, []).append(r)

    print("\n  BY FORMAT:")
    for fmt, results in sorted(by_format.items()):
        ok = sum(1 for r in results if not r.error)
        avg_blocks = sum(r.block_count for r in results) / max(ok, 1)
        avg_time = sum(r.total_time_ms for r in results) / max(ok, 1)
        print(f"    {fmt:>5s}: {ok}/{len(results)} ok | avg {avg_blocks:.0f} blocks | avg {avg_time:.0f}ms")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Forentis AI Extraction Benchmark")
    parser.add_argument("--input-dir", default="tests/fixtures",
                        help="Directory of files to benchmark")
    parser.add_argument("--manifest", default=None,
                        help="Path to manifest.json for accuracy testing")
    parser.add_argument("--output", default=None,
                        help="Path to write JSON report (default: stdout summary only)")
    args = parser.parse_args()

    # Auto-detect manifest
    manifest = args.manifest
    if manifest is None:
        auto = Path(args.input_dir) / "manifest.json"
        if auto.exists():
            manifest = str(auto)

    report = run_benchmark(args.input_dir, manifest)
    print_summary(report)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(asdict(report), f, indent=2)
        print(f"\nJSON report: {args.output}")


if __name__ == "__main__":
    main()
