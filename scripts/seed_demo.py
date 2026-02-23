#!/usr/bin/env python3
"""Seed demo data: 10 NotificationSubjects, 1 NotificationList, audit events.

Usage:
    python scripts/seed_demo.py          # uses DATABASE_URL from env / .env
    DATABASE_URL=... python scripts/seed_demo.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Ensure project root is on sys.path
sys.path.insert(0, ".")

from app.core.settings import get_settings
from app.db.base import Base
from app.db.models import AuditEvent, NotificationList, NotificationSubject


def seed(session: Session) -> None:
    """Insert demo notification subjects, a notification list, and audit events."""

    subjects: list[NotificationSubject] = []
    now = datetime.now(timezone.utc)

    demo_people = [
        # (name, email, phone, address_country, pii_types, review_status)
        ("Alice Johnson", "alice.johnson@example.com", "+12025551001", "US", ["ssn", "email", "name"], "AI_PENDING"),
        ("Bob Smith", "bob.smith@example.com", "+12025551002", "US", ["ssn", "dob", "name"], "AI_PENDING"),
        ("Priya Patel", "priya.patel@example.in", "+919876543210", "IN", ["aadhaar", "email", "name"], "AI_PENDING"),
        ("Carlos Rivera", "carlos.r@example.com", "+12025551003", "US", ["ssn", "email", "phone"], "HUMAN_REVIEW"),
        ("Fatima Khan", "fatima.khan@example.co.uk", "+447911123456", "UK", ["nhs_number", "name", "dob"], "HUMAN_REVIEW"),
        ("David Chen", "david.chen@example.com", "+12025551004", "US", ["ssn", "credit_card", "name"], "HUMAN_REVIEW"),
        ("Emily Williams", "emily.w@example.com", "+12025551005", "US", ["ssn", "email", "medical_record"], "APPROVED"),
        ("Raj Sharma", "raj.sharma@example.in", "+919876543211", "IN", ["aadhaar", "phone", "name"], "APPROVED"),
        ("Maria Garcia", "maria.g@example.com", "+12025551006", "US", ["ssn", "email", "name", "dob"], "NOTIFIED"),
        ("James Brown", "james.b@example.com", "+12025551007", "US", ["ssn", "financial_account", "name"], "NOTIFIED"),
    ]

    for name, email, phone, country, pii_types, status in demo_people:
        subj = NotificationSubject(
            subject_id=uuid4(),
            canonical_name=name,
            canonical_email=email,
            canonical_phone=phone,
            canonical_address={"country": country},
            pii_types_found=pii_types,
            source_records=[str(uuid4())],
            merge_confidence=0.92,
            notification_required=(status in ("APPROVED", "NOTIFIED")),
            review_status=status,
        )
        session.add(subj)
        subjects.append(subj)

    session.flush()

    # Notification list for HIPAA — includes the APPROVED + NOTIFIED subjects
    approved_ids = [str(s.subject_id) for s in subjects if s.review_status in ("APPROVED", "NOTIFIED")]
    nl = NotificationList(
        notification_list_id=uuid4(),
        job_id="demo-job-001",
        protocol_id="hipaa_breach_rule",
        subject_ids=approved_ids,
        status="APPROVED",
        approved_at=now,
        approved_by="demo-admin",
    )
    session.add(nl)

    # Audit events — one per subject
    event_map = {
        "AI_PENDING": "ai_extraction",
        "HUMAN_REVIEW": "human_review",
        "APPROVED": "approval",
        "NOTIFIED": "notification_sent",
    }
    for subj in subjects:
        evt = AuditEvent(
            audit_event_id=uuid4(),
            event_type=event_map[subj.review_status],
            actor="demo-seed" if subj.review_status == "AI_PENDING" else "demo-reviewer",
            subject_id=str(subj.subject_id),
            decision=subj.review_status.lower(),
            rationale=f"Demo seed — {subj.review_status}",
        )
        session.add(evt)

    session.commit()
    print(f"Seeded {len(subjects)} NotificationSubjects, 1 NotificationList, {len(subjects)} AuditEvents.")


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        seed(session)


if __name__ == "__main__":
    main()
