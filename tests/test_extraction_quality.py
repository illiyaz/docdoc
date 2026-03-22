"""Tests for extraction quality gates.

Gate 1: is_likely_name() — rejects headers, accepts real names
Gate 2: static_filter same-value PERSON guard
Gate 3: Expanded blocklist coverage
Integration: two_phase.py wiring verification
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Gate 1: is_likely_name()
# ---------------------------------------------------------------------------

from app.pipeline.person_discovery import is_likely_name, NAME_BLOCKLIST


class TestIsLikelyName:
    """The CMG killer: 'January Statement' should return False."""

    # --- Rejections (the whole point) ---

    def test_january_statement_rejected(self):
        assert not is_likely_name("January Statement")

    def test_report_summary_rejected(self):
        assert not is_likely_name("Report Summary")

    def test_account_criteria_rejected(self):
        assert not is_likely_name("Account Criteria")

    def test_page_header_rejected(self):
        assert not is_likely_name("Statement Summary")

    def test_single_word_rejected(self):
        assert not is_likely_name("Washington")

    def test_too_short_rejected(self):
        assert not is_likely_name("AB")

    def test_too_long_rejected(self):
        assert not is_likely_name("A" * 61)

    def test_contains_digits_rejected(self):
        assert not is_likely_name("John Smith 123")

    def test_all_short_words_rejected(self):
        assert not is_likely_name("A B C D E")

    def test_company_name_rejected(self):
        assert not is_likely_name("National Holdings Inc")

    def test_address_word_rejected(self):
        assert not is_likely_name("Park Avenue")

    def test_state_abbreviation_rejected(self):
        assert not is_likely_name("CA NY")

    def test_trust_rejected(self):
        assert not is_likely_name("Revocable Trust")

    def test_insurance_rejected(self):
        assert not is_likely_name("Federal Insurance")

    def test_balance_statement_rejected(self):
        assert not is_likely_name("Balance Statement")

    def test_checking_savings_rejected(self):
        assert not is_likely_name("Checking Savings")

    # --- Acceptances (real names must pass) ---

    def test_first_last_accepted(self):
        assert is_likely_name("ADELINE CHANDLER")

    def test_last_first_accepted(self):
        assert is_likely_name("CHANDLER, ADELINE")

    def test_three_part_name_accepted(self):
        assert is_likely_name("JOHN MICHAEL SMITH")

    def test_mixed_case_accepted(self):
        assert is_likely_name("John Smith")

    def test_hyphenated_accepted(self):
        assert is_likely_name("Smith-Jones, Mary")

    def test_titled_accepted(self):
        assert is_likely_name("Dr. Barbara Jones")

    def test_suffix_accepted(self):
        # In the pipeline, _clean_name() strips suffixes before is_likely_name runs
        # So test the cleaned version
        assert is_likely_name("JAMES WILSON")

    def test_unicode_accepted(self):
        assert is_likely_name("José García")

    def test_apostrophe_accepted(self):
        assert is_likely_name("O'Brien McDonald")


# ---------------------------------------------------------------------------
# Gate 1: discover_person_from_text()
# ---------------------------------------------------------------------------

from app.pipeline.person_discovery import discover_person_from_text


class TestDiscoverPersonFromText:

    def test_finds_names_on_nearby_pages(self, tmp_path):
        """Should find name patterns on pages near onset."""
        import fitz
        doc = fitz.open()

        # Page 0: cover page (no names)
        p0 = doc.new_page()
        p0.insert_text((50, 50), "Report Summary\nAccount Criteria\nJanuary Statement")

        # Page 1: data page with names
        p1 = doc.new_page()
        p1.insert_text((50, 50), "SMITH, JOHN\nDOE, JANE\nWILSON, ROBERT\nSSN: 123-45-6789")

        # Page 2: another data page
        p2 = doc.new_page()
        p2.insert_text((50, 50), "JONES, MARY\nBROWN, DAVID\nAmount: $500.00")

        path = str(tmp_path / "test.pdf")
        doc.save(path)
        doc.close()

        found, best_page = discover_person_from_text(path, onset=0)
        assert len(found) >= 2
        assert all(f["type"] == "PERSON" for f in found)
        assert best_page != 0  # Should have found names on page 1 or 2

    def test_rejects_non_name_patterns(self, tmp_path):
        """Pages with only report headers should return empty."""
        import fitz
        doc = fitz.open()

        for i in range(3):
            p = doc.new_page()
            p.insert_text((50, 50), f"Page {i}\nReport Summary\nJanuary Statement\nTotal Balance: $1000")

        path = str(tmp_path / "nonames.pdf")
        doc.save(path)
        doc.close()

        found, _ = discover_person_from_text(path, onset=0)
        assert found == []

    def test_invalid_path_returns_empty(self):
        found, onset = discover_person_from_text("/nonexistent/file.pdf", onset=0)
        assert found == []
        assert onset == 0


# ---------------------------------------------------------------------------
# Gate 2: static_filter same-value PERSON guard
# ---------------------------------------------------------------------------

from app.pipeline.static_filter import filter_static_values, _NEVER_FILTER, _PERSON_STATIC_THRESHOLD


class TestStaticFilterPersonGuard:
    """The second line of defense: if Gate 1 fails, catch it here."""

    def test_person_not_in_never_filter(self):
        """PERSON should no longer be in _NEVER_FILTER."""
        assert "PERSON" not in _NEVER_FILTER

    def test_ssn_still_in_never_filter(self):
        assert "US_SSN" in _NEVER_FILTER

    def test_gov_id_still_in_never_filter(self):
        assert "GOVERNMENT_ID" in _NEVER_FILTER

    def test_same_person_on_all_pages_removed(self):
        """1354 'January Statement' records should be caught."""
        page_records = {
            i: [{"PERSON": "January Statement", "DATE_OF_BIRTH": f"01/{i:02d}/1980"}]
            for i in range(100)
        }
        cleaned, removed = filter_static_values(page_records, threshold=0.5, min_pages=5)
        assert "PERSON" in removed
        assert "January Statement" in removed["PERSON"]

    def test_person_threshold_is_80_percent(self):
        """PERSON should only be filtered at 80% threshold, not 50%."""
        assert _PERSON_STATIC_THRESHOLD == 0.8

    def test_diverse_names_not_filtered(self):
        """Different names on each page should NOT be filtered."""
        page_records = {
            i: [{"PERSON": f"Person {i}", "DATE_OF_BIRTH": "03/15/2026"}]
            for i in range(20)
        }
        cleaned, removed = filter_static_values(page_records, threshold=0.5, min_pages=5)
        assert "PERSON" not in removed
        # All records should still have their names
        assert all("PERSON" in recs[0] for recs in cleaned.values())

    def test_person_at_70_percent_not_filtered(self):
        """PERSON appearing on 70% of pages should NOT be filtered (threshold is 80%)."""
        page_records = {}
        for i in range(10):
            name = "January Statement" if i < 7 else f"Real Person {i}"
            page_records[i] = [{"PERSON": name}]
        cleaned, removed = filter_static_values(page_records, threshold=0.5, min_pages=5)
        # 70% < 80% threshold → PERSON not filtered
        assert "PERSON" not in removed

    def test_person_at_90_percent_filtered(self):
        """PERSON appearing on 90% of pages SHOULD be filtered."""
        page_records = {}
        for i in range(10):
            name = "January Statement" if i < 9 else "Real Person"
            page_records[i] = [{"PERSON": name}]
        cleaned, removed = filter_static_values(page_records, threshold=0.5, min_pages=5)
        assert "PERSON" in removed


# ---------------------------------------------------------------------------
# Gate 3: Blocklist coverage
# ---------------------------------------------------------------------------

from app.pipeline.coordinate_extractor import _NAME_BLOCKLIST


class TestBlocklistCoverage:
    """Verify blocklist has critical entries from standalone."""

    def test_statement_in_blocklist(self):
        assert "STATEMENT" in _NAME_BLOCKLIST

    def test_january_word_not_needed(self):
        """'JANUARY' isn't in blocklist, but 'STATEMENT' blocks 'January Statement'."""
        assert "STATEMENT" in _NAME_BLOCKLIST

    def test_state_abbreviations(self):
        for st in ("CA", "NY", "TX", "FL", "IL"):
            assert st in _NAME_BLOCKLIST, f"{st} missing from blocklist"

    def test_trust_terms(self):
        for term in ("TRUST", "TRUSTEE", "CUSTODIAN", "BENEFICIARY", "ESTATE"):
            assert term in _NAME_BLOCKLIST, f"{term} missing from blocklist"

    def test_financial_terms(self):
        for term in ("CHECKING", "SAVINGS", "ADVICE", "INCOME"):
            assert term in _NAME_BLOCKLIST, f"{term} missing from blocklist"

    def test_blocklist_size(self):
        """Production blocklist should be at least 300 words."""
        assert len(_NAME_BLOCKLIST) >= 300

    def test_person_discovery_blocklist_covers_production(self):
        """person_discovery.NAME_BLOCKLIST should be a superset of coordinate_extractor._NAME_BLOCKLIST."""
        # person_discovery includes suffix words (JR, SR, etc.) which
        # coordinate_extractor keeps in a separate _SUFFIX_WORDS set
        missing = _NAME_BLOCKLIST - NAME_BLOCKLIST
        assert not missing, f"Production blocklist has words not in person_discovery: {missing}"


