"""Tests for app/protocols/ — Phase 3 protocol configuration."""
from __future__ import annotations

import textwrap
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import NotificationSubject
from app.protocols.protocol import Protocol
from app.protocols.loader import load_protocol, load_all_protocols
from app.protocols.registry import ProtocolRegistry
from app.protocols.regulatory_threshold import apply_protocol, apply_protocol_to_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BUILTIN_DIR = "config/protocols"


def _hipaa() -> Protocol:
    return Protocol(
        protocol_id="hipaa_breach_rule",
        name="HIPAA Breach Notification Rule",
        jurisdiction="US-FEDERAL",
        triggering_entity_types=["US_SSN", "PHI_MRN", "PHI_NPI"],
        notification_threshold=1,
        notification_deadline_days=60,
        required_notification_content=["description of breach"],
        regulatory_framework="45 CFR §164.400-414",
    )


# ===========================================================================
# Protocol.is_triggered_by
# ===========================================================================

class TestIsTriggeredBy:
    def test_triggered_by_matching_type(self):
        p = _hipaa()
        assert p.is_triggered_by(["US_SSN"]) is True

    def test_not_triggered_by_unrelated_type(self):
        p = _hipaa()
        assert p.is_triggered_by(["FERPA_STUDENT_ID"]) is False

    def test_case_insensitive(self):
        p = _hipaa()
        assert p.is_triggered_by(["us_ssn"]) is True

    def test_empty_list_returns_false(self):
        p = _hipaa()
        assert p.is_triggered_by([]) is False

    def test_multiple_types_any_match(self):
        p = _hipaa()
        assert p.is_triggered_by(["FERPA_STUDENT_ID", "PHI_MRN"]) is True

    def test_none_matching(self):
        p = _hipaa()
        assert p.is_triggered_by(["EMAIL", "PHONE"]) is False


# ===========================================================================
# load_protocol
# ===========================================================================

