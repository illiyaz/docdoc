#!/usr/bin/env python3
"""End-to-end test of auditor workflow features against real PDFs.

Tests all new endpoints from Steps 26b-26d and 29a:
  1. Document viewer — render pages, bbox overlays
  2. Merge explanation — entity resolution with signals
  3. Notification preview — masked template rendering
  4. Delivery dashboard — status summary
  5. Dedup summary — enriched completion result

Usage:
    python scripts/test_auditor_workflow.py
"""
from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, Document, Extraction, IngestionRun, NotificationSubject,
)

SAMPLES = Path(__file__).resolve().parent.parent / "docs" / "testingsamples"

# In-memory DB for isolated testing
engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

passed = 0
failed = 0


def _result(name: str, ok: bool, detail: str = ""):
    global passed, failed
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def _make_run(db):
    run = IngestionRun(
        id=uuid4(), source_path="/test", config_hash="t",
        code_version="1.0", initiated_by="test", status="completed",
    )
    db.add(run)
    db.commit()
    return run


# =========================================================================
# TEST 1: Document Viewer — Page Rendering
# =========================================================================
def test_document_viewer():
    print("\n=== TEST 1: Document Viewer ===")
    db = Session()
    run = _make_run(db)

    # Use a real PDF
    pdf_path = str(SAMPLES / "CMG_Inc_0001352703.pdf")
    if not Path(pdf_path).exists():
        _result("Real PDF exists", False, f"{pdf_path} not found")
        db.close()
        return

    doc = Document(
        id=uuid4(), ingestion_run_id=run.id,
        source_path=pdf_path, file_name="CMG_Inc_0001352703.pdf",
        file_type="pdf", sha256="cmg123", page_count=453,
        content_onset_page=1,
    )
    db.add(doc)

    # Add an extraction with bbox on page 1
    ext = Extraction(
        id=uuid4(), document_id=doc.id,
        pii_type="PERSON", sensitivity="high",
        hashed_value="h1", masked_value="K*** A***",
        evidence_page=1,
        evidence_bbox={"x0": 110.7, "y0": 66.0, "x1": 160.0, "y1": 77.0},
    )
    db.add(ext)
    db.commit()

    from app.api.routes.documents import get_document_info, get_document_page

    # 1a: Document info
    info = get_document_info(doc.id, db)
    _result("get_document_info returns metadata", info["is_pdf"] and info["page_count"] == 453)
    _result("onset_page correct", info["onset_page"] == 1)

    # 1b: Render page without highlights
    page = get_document_page(doc.id, 1, db)
    img_bytes = base64.b64decode(page["image_base64"])
    _result("Page renders as PNG", img_bytes[:4] == b"\x89PNG")
    _result("Page count in response", page["page_count"] == 453)

    # 1c: Render page WITH highlights
    page_hl = get_document_page(doc.id, 1, db, highlight_extractions=True)
    _result("Highlights returned", len(page_hl["highlighted_extractions"]) == 1)
    _result("Highlight has bbox", page_hl["highlighted_extractions"][0]["bbox"] is not None)
    _result("Highlight has masked value", page_hl["highlighted_extractions"][0]["masked_value"] == "K*** A***")

    # 1d: Highlighted image is valid PNG
    img_hl = base64.b64decode(page_hl["image_base64"])
    _result("Highlighted page is PNG", img_hl[:4] == b"\x89PNG")

    # 1e: Page 0 (cover page)
    page0 = get_document_page(doc.id, 0, db)
    _result("Cover page renders", len(page0["image_base64"]) > 100)

    db.close()


