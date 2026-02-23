"""Tests for app/pii/layer2_context.py and app/pii/layer3_positional.py.

Covers:
- Layer 2: score boosted when signal keyword is in context window
- Layer 2: score unchanged when no signal keyword found
- Layer 2: score capped at 1.0
- Layer 2: extraction_layer always "layer_2_context"
- Layer 2: needs_layer2 recalculated from new score
- Layer 2: context window boundaries respected
- Layer 2: unknown entity type → no boost
- Layer 2: safety — raw text not logged
- Layer 3: returns None when col_header is None
- Layer 3: returns None when no keyword matches
- Layer 3: correct entity_type for matching keywords
- Layer 3: longest keyword wins over shorter prefix
- Layer 3: score boost applied and capped at 1.0
- Layer 3: extraction_layer = "layer_3_positional"
- Layer 3: pattern_used = "header:<keyword>"
- Layer 3: [REVIEW] prefix stripped before matching
- Layer 3: case-insensitive matching
- Layer 3: safety — only header metadata logged (not values)
"""
from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy external deps before any project import
# ---------------------------------------------------------------------------
for _mod in (
    "presidio_analyzer",
    "presidio_analyzer.nlp_engine",
    "presidio_analyzer.pattern",
    "presidio_analyzer.pattern_recognizer",
    "spacy",
):
    sys.modules.setdefault(_mod, MagicMock())

