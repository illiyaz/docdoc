"""Layer 3: positional / column-header inference for tabular blocks.

Infers or corroborates PII type from the col_header field pre-attached to
ExtractedBlock by the reader.  Matching is case-insensitive keyword lookup
— never ML.  The matched keyword is recorded as pattern_used in the audit
trail.

Rules (CLAUDE.md § 3)
---------------------
- Never used in isolation — only applies corroborating signal when Layer 1
  or Layer 2 has already produced a candidate result.
- infer() returns None when no HEADER_KEYWORDS match is found.
- Column headers feed this layer; readers must populate col_header.

Safety rule: col_header values are structural metadata (not PII values) and
are safe to include in log output.
"""
from __future__ import annotations

import logging

from app.pii.presidio_engine import DetectionResult
from app.readers.base import ExtractedBlock

logger = logging.getLogger(__name__)

_SCORE_BOOST: float = 0.15
_MAX_SCORE: float = 1.0

# Keyword → canonical PII entity type (case-insensitive substring match).
# Longer keywords must be checked before shorter ones to avoid partial matches
# (e.g. "date of birth" should win over "date").  The lookup is done with
# sorted(key=len, reverse=True) so order here does not matter.
HEADER_KEYWORDS: dict[str, str] = {
    "ssn": "SSN",
    "social security": "SSN",
    "sin": "SSN",
    "full name": "PERSON",
    "first name": "PERSON",
    "last name": "PERSON",
    "name": "PERSON",
    "email address": "EMAIL_ADDRESS",
    "e-mail address": "EMAIL_ADDRESS",
    "e-mail": "EMAIL_ADDRESS",
    "email": "EMAIL_ADDRESS",
    "telephone": "PHONE_NUMBER",
    "mobile": "PHONE_NUMBER",
    "cell": "PHONE_NUMBER",
    "phone": "PHONE_NUMBER",
    "postal code": "LOCATION",
    "zip code": "LOCATION",
    "postal": "LOCATION",
    "address": "LOCATION",
    "addr": "LOCATION",
    "city": "LOCATION",
    "zip": "LOCATION",
    "date of birth": "DATE_TIME",
    "dob": "DATE_TIME",
    "birth": "DATE_TIME",
    "hired": "DATE_TIME",
    "date": "DATE_TIME",
    "iban": "FINANCIAL_ACCOUNT",
    "routing": "FINANCIAL_ACCOUNT",
    "account number": "FINANCIAL_ACCOUNT",
    "account": "FINANCIAL_ACCOUNT",
    "acct": "FINANCIAL_ACCOUNT",
    "policy number": "POLICY_NUMBER",
    "policy": "POLICY_NUMBER",
    "passport": "PASSPORT",
    "driver license": "DRIVER_LICENSE_US",
    "driver licence": "DRIVER_LICENSE_US",
    "license": "DRIVER_LICENSE_US",
    "licence": "DRIVER_LICENSE_US",
    "aadhaar": "AADHAAR",
    "aadhar": "AADHAAR",
    "pan number": "PAN_IN",
    "pan": "PAN_IN",
    "national insurance": "NI_UK",
    "nino": "NI_UK",
    "ni number": "NI_UK",
    "ip address": "IP_ADDRESS",
    "ip": "IP_ADDRESS",
}


class Layer3PositionalInference:
    """Infer or corroborate PII type from column header context.

    One instance may be shared across calls — this class holds no mutable state.
    """

    def infer(
        self,
        block: ExtractedBlock,
        candidate: DetectionResult,
    ) -> DetectionResult | None:
        """Return corroborated DetectionResult if a header keyword matches; else None.

        Parameters
        ----------
        block:
            The tabular ExtractedBlock.  Must have col_header set; if
            col_header is None this method always returns None.
        candidate:
            Existing Layer 1 or Layer 2 DetectionResult for this block.

        Returns
        -------
        DetectionResult with extraction_layer="layer_3_positional" and
        pattern_used="header:<keyword>", or None when no keyword matches.
        Score is boosted by _SCORE_BOOST (capped at 1.0).
        """
        if not block.col_header:
            return None

        col_lower = block.col_header.lower().strip()

        # Strip [REVIEW] prefix added by ExcelReader's false-positive guard
        if col_lower.startswith("[review] "):
            col_lower = col_lower[len("[review] "):]

        # Longest-match first: prevents "date" swallowing "date of birth"
        matched_entity_type: str | None = None
        matched_keyword: str | None = None
        for keyword in sorted(HEADER_KEYWORDS, key=len, reverse=True):
            if keyword in col_lower:
                matched_entity_type = HEADER_KEYWORDS[keyword]
                matched_keyword = keyword
                break

        if matched_entity_type is None:
            return None

        new_score = min(_MAX_SCORE, candidate.score + _SCORE_BOOST)

        logger.debug(
            "Layer3: header_keyword=%r inferred_entity_type=%s new_score=%.3f",
            matched_keyword,
            matched_entity_type,
            new_score,
        )

        return DetectionResult(
            block=block,
            entity_type=matched_entity_type,
            start=candidate.start,
            end=candidate.end,
            score=new_score,
            pattern_used=f"header:{matched_keyword}",
            geography=candidate.geography,
            regulatory_framework=candidate.regulatory_framework,
            extraction_layer="layer_3_positional",
        )