# =========================================================================
# TEST 2: Merge Explanation
# =========================================================================
def test_merge_explanation():
    print("\n=== TEST 2: Merge Explanation ===")

    from app.rra.entity_resolver import (
        PIIRecord, EntityResolver, build_confidence_explained, MergeExplanation,
    )

    # Two records with shared SSN + similar name
    r1 = PIIRecord(
        record_id="r1", entity_type="PERSON", normalized_value="John Smith",
        raw_name="John Smith", raw_government_id="123-45-6789",
        raw_email="john@test.com", raw_dob="1985-01-15",
        source_document_id="claims.pdf", page_or_sheet=5,
    )
    r2 = PIIRecord(
        record_id="r2", entity_type="PERSON", normalized_value="J. Smith",
        raw_name="J. Smith", raw_government_id="123-45-6789",
        raw_email="john@test.com",
        source_document_id="roster.xlsx", page_or_sheet=42,
    )

    # 2a: build_confidence_explained
    ex = build_confidence_explained(r1, r2)
    _result("Returns MergeExplanation", isinstance(ex, MergeExplanation))
    _result("Has signals", len(ex.signals) > 0)
    _result("SSN signal matched", any(s.anchor == "ssn" and s.matched for s in ex.signals))
    _result("Email signal matched", any(s.anchor == "email" and s.matched for s in ex.signals))
    _result("Record labels populated", "John Smith" in ex.record_a_label and "J. Smith" in ex.record_b_label)

    # 2b: Gov ID masked in signal
    ssn_sig = [s for s in ex.signals if s.anchor == "ssn"][0]
    _result("Gov ID masked (no full SSN)", "123-45" not in ssn_sig.field_a)
    _result("Gov ID shows last 4", ssn_sig.field_a.endswith("6789"))

    # 2c: Resolve produces explanations
    resolver = EntityResolver()
    groups = resolver.resolve([r1, r2])
    _result("Merged into 1 group", len(groups) == 1)
    _result("Group has explanations", len(groups[0].merge_explanations) > 0)

    # 2d: Overall confidence matches build_confidence
    from app.rra.entity_resolver import build_confidence
    conf = build_confidence(r1, r2)
    _result("Explained confidence matches", abs(ex.overall_confidence - conf) < 0.01,
            f"{ex.overall_confidence} vs {conf}")

    # 2e: Cross-role blocked
    r3 = PIIRecord(
        record_id="r3", entity_type="PERSON", normalized_value="Corp Inc",
        raw_name="Corp Inc", entity_role="institutional",
        source_document_id="d1", page_or_sheet=1,
    )
    r4 = PIIRecord(
        record_id="r4", entity_type="PERSON", normalized_value="John",
        raw_name="John", entity_role="primary_subject",
        source_document_id="d1", page_or_sheet=1,
    )
    ex_blocked = build_confidence_explained(r3, r4)
    _result("Cross-role blocked (conf=0)", ex_blocked.overall_confidence == 0.0)
    _result("Blocked signal present", ex_blocked.signals[0].anchor == "role")

    # 2f: Store in DB
    db = Session()
    ns = NotificationSubject(
        subject_id=uuid4(),
        canonical_name="John Smith",
        merge_confidence=ex.overall_confidence,
        merge_explanation={
            "pairs": [{
                "record_a_label": ex.record_a_label,
                "record_b_label": ex.record_b_label,
                "overall_confidence": ex.overall_confidence,
                "signals": [{"anchor": s.anchor, "matched": s.matched, "score": s.score,
                             "detail": s.detail, "field_a": s.field_a, "field_b": s.field_b}
                            for s in ex.signals],
            }]
        },
    )
    db.add(ns)
    db.commit()

    loaded = db.query(NotificationSubject).filter_by(subject_id=ns.subject_id).first()
    _result("Merge explanation persisted", loaded.merge_explanation is not None)
    _result("Pairs in DB", len(loaded.merge_explanation["pairs"]) == 1)
    db.close()


# =========================================================================
# TEST 3: Notification Preview
# =========================================================================
def test_notification_preview():
    print("\n=== TEST 3: Notification Preview ===")
    db = Session()

    ns = NotificationSubject(
        subject_id=uuid4(),
        canonical_name="Adeline Chandler",
        canonical_email="adeline.chandler@example.com",
        canonical_address={"street": "123 Main St", "city": "Springfield", "state": "IL", "zip": "62701"},
        pii_types_found=["PERSON", "US_SSN", "DOB", "LOCATION"],
        merge_confidence=0.95,
        notification_required=True,
        review_status="APPROVED",
    )
    db.add(ns)
    db.commit()

    from app.api.routes.notifications import (
        preview_email, preview_letter, _mask_name, _mask_email,
    )

    # 3a: Masking
    _result("Name masked", _mask_name("Adeline Chandler") == "A*** C***")
    _result("Email masked", _mask_email("adeline.chandler@example.com") == "a***@example.com")
    _result("None name → default", _mask_name(None) == "Affected Individual")

    # 3b: Email preview
    result = preview_email(ns.subject_id, "default", db)
    _result("Email preview returns HTML", len(result["html"]) > 50)
    _result("Email format correct", result["format"] == "email")
    _result("No raw name in preview", "Adeline Chandler" not in result["html"])
    _result("No raw email in preview", "adeline.chandler@example.com" not in result["html"])
    _result("Masked name in preview", "A*** C***" in result["html"] or "Affected Individual" in result["html"])

    # 3c: Letter preview
    letter = preview_letter(ns.subject_id, "default", db)
    _result("Letter preview returns HTML", len(letter["html"]) > 50)
    _result("Letter format correct", letter["format"] == "letter")
    _result("No raw name in letter", "Adeline Chandler" not in letter["html"])

    # 3d: HIPAA template
    hipaa = preview_email(ns.subject_id, "hipaa_breach_rule", db)
    _result("HIPAA template renders", len(hipaa["html"]) > 50)

    # 3e: PII types in preview (shown as categories, not values)
    _result("PII types shown", "PERSON" in result["html"] or "US_SSN" in result["html"])

    db.close()


