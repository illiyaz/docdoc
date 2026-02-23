"""Tests for app/rra/entity_resolver.py — Phase 2 entity resolution."""
from __future__ import annotations

import pytest

from app.rra.entity_resolver import (
    PIIRecord,
    ResolvedGroup,
    EntityResolver,
    build_confidence,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rec(
    *,
    record_id: str = "r1",
    entity_type: str = "PERSON",
    normalized_value: str = "",
    raw_name: str | None = None,
    raw_address: dict | None = None,
    raw_phone: str | None = None,
    raw_email: str | None = None,
    raw_dob: str | None = None,
    country: str = "US",
    source_document_id: str = "doc1",
    page_or_sheet: str | int = 0,
) -> PIIRecord:
    return PIIRecord(
        record_id=record_id,
        entity_type=entity_type,
        normalized_value=normalized_value,
        raw_name=raw_name,
        raw_address=raw_address,
        raw_phone=raw_phone,
        raw_email=raw_email,
        raw_dob=raw_dob,
        country=country,
        source_document_id=source_document_id,
        page_or_sheet=page_or_sheet,
    )


def _addr(
    street="123 main st",
    city="springfield",
    state="IL",
    zip_code="62701",
    country="US",
) -> dict:
    return {
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "country": country,
    }


# ===========================================================================
# build_confidence
# ===========================================================================

class TestBuildConfidence:
    def test_ssn_match_gives_050(self):
        r1 = _rec(entity_type="US_SSN", normalized_value="123-45-6789")
        r2 = _rec(entity_type="US_SSN", normalized_value="123-45-6789", record_id="r2")
        assert build_confidence(r1, r2) == pytest.approx(0.50)

    def test_email_match_gives_040(self):
        r1 = _rec(raw_email="alice@example.com")
        r2 = _rec(raw_email="alice@example.com", record_id="r2")
        assert build_confidence(r1, r2) == pytest.approx(0.40)

    def test_phone_match_gives_035(self):
        r1 = _rec(raw_phone="+15551234567")
        r2 = _rec(raw_phone="+15551234567", record_id="r2")
        assert build_confidence(r1, r2) == pytest.approx(0.35)

    def test_name_dob_match_gives_045(self):
        """Name + DOB = 0.35 + name-alone 0.10 = 0.45."""
        r1 = _rec(raw_name="John Smith", raw_dob="1990-01-15")
        r2 = _rec(raw_name="John Smith", raw_dob="1990-01-15", record_id="r2")
        assert build_confidence(r1, r2) == pytest.approx(0.45)

    def test_name_address_match_gives_035(self):
        """Name + address = 0.25 + name-alone 0.10 = 0.35."""
        r1 = _rec(raw_name="John Smith", raw_address=_addr())
        r2 = _rec(raw_name="John Smith", raw_address=_addr(), record_id="r2")
        assert build_confidence(r1, r2) == pytest.approx(0.35)

    def test_name_only_gives_010(self):
        r1 = _rec(raw_name="John Smith")
        r2 = _rec(raw_name="John Smith", record_id="r2")
        assert build_confidence(r1, r2) == pytest.approx(0.10)

    def test_no_signal_gives_zero(self):
        r1 = _rec()
        r2 = _rec(record_id="r2")
        assert build_confidence(r1, r2) == 0.0

    def test_multiple_signals_stack(self):
        """Email (0.40) + name+DOB (0.35+0.10) = 0.85."""
        r1 = _rec(
            raw_email="alice@example.com",
            raw_name="Alice Jones",
            raw_dob="1985-03-20",
        )
        r2 = _rec(
            raw_email="alice@example.com",
            raw_name="Alice Jones",
            raw_dob="1985-03-20",
            record_id="r2",
        )
        assert build_confidence(r1, r2) == pytest.approx(0.85)

    def test_cap_at_1_0(self):
        """SSN(0.50) + email(0.40) + phone(0.35) + name+DOB(0.45) > 1.0."""
        r1 = _rec(
            entity_type="US_SSN",
            normalized_value="123-45-6789",
            raw_email="a@b.com",
            raw_phone="+15551234567",
            raw_name="John Smith",
            raw_dob="1990-01-15",
        )
        r2 = _rec(
            entity_type="US_SSN",
            normalized_value="123-45-6789",
            raw_email="a@b.com",
            raw_phone="+15551234567",
            raw_name="John Smith",
            raw_dob="1990-01-15",
            record_id="r2",
        )
        assert build_confidence(r1, r2) == 1.0

    def test_email_case_insensitive(self):
        r1 = _rec(raw_email="Alice@Example.COM")
        r2 = _rec(raw_email="alice@example.com", record_id="r2")
        assert build_confidence(r1, r2) == pytest.approx(0.40)

    def test_gmail_dot_normalized_match(self):
        """Gmail dots are insignificant: j.smith@gmail.com == jsmith@gmail.com."""
        r1 = _rec(raw_email="j.smith@gmail.com")
        r2 = _rec(raw_email="jsmith@gmail.com", record_id="r2")
        assert build_confidence(r1, r2) == pytest.approx(0.40)

    def test_gmail_dot_and_case_normalized_match(self):
        """Case + gmail dot normalization combined."""
        r1 = _rec(raw_email="J.SMITH@GMAIL.COM")
        r2 = _rec(raw_email="jsmith@gmail.com", record_id="r2")
        assert build_confidence(r1, r2) == pytest.approx(0.40)

    def test_phone_none_no_match(self):
        r1 = _rec(raw_phone="+15551234567")
        r2 = _rec(raw_phone=None, record_id="r2")
        assert build_confidence(r1, r2) == 0.0

    def test_different_gov_id_types_no_match(self):
        r1 = _rec(entity_type="US_SSN", normalized_value="123-45-6789")
        r2 = _rec(entity_type="US_PASSPORT", normalized_value="123-45-6789", record_id="r2")
        assert build_confidence(r1, r2) == 0.0

    def test_names_dont_match_no_name_signals(self):
        """Different names → no name-dependent signals fire."""
        r1 = _rec(raw_name="Alice Zhang", raw_dob="1990-01-01")
        r2 = _rec(raw_name="Bob Johnson", raw_dob="1990-01-01", record_id="r2")
        assert build_confidence(r1, r2) == 0.0

    def test_email_plus_name_dob_stacks_to_075(self):
        """Email(0.40) + name+DOB(0.35) + name(0.10) = 0.85 — but
        without DOB: email(0.40) + name(0.10) = 0.50."""
        r1 = _rec(raw_email="a@b.com", raw_name="John Smith")
        r2 = _rec(raw_email="a@b.com", raw_name="John Smith", record_id="r2")
        assert build_confidence(r1, r2) == pytest.approx(0.50)


# ===========================================================================
# EntityResolver.resolve
# ===========================================================================

class TestResolve:
    def setup_method(self):
        self.resolver = EntityResolver()

    def test_empty_input(self):
        assert self.resolver.resolve([]) == []

    def test_single_record_one_group(self):
        r = _rec(record_id="r1")
        groups = self.resolver.resolve([r])
        assert len(groups) == 1
        assert len(groups[0].records) == 1
        assert groups[0].needs_human_review is False

    def test_two_records_same_ssn_one_group(self):
        r1 = _rec(entity_type="US_SSN", normalized_value="123-45-6789", record_id="r1")
        r2 = _rec(entity_type="US_SSN", normalized_value="123-45-6789", record_id="r2")
        groups = self.resolver.resolve([r1, r2])
        merged = [g for g in groups if len(g.records) > 1]
        assert len(merged) == 1
        assert {r.record_id for r in merged[0].records} == {"r1", "r2"}

    def test_transitive_merge_via_union_find(self):
        """A↔B share email, B↔C share phone → all three in one group."""
        a = _rec(record_id="a", raw_email="shared@x.com")
        b = _rec(record_id="b", raw_email="shared@x.com", raw_phone="+15551234567")
        c = _rec(record_id="c", raw_phone="+15551234567")
        groups = self.resolver.resolve([a, b, c])
        merged = [g for g in groups if len(g.records) == 3]
        assert len(merged) == 1
        assert {r.record_id for r in merged[0].records} == {"a", "b", "c"}

    def test_below_threshold_separate_groups(self):
        """Name-only match (0.10) is below 0.60 → separate groups."""
        r1 = _rec(record_id="r1", raw_name="John Smith")
        r2 = _rec(record_id="r2", raw_name="John Smith")
        groups = self.resolver.resolve([r1, r2])
        assert len(groups) == 2

    def test_needs_human_review_true_below_080(self):
        """Email match (0.40) + name(0.10) = 0.50 is below threshold, so
        they won't merge. Use phone(0.35) + name(0.10) = 0.45 — also no merge.
        Use email-only (0.40) + phone(0.35) = would need both on same record.
        Actually: two records sharing email = 0.40 → below 0.60 → no merge.
        Let's use a confidence of 0.65: email(0.40) + name+address(0.25+0.10) = 0.75.
        Wait, that's 0.75. Let's just get 0.65 exactly:
        phone(0.35) + name+address(0.25+0.10) = 0.70."""
        r1 = _rec(
            record_id="r1",
            raw_phone="+15551234567",
            raw_name="John Smith",
            raw_address=_addr(),
        )
        r2 = _rec(
            record_id="r2",
            raw_phone="+15551234567",
            raw_name="John Smith",
            raw_address=_addr(),
        )
        groups = self.resolver.resolve([r1, r2])
        merged = [g for g in groups if len(g.records) > 1]
        assert len(merged) == 1
        assert merged[0].merge_confidence == pytest.approx(0.70)
        assert merged[0].needs_human_review is True

    def test_needs_human_review_false_above_080(self):
        """Email(0.40) + phone(0.35) + name(0.10) = 0.85 → no review."""
        r1 = _rec(
            record_id="r1",
            raw_email="a@b.com",
            raw_phone="+15551234567",
            raw_name="John Smith",
        )
        r2 = _rec(
            record_id="r2",
            raw_email="a@b.com",
            raw_phone="+15551234567",
            raw_name="John Smith",
        )
        groups = self.resolver.resolve([r1, r2])
        merged = [g for g in groups if len(g.records) > 1]
        assert len(merged) == 1
        assert merged[0].merge_confidence >= 0.80
        assert merged[0].needs_human_review is False

    def test_different_countries_same_postal_not_matched(self):
        """Addresses with different countries don't match, so name+address
        doesn't fire — only name-alone (0.10) which is below threshold."""
        addr_us = _addr(country="US")
        addr_gb = _addr(country="GB")
        r1 = _rec(record_id="r1", raw_name="John Smith", raw_address=addr_us)
        r2 = _rec(record_id="r2", raw_name="John Smith", raw_address=addr_gb)
        groups = self.resolver.resolve([r1, r2])
        # Name-only = 0.10 < 0.60, so they stay separate
        assert len(groups) == 2

    def test_group_ids_are_unique(self):
        r1 = _rec(record_id="r1")
        r2 = _rec(record_id="r2")
        groups = self.resolver.resolve([r1, r2])
        ids = [g.group_id for g in groups]
        assert len(ids) == len(set(ids))

    def test_single_record_group_confidence_is_1(self):
        r = _rec(record_id="r1")
        groups = self.resolver.resolve([r])
        assert groups[0].merge_confidence == 1.0

    def test_four_records_two_clusters(self):
        """A↔B share SSN; C↔D share email. Two separate groups."""
        a = _rec(record_id="a", entity_type="US_SSN", normalized_value="111-11-1111")
        b = _rec(record_id="b", entity_type="US_SSN", normalized_value="111-11-1111")
        c = _rec(record_id="c", raw_email="x@y.com", raw_phone="+19999999999")
        d = _rec(record_id="d", raw_email="x@y.com", raw_phone="+19999999999")
        groups = self.resolver.resolve([a, b, c, d])
        merged = [g for g in groups if len(g.records) > 1]
        assert len(merged) == 2
        ids_per_group = [
            {r.record_id for r in g.records} for g in merged
        ]
        assert {"a", "b"} in ids_per_group
        assert {"c", "d"} in ids_per_group

    def test_merge_confidence_is_min_pairwise(self):
        """Three records: A↔B via SSN (0.50+), B↔C via email+name (0.50).
        The group's merge_confidence should be the minimum."""
        a = _rec(
            record_id="a",
            entity_type="US_SSN",
            normalized_value="123-45-6789",
            raw_name="Alice Jones",
            raw_email="alice@x.com",
        )
        b = _rec(
            record_id="b",
            entity_type="US_SSN",
            normalized_value="123-45-6789",
            raw_name="Alice Jones",
            raw_email="alice@x.com",
        )
        c = _rec(
            record_id="c",
            raw_name="Alice Jones",
            raw_email="alice@x.com",
        )
        # A↔B: SSN(0.50) + email(0.40) + name(0.10) = 1.0
        # B↔C: email(0.40) + name(0.10) = 0.50 → below 0.60, won't merge!
        # So let's add phone to make B↔C above threshold
        b2 = _rec(
            record_id="b",
            entity_type="US_SSN",
            normalized_value="123-45-6789",
            raw_name="Alice Jones",
            raw_email="alice@x.com",
            raw_phone="+15551234567",
        )
        c2 = _rec(
            record_id="c",
            raw_name="Alice Jones",
            raw_email="alice@x.com",
            raw_phone="+15551234567",
        )
        # A↔B2: SSN(0.50) + email(0.40) + phone(0.35) + name(0.10) = capped 1.0
        # B2↔C2: email(0.40) + phone(0.35) + name(0.10) = 0.85
        # A↔C2: email(0.40) + phone(?—A has no phone) + name(0.10) = 0.50 → indirect
        a2 = _rec(
            record_id="a",
            entity_type="US_SSN",
            normalized_value="123-45-6789",
            raw_name="Alice Jones",
            raw_email="alice@x.com",
            raw_phone="+15551234567",
        )
        # Now A2↔C2: email(0.40) + phone(0.35) + name(0.10) = 0.85
        groups = self.resolver.resolve([a2, b2, c2])
        merged = [g for g in groups if len(g.records) == 3]
        assert len(merged) == 1
        # min of {A2↔B2=1.0, B2↔C2=0.85, A2↔C2=0.85} = 0.85
        assert merged[0].merge_confidence == pytest.approx(0.85)
