"""Static value filter for post-extraction cleanup (Step 23d).

Problem: Report dates, company phone numbers, and header text get extracted
as individual PII because they exist at the correct coordinates on every page.
They're document metadata, not individual personal data.

Solution: After extraction, identify values that appear on >N% of pages
and remove them from individual records. These are static report values.

Proven March 2026: WashingtonCMD had report dates extracted as DOB on
every page. Filter removes these while keeping real per-person DOBs.

Usage:
    from app.pipeline.static_filter import filter_static_values
    
    # page_records = {page_num: [{"PERSON": "...", "US_SSN": "...", "DOB": "01/15/80"}, ...]}
    cleaned, removed = filter_static_values(page_records, threshold=0.5)
"""
from __future__ import annotations

import logging
from collections import Counter

logger = logging.getLogger(__name__)

# Fields that should NEVER be filtered (always individual-specific)
_NEVER_FILTER = frozenset({"PERSON", "US_SSN", "GOVERNMENT_ID"})

# Fields commonly static across pages (report metadata)
_LIKELY_STATIC = frozenset({
    "DATE_OF_BIRTH", "PHONE_NUMBER", "EMAIL_ADDRESS",
    "LOCATION", "CITY_STATE_ZIP", "ACCOUNT_NUMBER",
})


def filter_static_values(
    page_records: dict[int, list[dict]],
    threshold: float = 0.5,
    min_pages: int = 5,
) -> tuple[dict[int, list[dict]], dict[str, list[str]]]:
    """Remove values that appear on too many pages (report metadata).
    
    Parameters
    ----------
    page_records:
        {page_num: [record_dict, ...]} — extraction output
    threshold:
        Fraction of pages a value must appear on to be considered static.
        Default 0.5 = value appears on >50% of pages → static.
    min_pages:
        Minimum number of pages required before filtering applies.
        Prevents filtering on small documents where repetition is normal.
    
    Returns
    -------
    tuple[cleaned_page_records, removed_values]
        cleaned: same structure with static values removed from records
        removed: {field_type: [list of removed values]} for audit trail
    """
    total_pages = len(page_records)
    
    if total_pages < min_pages:
        return page_records, {}
    
    # Count how many pages each (field_type, value) pair appears on
    value_page_counts: Counter[tuple[str, str]] = Counter()
    
    for page_recs in page_records.values():
        # Deduplicate within page (same value on same page = 1 count)
        page_values: set[tuple[str, str]] = set()
        for rec in page_recs:
            for field_type, value in rec.items():
                if field_type.startswith("_"):
                    continue
                if field_type in _NEVER_FILTER:
                    continue
                if field_type not in _LIKELY_STATIC:
                    continue
                page_values.add((field_type, str(value)))
        
        for fv in page_values:
            value_page_counts[fv] += 1
    
    # Identify static values
    static_values: set[tuple[str, str]] = set()
    cutoff = threshold * total_pages
    
    for (field_type, value), count in value_page_counts.items():
        if count >= cutoff:
            static_values.add((field_type, value))
    
    if not static_values:
        return page_records, {}
    
    # Build removed audit trail
    removed: dict[str, list[str]] = {}
    for field_type, value in static_values:
        removed.setdefault(field_type, []).append(value)
    
    logger.info(
        "Static filter: removing %d values appearing on >%.0f%% of %d pages: %s",
        len(static_values), threshold * 100, total_pages,
        {k: v[:3] for k, v in removed.items()},
    )
    
    # Clean records
    cleaned: dict[int, list[dict]] = {}
    total_removed = 0
    
    for page_num, page_recs in page_records.items():
        cleaned_recs = []
        for rec in page_recs:
            clean_rec = {}
            for field_type, value in rec.items():
                if (field_type, str(value)) in static_values:
                    total_removed += 1
                else:
                    clean_rec[field_type] = value
            
            # Keep record if it still has at least PERSON or another key field
            if clean_rec and ("PERSON" in clean_rec or "US_SSN" in clean_rec 
                             or "GOVERNMENT_ID" in clean_rec):
                cleaned_recs.append(clean_rec)
        
        if cleaned_recs:
            cleaned[page_num] = cleaned_recs
    
    logger.info(
        "Static filter: removed %d field values across %d pages",
        total_removed, total_pages,
    )
    
    return cleaned, removed