# =========================================================================
# TEST 4: Delivery Dashboard
# =========================================================================
def test_delivery_dashboard():
    print("\n=== TEST 4: Delivery Dashboard ===")
    db = Session()

    project_id = uuid4()
    statuses = ["APPROVED", "APPROVED", "NOTIFIED", "REJECTED", "AI_PENDING"]
    for i, status in enumerate(statuses):
        ns = NotificationSubject(
            subject_id=uuid4(),
            project_id=project_id,
            canonical_name=f"Subject {i}",
            review_status=status,
            notification_required=(status in ("APPROVED", "NOTIFIED")),
        )
        db.add(ns)
    db.commit()

    from app.api.routes.notifications import get_delivery_status
    result = get_delivery_status(project_id, db)

    _result("Total subjects", result["total_subjects"] == 5)
    _result("Notification required count", result["notification_required"] == 3)
    _result("Approved ready", result["summary"]["approved_ready"] == 2)
    _result("Notified sent", result["summary"]["notified_sent"] == 1)
    _result("Rejected", result["summary"]["rejected"] == 1)
    _result("Pending review", result["summary"]["pending_review"] == 1)
    _result("Subject list filtered to notif_required", len(result["subjects"]) == 3)

    # Verify names are masked
    for s in result["subjects"]:
        _result(f"Name masked ({s['name']})", "***" in s["name"] or s["name"] == "Affected Individual")

    db.close()


# =========================================================================
# TEST 5: Renderer with Real PDF Bboxes
# =========================================================================
def test_renderer_real_pdf():
    print("\n=== TEST 5: Renderer with Real PDF ===")

    pdf_path = str(SAMPLES / "ABCNY_560_0001384129.pdf")
    if not Path(pdf_path).exists():
        _result("ABCNY exists", False, "not found")
        return

    from app.pdf.renderer import render_page_with_overlays, render_page_to_image

    # 5a: Plain render
    img = render_page_to_image(pdf_path, 0, dpi=150)
    _result("ABCNY page 0 renders", len(img) > 1000)

    # 5b: With overlay bboxes
    bboxes = [
        {"x0": 100, "y0": 200, "x1": 300, "y1": 220, "pii_type": "PERSON"},
        {"x0": 100, "y0": 230, "x1": 250, "y1": 250, "pii_type": "US_SSN"},
        {"x0": 100, "y0": 260, "x1": 200, "y1": 280, "pii_type": "DOB"},
    ]
    img_hl = render_page_with_overlays(pdf_path, 0, bboxes, dpi=150)
    _result("Overlay renders", len(img_hl) > 1000)

    # 5c: Verify it's a valid PNG
    img_bytes = base64.b64decode(img_hl)
    _result("Overlay is valid PNG", img_bytes[:4] == b"\x89PNG")

    # 5d: Different DPI
    img_72 = render_page_to_image(pdf_path, 0, dpi=72)
    img_300 = render_page_to_image(pdf_path, 0, dpi=300)
    _result("72 DPI < 150 DPI < 300 DPI", len(img_72) < len(img) < len(img_300))


# =========================================================================
# Run all
# =========================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("AUDITOR WORKFLOW E2E TEST — Real PDFs + Real Data")
    print("=" * 60)

    test_document_viewer()
    test_merge_explanation()
    test_notification_preview()
    test_delivery_dashboard()
    test_renderer_real_pdf()

    print("\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
    print("=" * 60)

    sys.exit(1 if failed > 0 else 0)