class TestLoadProtocol:
    def test_valid_yaml_loads_correctly(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            protocol_id: test_protocol
            name: Test Protocol
            jurisdiction: TEST
            triggering_entity_types:
              - US_SSN
            notification_threshold: 1
            notification_deadline_days: 30
            required_notification_content:
              - description
            regulatory_framework: "Test Framework"
        """)
        f = tmp_path / "test.yaml"
        f.write_text(yaml_content)

        p = load_protocol(f)
        assert p.protocol_id == "test_protocol"
        assert p.name == "Test Protocol"
        assert p.jurisdiction == "TEST"
        assert p.triggering_entity_types == ["US_SSN"]
        assert p.notification_threshold == 1
        assert p.notification_deadline_days == 30
        assert p.required_notification_content == ["description"]
        assert p.regulatory_framework == "Test Framework"

    def test_missing_required_field_raises(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            protocol_id: broken
            name: Broken
        """)
        f = tmp_path / "broken.yaml"
        f.write_text(yaml_content)

        with pytest.raises(ValueError, match="missing required fields"):
            load_protocol(f)

    def test_optional_fields_loaded(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            protocol_id: opt
            name: Optional Fields
            jurisdiction: TEST
            triggering_entity_types:
              - EMAIL
            notification_threshold: 1
            notification_deadline_days: 3
            individual_deadline_days: 30
            requires_hhs_notification: true
            required_notification_content:
              - description
            regulatory_framework: "Test"
        """)
        f = tmp_path / "opt.yaml"
        f.write_text(yaml_content)

        p = load_protocol(f)
        assert p.individual_deadline_days == 30
        assert p.requires_hhs_notification is True

    def test_all_six_builtins_load(self):
        protocols = load_all_protocols(BUILTIN_DIR)
        assert len(protocols) == 6
        ids = {p.protocol_id for p in protocols}
        assert ids == {
            "hipaa_breach_rule",
            "gdpr_article_33",
            "ccpa",
            "hitech",
            "ferpa",
            "state_breach_generic",
        }

    def test_non_yaml_files_skipped(self, tmp_path):
        # Create a valid YAML and a non-YAML file
        yaml_content = textwrap.dedent("""\
            protocol_id: valid
            name: Valid
            jurisdiction: TEST
            triggering_entity_types: [EMAIL]
            notification_threshold: 1
            notification_deadline_days: 30
            required_notification_content: [desc]
            regulatory_framework: "Test"
        """)
        (tmp_path / "valid.yaml").write_text(yaml_content)
        (tmp_path / "readme.txt").write_text("not a protocol")
        (tmp_path / ".gitkeep").write_text("")

        protocols = load_all_protocols(tmp_path)
        assert len(protocols) == 1
        assert protocols[0].protocol_id == "valid"


# ===========================================================================
# ProtocolRegistry
# ===========================================================================

class TestProtocolRegistry:
    def test_get_returns_correct_protocol(self):
        registry = ProtocolRegistry.default()
        p = registry.get("hipaa_breach_rule")
        assert p.protocol_id == "hipaa_breach_rule"
        assert p.regulatory_framework == "45 CFR §164.400-414"

    def test_get_nonexistent_raises_keyerror(self):
        registry = ProtocolRegistry.default()
        with pytest.raises(KeyError, match="nonexistent"):
            registry.get("nonexistent")

    def test_list_all_returns_six_sorted(self):
        registry = ProtocolRegistry.default()
        protocols = registry.list_all()
        assert len(protocols) == 6
        ids = [p.protocol_id for p in protocols]
        assert ids == sorted(ids)

    def test_gdpr_has_individual_deadline_days(self):
        registry = ProtocolRegistry.default()
        gdpr = registry.get("gdpr_article_33")
        assert gdpr.individual_deadline_days == 30

    def test_hitech_requires_hhs_notification(self):
        registry = ProtocolRegistry.default()
        hitech = registry.get("hitech")
        assert hitech.requires_hhs_notification is True

    def test_register_adds_protocol(self):
        registry = ProtocolRegistry()
        p = _hipaa()
        registry.register(p)
        assert registry.get("hipaa_breach_rule") is p

    def test_register_replaces_existing(self):
        registry = ProtocolRegistry.default()
        replacement = _hipaa()
        replacement.name = "Replaced"
        registry.register(replacement)
        assert registry.get("hipaa_breach_rule").name == "Replaced"


# ===========================================================================
# Helpers for regulatory threshold tests
# ===========================================================================

@pytest.fixture()
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Session() as session:
        yield session


def _subject(
    pii_types: list[str],
    **kwargs,
) -> NotificationSubject:
    defaults = dict(
        subject_id=uuid4(),
        pii_types_found=pii_types,
        notification_required=False,
        review_status="AI_PENDING",
    )
    defaults.update(kwargs)
    return NotificationSubject(**defaults)


# ===========================================================================
# apply_protocol
# ===========================================================================

class TestApplyProtocol:
    def test_matching_type_triggers(self):
        subject = _subject(["US_SSN", "EMAIL"])
        required, triggered = apply_protocol(subject, _hipaa())
        assert required is True
        assert "US_SSN" in triggered

    def test_no_matching_type(self):
        subject = _subject(["EMAIL", "PHONE"])
        required, triggered = apply_protocol(subject, _hipaa())
        assert required is False
        assert triggered == []

    def test_case_insensitive(self):
        subject = _subject(["us_ssn"])
        required, triggered = apply_protocol(subject, _hipaa())
        assert required is True

    def test_triggered_by_is_sorted(self):
        subject = _subject(["PHI_NPI", "US_SSN", "PHI_MRN"])
        _, triggered = apply_protocol(subject, _hipaa())
        assert triggered == sorted(triggered)

    def test_empty_pii_types(self):
        subject = _subject([])
        required, triggered = apply_protocol(subject, _hipaa())
        assert required is False
        assert triggered == []

    def test_none_pii_types(self):
        subject = _subject([])
        subject.pii_types_found = None
        required, triggered = apply_protocol(subject, _hipaa())
        assert required is False
        assert triggered == []

    def test_threshold_gt_1(self):
        """Protocol with threshold=2 requires at least 2 matching types."""
        protocol = Protocol(
            protocol_id="strict",
            name="Strict",
            jurisdiction="TEST",
            triggering_entity_types=["US_SSN", "CREDIT_CARD", "EMAIL"],
            notification_threshold=2,
            notification_deadline_days=30,
            required_notification_content=["desc"],
            regulatory_framework="Test",
        )
        # Only 1 match — below threshold
        subject_one = _subject(["US_SSN", "PHONE"])
        required, triggered = apply_protocol(subject_one, protocol)
        assert required is False
        assert triggered == []

        # 2 matches — meets threshold
        subject_two = _subject(["US_SSN", "CREDIT_CARD"])
        required, triggered = apply_protocol(subject_two, protocol)
        assert required is True
        assert len(triggered) == 2

    def test_multiple_matches_returned(self):
        subject = _subject(["US_SSN", "PHI_MRN", "EMAIL"])
        _, triggered = apply_protocol(subject, _hipaa())
        assert set(triggered) == {"US_SSN", "PHI_MRN"}


# ===========================================================================
# apply_protocol_to_all
# ===========================================================================

class TestApplyProtocolToAll:
    def test_updates_notification_required_in_db(self, db_session):
        s1 = _subject(["US_SSN", "EMAIL"])
        s2 = _subject(["EMAIL", "PHONE"])
        db_session.add_all([s1, s2])
        db_session.flush()

        results = apply_protocol_to_all([s1, s2], _hipaa(), db_session)
        db_session.commit()

        assert results[str(s1.subject_id)] is True
        assert results[str(s2.subject_id)] is False

        # Verify persisted state
        refreshed = db_session.get(NotificationSubject, s1.subject_id)
        assert refreshed.notification_required is True
        refreshed2 = db_session.get(NotificationSubject, s2.subject_id)
        assert refreshed2.notification_required is False

    def test_empty_subjects_list(self, db_session):
        results = apply_protocol_to_all([], _hipaa(), db_session)
        assert results == {}

    def test_all_triggered(self, db_session):
        s1 = _subject(["US_SSN"])
        s2 = _subject(["PHI_MRN"])
        db_session.add_all([s1, s2])
        db_session.flush()

        results = apply_protocol_to_all([s1, s2], _hipaa(), db_session)
        db_session.commit()

        assert all(results.values())

    def test_none_triggered(self, db_session):
        s1 = _subject(["EMAIL"])
        s2 = _subject(["PHONE"])
        db_session.add_all([s1, s2])
        db_session.flush()

        results = apply_protocol_to_all([s1, s2], _hipaa(), db_session)
        db_session.commit()

        assert not any(results.values())