from app.readers.base import ExtractedBlock  # noqa: E402
from app.pii.presidio_engine import DetectionResult  # noqa: E402
from app.pii.layer2_context import (  # noqa: E402
    Layer2ContextClassifier,
    CONFIDENCE_THRESHOLD,
    _BOOST_AMOUNT,
    _CONTEXT_WINDOW_CHARS,
)
from app.pii.layer3_positional import (  # noqa: E402
    Layer3PositionalInference,
    HEADER_KEYWORDS,
    _SCORE_BOOST,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _block(text: str = "x", col_header: str | None = None) -> ExtractedBlock:
    return ExtractedBlock(
        text=text,
        page_or_sheet=1,
        source_path="/f.pdf",
        file_type="pdf",
        block_type="prose" if col_header is None else "table_cell",
        col_header=col_header,
    )


def _result(
    block: ExtractedBlock | None = None,
    entity_type: str = "SSN",
    start: int = 0,
    end: int = 11,
    score: float = 0.60,
    pattern_used: str = r"\d{3}-\d{2}-\d{4}",
    geography: str = "US",
    regulatory_framework: str = "HIPAA",
    extraction_layer: str = "layer_1_pattern",
) -> DetectionResult:
    b = block or _block("123-45-6789 appears here")
    return DetectionResult(
        block=b,
        entity_type=entity_type,
        start=start,
        end=end,
        score=score,
        pattern_used=pattern_used,
        geography=geography,
        regulatory_framework=regulatory_framework,
        extraction_layer=extraction_layer,
    )


# ===========================================================================
# Layer 2 — context window classifier
# ===========================================================================

class TestLayer2ContextClassifier:

    # -----------------------------------------------------------------------
    # extraction_layer is always "layer_2_context"
    # -----------------------------------------------------------------------

    def test_extraction_layer_always_layer_2_context(self):
        clf = Layer2ContextClassifier()
        r = _result(score=0.60)
        block_text = r.block.text
        out = clf.classify(r, block_text)
        assert out.extraction_layer == "layer_2_context"

    def test_extraction_layer_even_when_no_boost(self):
        clf = Layer2ContextClassifier()
        r = _result(entity_type="SSN", score=0.60)
        out = clf.classify(r, "unrelated words around the match")
        assert out.extraction_layer == "layer_2_context"

    # -----------------------------------------------------------------------
    # Score boosted when signal keyword is present
    # -----------------------------------------------------------------------

    def test_score_boosted_when_signal_in_context(self):
        clf = Layer2ContextClassifier()
        full_text = "SSN: 123-45-6789 for the employee"
        r = _result(entity_type="SSN", start=5, end=16, score=0.60)
        out = clf.classify(r, full_text)
        assert out.score == pytest.approx(0.60 + _BOOST_AMOUNT)

    def test_score_unchanged_when_no_signal_in_context(self):
        clf = Layer2ContextClassifier()
        full_text = "value is 123-45-6789 something else"
        r = _result(entity_type="SSN", start=9, end=20, score=0.60)
        out = clf.classify(r, full_text)
        assert out.score == pytest.approx(0.60)

    # -----------------------------------------------------------------------
    # Score capped at 1.0
    # -----------------------------------------------------------------------

    def test_score_capped_at_1_0(self):
        clf = Layer2ContextClassifier()
        full_text = "SSN 123-45-6789"
        r = _result(entity_type="SSN", start=4, end=15, score=0.95)
        out = clf.classify(r, full_text)
        assert out.score <= 1.0

    def test_score_exactly_1_0_when_capped(self):
        clf = Layer2ContextClassifier()
        full_text = "social security 123-45-6789"
        r = _result(entity_type="SSN", start=16, end=27, score=0.90)
        out = clf.classify(r, full_text)
        assert out.score == pytest.approx(1.0)

    # -----------------------------------------------------------------------
    # needs_layer2 recalculated from new score
    # -----------------------------------------------------------------------

    def test_needs_layer2_false_after_boost_above_threshold(self):
        clf = Layer2ContextClassifier()
        full_text = "ssn: 123-45-6789"
        # 0.60 + 0.20 = 0.80 >= 0.75 → needs_layer2 = False
        r = _result(entity_type="SSN", start=5, end=16, score=0.60)
        out = clf.classify(r, full_text)
        assert out.score == pytest.approx(0.80)
        assert out.needs_layer2 is False

    def test_needs_layer2_true_when_score_still_below_threshold(self):
        clf = Layer2ContextClassifier()
        full_text = "unrelated content 123-45-6789"
        r = _result(entity_type="SSN", start=18, end=29, score=0.50)
        out = clf.classify(r, full_text)
        # No boost → 0.50 < 0.75 → needs_layer2 = True
        assert out.needs_layer2 is True

    def test_needs_layer2_false_when_already_above_threshold(self):
        clf = Layer2ContextClassifier()
        full_text = "unrelated 123-45-6789"
        r = _result(entity_type="SSN", start=10, end=21, score=0.80)
        out = clf.classify(r, full_text)
        assert out.needs_layer2 is False

    # -----------------------------------------------------------------------
    # Context window boundaries
    # -----------------------------------------------------------------------

    def test_signal_keyword_exactly_at_window_boundary_counts(self):
        clf = Layer2ContextClassifier()
        # "ssn" placed exactly _CONTEXT_WINDOW_CHARS chars before match start
        prefix = "ssn" + " " * (_CONTEXT_WINDOW_CHARS - 3)  # 100 chars total before match
        full_text = prefix + "123-45-6789"
        match_start = len(prefix)
        r = _result(entity_type="SSN", start=match_start, end=match_start + 11, score=0.60)
        out = clf.classify(r, full_text)
        # Signal is within window → boost
        assert out.score > 0.60

    def test_signal_keyword_beyond_window_not_counted(self):
        clf = Layer2ContextClassifier()
        # "ssn" placed 200 chars before match start — beyond 100-char window
        prefix = "ssn" + " " * 200
        full_text = prefix + "123-45-6789"
        match_start = len(prefix)
        r = _result(entity_type="SSN", start=match_start, end=match_start + 11, score=0.60)
        out = clf.classify(r, full_text)
        assert out.score == pytest.approx(0.60)

    # -----------------------------------------------------------------------
    # Unknown entity type → no boost
    # -----------------------------------------------------------------------

    def test_unknown_entity_type_no_boost(self):
        clf = Layer2ContextClassifier()
        full_text = "foobar UNKNOWN_TYPE_VALUE something"
        r = _result(entity_type="UNKNOWN_ENTITY_XYZ", start=7, end=27, score=0.60)
        out = clf.classify(r, full_text)
        assert out.score == pytest.approx(0.60)

    # -----------------------------------------------------------------------
    # Entity type preserved
    # -----------------------------------------------------------------------

    def test_entity_type_unchanged(self):
        clf = Layer2ContextClassifier()
        r = _result(entity_type="EMAIL_ADDRESS", score=0.60)
        out = clf.classify(r, "send email to match@example.com")
        assert out.entity_type == "EMAIL_ADDRESS"

    # -----------------------------------------------------------------------
    # Other fields preserved
    # -----------------------------------------------------------------------

    def test_pattern_used_preserved(self):
        clf = Layer2ContextClassifier()
        r = _result(pattern_used=r"\S+@\S+\.\S+", entity_type="EMAIL_ADDRESS", score=0.60)
        out = clf.classify(r, "email match@x.com here")
        assert out.pattern_used == r"\S+@\S+\.\S+"

    def test_geography_preserved(self):
        clf = Layer2ContextClassifier()
        r = _result(geography="UK", score=0.60)
        out = clf.classify(r, "plain context")
        assert out.geography == "UK"

    def test_block_reference_preserved(self):
        clf = Layer2ContextClassifier()
        b = _block("phone 555-867-5309 here")
        r = _result(block=b, entity_type="PHONE_NUMBER", start=6, end=18, score=0.60)
        out = clf.classify(r, b.text)
        assert out.block is b

    # -----------------------------------------------------------------------
    # Safety: raw text not logged
    # -----------------------------------------------------------------------

    def test_safety_raw_text_not_logged(self):
        clf = Layer2ContextClassifier()
        sensitive_text = "SSN 123-45-6789 belongs to John Doe"
        r = _result(entity_type="SSN", start=4, end=15, score=0.60)
        with patch("app.pii.layer2_context.logger") as mock_logger:
            clf.classify(r, sensitive_text)
        # Ensure sensitive_text itself was never passed to any log call
        for call in mock_logger.debug.call_args_list:
            args = call[0]
            for arg in args:
                assert sensitive_text not in str(arg), (
                    f"Raw text leaked into log: {arg!r}"
                )

    # -----------------------------------------------------------------------
    # Multiple signal keywords — first match wins (boost only once)
    # -----------------------------------------------------------------------

    def test_boost_applied_once_even_with_multiple_signals(self):
        clf = Layer2ContextClassifier()
        full_text = "ssn social security 123-45-6789"
        r = _result(entity_type="SSN", start=20, end=31, score=0.50)
        out = clf.classify(r, full_text)
        # Boost is applied once: 0.50 + 0.20 = 0.70, not 0.90
        assert out.score == pytest.approx(0.50 + _BOOST_AMOUNT)

    # -----------------------------------------------------------------------
    # Phone number signals
    # -----------------------------------------------------------------------

    def test_phone_signal_in_context_boosts_phone_entity(self):
        clf = Layer2ContextClassifier()
        full_text = "mobile: 555-867-5309"
        r = _result(entity_type="PHONE_NUMBER", start=8, end=20, score=0.60)
        out = clf.classify(r, full_text)
        assert out.score > 0.60

    # -----------------------------------------------------------------------
    # Email signals
    # -----------------------------------------------------------------------

    def test_email_signal_boosts_email_entity(self):
        clf = Layer2ContextClassifier()
        full_text = "email address: user@example.com"
        r = _result(entity_type="EMAIL_ADDRESS", start=15, end=31, score=0.60)
        out = clf.classify(r, full_text)
        assert out.score > 0.60


# ===========================================================================
# Layer 3 — positional / header inference
# ===========================================================================

class TestLayer3PositionalInference:

    # -----------------------------------------------------------------------
    # Returns None when col_header is None
    # -----------------------------------------------------------------------

    def test_returns_none_when_col_header_none(self):
        infer = Layer3PositionalInference()
        b = _block(col_header=None)
        r = _result(block=b)
        assert infer.infer(b, r) is None

    def test_returns_none_when_col_header_empty_string(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="")
        r = _result(block=b)
        assert infer.infer(b, r) is None

    # -----------------------------------------------------------------------
    # Returns None when no keyword matches
    # -----------------------------------------------------------------------

    def test_returns_none_when_no_keyword_matches(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="product_sku")
        r = _result(block=b)
        assert infer.infer(b, r) is None

    def test_returns_none_for_numeric_column_header(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="123")
        r = _result(block=b)
        assert infer.infer(b, r) is None

    # -----------------------------------------------------------------------
    # Correct entity_type returned for matching headers
    # -----------------------------------------------------------------------

    def test_ssn_header_returns_ssn_entity(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="SSN")
        r = _result(block=b, entity_type="UNKNOWN", score=0.60)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "SSN"

    def test_name_header_returns_person_entity(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="Employee Name")
        r = _result(block=b, entity_type="UNKNOWN", score=0.60)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "PERSON"

    def test_email_header_returns_email_address_entity(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="Email Address")
        r = _result(block=b, entity_type="UNKNOWN", score=0.50)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "EMAIL_ADDRESS"

    def test_phone_header_returns_phone_number_entity(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="Phone Number")
        r = _result(block=b, entity_type="UNKNOWN", score=0.50)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "PHONE_NUMBER"

    def test_dob_header_returns_date_time_entity(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="DOB")
        r = _result(block=b, entity_type="UNKNOWN", score=0.50)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "DATE_TIME"

    def test_iban_header_returns_financial_account(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="IBAN")
        r = _result(block=b, entity_type="UNKNOWN", score=0.50)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "FINANCIAL_ACCOUNT"

    def test_passport_header_returns_passport_entity(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="Passport No")
        r = _result(block=b)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "PASSPORT"

    # -----------------------------------------------------------------------
    # Longest keyword wins (multi-word before single-word)
    # -----------------------------------------------------------------------

    def test_date_of_birth_wins_over_date(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="Date of Birth")
        r = _result(block=b, entity_type="UNKNOWN", score=0.50)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "DATE_TIME"
        assert "date of birth" in out.pattern_used

    def test_account_number_wins_over_account(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="Account Number")
        r = _result(block=b, entity_type="UNKNOWN", score=0.50)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "FINANCIAL_ACCOUNT"
        assert "account number" in out.pattern_used

    # -----------------------------------------------------------------------
    # Score boosted and capped
    # -----------------------------------------------------------------------

    def test_score_boosted_by_score_boost(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="SSN")
        r = _result(block=b, score=0.60)
        out = infer.infer(b, r)
        assert out is not None
        assert out.score == pytest.approx(0.60 + _SCORE_BOOST)

    def test_score_capped_at_1_0(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="SSN")
        r = _result(block=b, score=0.95)
        out = infer.infer(b, r)
        assert out is not None
        assert out.score <= 1.0

    # -----------------------------------------------------------------------
    # extraction_layer and pattern_used
    # -----------------------------------------------------------------------

    def test_extraction_layer_is_layer_3_positional(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="email")
        r = _result(block=b)
        out = infer.infer(b, r)
        assert out is not None
        assert out.extraction_layer == "layer_3_positional"

    def test_pattern_used_contains_header_prefix(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="ssn")
        r = _result(block=b)
        out = infer.infer(b, r)
        assert out is not None
        assert out.pattern_used.startswith("header:")

    def test_pattern_used_contains_matched_keyword(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="email address")
        r = _result(block=b)
        out = infer.infer(b, r)
        assert out is not None
        assert "email" in out.pattern_used

    # -----------------------------------------------------------------------
    # [REVIEW] prefix stripped before matching
    # -----------------------------------------------------------------------

    def test_review_prefix_stripped_for_ssn(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="[REVIEW] SSN")
        r = _result(block=b, entity_type="UNKNOWN", score=0.60)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "SSN"

    def test_review_prefix_stripped_case_insensitive(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="[REVIEW] phone number")
        r = _result(block=b, entity_type="UNKNOWN", score=0.60)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "PHONE_NUMBER"

    # -----------------------------------------------------------------------
    # Case-insensitive matching
    # -----------------------------------------------------------------------

    def test_case_insensitive_upper(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="EMAIL")
        r = _result(block=b)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "EMAIL_ADDRESS"

    def test_case_insensitive_mixed(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="Social Security Number")
        r = _result(block=b)
        out = infer.infer(b, r)
        assert out is not None
        assert out.entity_type == "SSN"

    # -----------------------------------------------------------------------
    # Block and candidate fields preserved
    # -----------------------------------------------------------------------

    def test_block_reference_is_passed_block(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="SSN")
        r = _result(block=b, start=0, end=11)
        out = infer.infer(b, r)
        assert out is not None
        assert out.block is b

    def test_start_end_offsets_from_candidate(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="SSN")
        r = _result(block=b, start=5, end=16)
        out = infer.infer(b, r)
        assert out is not None
        assert out.start == 5
        assert out.end == 16

    def test_geography_from_candidate(self):
        infer = Layer3PositionalInference()
        b = _block(col_header="SSN")
        r = _result(block=b, geography="US")
        out = infer.infer(b, r)
        assert out is not None
        assert out.geography == "US"

    # -----------------------------------------------------------------------
    # Safety: only header metadata logged (not cell values)
    # -----------------------------------------------------------------------

    def test_safety_cell_value_not_logged(self):
        infer = Layer3PositionalInference()
        sensitive_value = "123-45-6789"
        b = ExtractedBlock(
            text=sensitive_value,
            page_or_sheet=1,
            source_path="/f.xlsx",
            file_type="xlsx",
            block_type="table_cell",
            col_header="SSN",
        )
        r = _result(block=b, entity_type="SSN")
        with patch("app.pii.layer3_positional.logger") as mock_logger:
            infer.infer(b, r)
        for call in mock_logger.debug.call_args_list:
            args = call[0]
            for arg in args:
                assert sensitive_value not in str(arg), (
                    f"Cell value leaked into log: {arg!r}"
                )

    # -----------------------------------------------------------------------
    # HEADER_KEYWORDS dictionary sanity checks
    # -----------------------------------------------------------------------

    def test_header_keywords_all_values_are_strings(self):
        for k, v in HEADER_KEYWORDS.items():
            assert isinstance(v, str), f"HEADER_KEYWORDS[{k!r}] is not a str"

    def test_header_keywords_all_keys_lowercase(self):
        for k in HEADER_KEYWORDS:
            assert k == k.lower(), f"HEADER_KEYWORDS key {k!r} is not lowercase"