# ---------------------------------------------------------------------------
# Integration: two_phase.py wiring
# ---------------------------------------------------------------------------

class TestTwoPhaseWiring:
    """Verify all three gates are wired into the production pipeline."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        self.src = (pathlib.Path(__file__).parent.parent / "app" / "pipeline" / "two_phase.py").read_text()

    def test_person_discovery_import(self):
        assert "from app.pipeline.person_discovery import" in self.src

    def test_is_likely_name_called(self):
        assert "is_likely_name" in self.src

    def test_discover_person_from_text_called(self):
        assert "discover_person_from_text" in self.src

    def test_validation_before_field_map(self):
        """PERSON validation must happen BEFORE field map building."""
        val_pos = self.src.index("is_likely_name")
        build_pos = self.src.index("builder.build_field_map")
        assert val_pos < build_pos

    def test_presidio_downgrade_on_no_person(self):
        """If no valid PERSON found, should downgrade to presidio."""
        assert 'routing.recommended_path = "presidio"' in self.src

    def test_static_filter_handles_person(self):
        """Static filter should null out raw_name for static PERSON."""
        assert '("PERSON", rec.raw_name)' in self.src

    def test_static_person_removes_empty_records(self):
        """Records that lost their static PERSON should be removed."""
        assert '"PERSON" in removed_static' in self.src