"""Person name validation and text-based discovery (Extraction Quality Fix).

Ported from standalone test_hybrid_pipeline.py proven on 34 documents.

Two functions:
1. is_likely_name(text) — validates whether a string is actually a person name
   Used to gate vision PERSON results before they enter the field map.
   "January Statement" → False. "ADELINE CHANDLER" → True.

2. discover_person_from_text(doc_path, onset) — scans nearby pages for name
   patterns when vision fails to identify real PERSON values.
   Returns synthetic PII fields to inject into the vision result.

Usage in two_phase.py:
    from app.pipeline.person_discovery import is_likely_name, discover_person_from_text

    # After vision routing, validate PERSON fields
    person_fields = [f for f in routing.pii_fields if f.get("type") == "PERSON"]
    valid_persons = [f for f in person_fields if is_likely_name(f.get("value", ""))]

    if not valid_persons:
        discovered, best_page = discover_person_from_text(doc.source_path, onset)
        if discovered:
            routing.pii_fields = [f for f in routing.pii_fields if f.get("type") != "PERSON"]
            routing.pii_fields.extend(discovered)
            onset = best_page
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Comprehensive blocklist for name rejection (369 words)
# Ported from standalone _LOC_WORDS — proven on 34 documents.
# Includes: address words, report labels, company suffixes, legal terms,
# financial terms, education terms, US state abbreviations, common English
# function words.
# ---------------------------------------------------------------------------

NAME_BLOCKLIST: frozenset[str] = frozenset({
    # Address words
    "ST", "AVE", "RD", "DR", "LN", "BLVD", "WAY", "PL", "CT", "STREET", "AVENUE",
    "ROAD", "DRIVE", "LANE", "BOULEVARD", "PLACE", "COURT", "NE", "NW", "SE", "SW",
    "APT", "SUITE", "STE", "FLOOR", "UNIT", "BOX",
    # Directions / geography
    "NORTH", "SOUTH", "EAST", "WEST", "PARK", "HILL", "BEACH", "SPRINGS", "FALLS",
    "CREEK", "LAKE", "VALLEY", "RIDGE", "HEIGHTS", "MANOR", "GROVE", "ACRES",
    "MEADOW", "HARBOR", "POINT", "HAVEN", "ISLAND", "CENTER", "CENTRE", "VILLAGE",
    "TOWN", "CITY", "COUNTY",
    # Place names that aren't people
    "YORK", "VIRGINIA", "CEDAR", "SILVER", "ROCK", "SPRING", "SAGE", "CRYSTAL",
    "SANDY", "GRAND", "PLEASANT", "MOUNT", "FORT", "PORT", "CAPE", "BAY", "KEY",
    "PALM", "PINE", "OAK", "ELM", "MAPLE", "GREEN", "BOWLING",
    # Report/form labels
    "REPORT", "REPORTS", "PAYROLL", "MANAGEMENT", "SUMMARY", "TOTAL", "PAGE", "PAGES",
    "FORM", "SECTION", "SECT", "PART", "DEPARTMENT", "COMPANY", "DISTRICT", "OFFICE",
    "SYSTEM", "DATE", "NUMBER", "ACCOUNT", "EMPLOYEE", "EMPLOYER", "NAME", "ADDRESS",
    "PHONE", "EMAIL", "CODE", "TYPE", "STATUS", "AMOUNT", "BALANCE", "PERIOD",
    "BEGIN", "END", "RATE", "LEVEL", "GROUP", "CHECK", "CHECKING", "SAVINGS",
    "ADVICE", "STATEMENT", "DEDUCTION", "EARNINGS", "FEDERAL", "STATE", "LOCAL",
    "TAX", "INSURANCE", "BENEFIT", "PLAN", "COVERAGE", "PREMIUM", "INFORMATION",
    "DESCRIPTION", "SCHEDULE", "RECORD", "DETAIL", "DETAILS", "VERIFICATION",
    "IDENTIFICATION", "AUTHORIZATION", "DOCUMENT", "PROVIDER",
    # Document/form types
    "INVOICE", "RECEIPT", "BILL", "NOTICE", "CERTIFICATE", "EXHIBIT",
    # Financial terms
    "INCOME", "COLLECTIONS", "ATTN", "OFFER", "FOLLOWING", "START", "MONTH",
    "DEFAULT", "ORIGINAL", "CORRECTED", "AMENDED", "VOID", "BREAKAGE", "FAST",
    # Company/org suffixes
    "LLP", "LLC", "INC", "CORP", "LTD", "PLC", "HOLDINGS", "PARTNERS", "ASSOCIATES",
    "CONSULTING", "SERVICES", "SOLUTIONS", "TECHNOLOGIES", "ENTERPRISES",
    "INTERNATIONAL", "GLOBAL", "NATIONAL", "INDUSTRIES",
    # Education
    "SCHOOL", "UNIVERSITY", "COLLEGE", "ACADEMY", "INSTITUTE", "HOSPITAL", "MEDICAL",
    "HIGH", "LENGTH", "CRDTS", "CREDITS", "GRADE", "SEMESTER", "TERM", "COURSE",
    # Legal / trust terms
    "TRUST", "TRUSTEE", "TTEE", "REVOCABLE", "IRREVOCABLE", "LIVING", "ESTATE",
    "CUSTODIAN", "CUST", "GUARDIAN", "BENEFICIARY", "FBO", "DIRECTED", "IRA",
    "OTMA", "UTMA", "UGMA", "SUBJECT", "RULES", "DATED", "FORMERLY",
    # Common English function words (prevent form labels being names)
    "AND", "OR", "THE", "FOR", "WITH", "FROM", "THAT", "WILL", "BE", "IS", "NOT",
    "ON", "OF", "SELF", "ONLY", "THIS", "HAS", "BEEN", "WAS", "ARE", "ALL", "ANY",
    "BUT", "CAN", "DID", "DO", "HAD", "HER", "HIS", "HOW", "ITS", "MAY", "NEW",
    "NOW", "OLD", "OUR", "OUT", "OWN", "PER", "PUT", "RUN", "SAY", "SHE", "TOO",
    "USE", "HIM", "LET", "SET", "TRY", "WHO", "WHY", "EACH", "THAN", "THEM",
    "THEN", "THEY", "INTO", "JUST", "LIKE", "MAKE", "MANY", "MOST", "MUCH", "MUST",
    "NEED", "NEXT", "ALSO", "BACK", "CALL", "COME", "COPY", "DOES", "DOWN", "EVEN",
    "FIND", "GIVE", "HAVE", "HERE", "KEEP", "KNOW", "LAST", "LINE", "LONG", "LOOK",
    "MADE", "MORE", "MOVE", "NONE", "ONCE", "OPEN", "OVER", "SAME", "SHOW", "SIDE",
    "SOME", "SUCH", "SURE", "TAKE", "TELL", "VERY", "WANT", "WHAT", "WHEN", "WORK",
    "YEAR", "YOUR", "PRIOR", "BELOW", "ABOVE",
    # US state abbreviations (prevent "CA", "NY" etc from being name words)
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
    # Name suffixes (handled separately in structural analysis)
    "JR", "SR", "II", "III", "IV", "V", "VI", "VII", "VIII", "ESQ", "MD", "PHD", "DDS",
    "STATION",
})


def is_likely_name(text: str) -> bool:
    """Check whether a string looks like a real person name.

    Returns False for page headers, form labels, report titles, addresses,
    and other non-name text that vision models sometimes misidentify as PERSON.

    Proven to reject "January Statement", "Report Summary", "Account Criteria"
    while accepting "ADELINE CHANDLER", "Smith, John", "Dr. Barbara Jones".
    """
    t = text.strip()
    words = t.upper().split()

    # Too short or too long
    if len(t) < 3 or len(t) > 60:
        return False

    # Contains digits — names don't have numbers
    if any(c.isdigit() for c in t):
        return False

    # Any word hits the blocklist
    if any(w in NAME_BLOCKLIST for w in words):
        return False

    # Needs at least 2 words (single words are unlikely full names)
    if len(words) < 2:
        return False

    # All very short words — likely form labels ("A B C D")
    if len(words) >= 3 and all(len(w) <= 2 for w in words):
        return False

    return True


# ---------------------------------------------------------------------------
# Text-based person discovery — fallback when vision gets PERSON wrong
# ---------------------------------------------------------------------------

# Name patterns to search for in page text
_PERSON_PATTERNS = [
    (re.compile(r"^([A-Z][a-z'-]+,\s*[A-Z][a-z'-]+(?:\s+[A-Z]\.?)?)$"), "last_first"),
    (re.compile(r"^([A-Z]{2,},\s*[A-Z]{2,}(?:\s+[A-Z]\.?)?)$"), "last_first_caps"),
    (re.compile(r"^([A-Z]{2,}\s+[A-Z]\.?\s+[A-Z]{2,})$"), "first_m_last"),
    (re.compile(r"^([A-Z][a-z'-]+\s+[A-Z]\.?\s+[A-Z][a-z'-]+)$"), "first_last"),
    (re.compile(r"^((?:Mr|Mrs|Ms|Dr)\.?\s+[A-Z]\s+[A-Z]\s+[A-Za-z]+)$"), "titled"),
]

# Common non-name words that pattern-match but aren't people
_DISCOVERY_SKIP = frozenset({
    "REPORT", "TOTAL", "PAGE", "ACCOUNT", "SUMMARY", "DATE", "NUMBER", "ADDRESS",
    "STATEMENT", "BALANCE", "PHONE", "EMAIL", "TAX", "INSURANCE", "CERTIFICATE",
    "SHARES", "CERT", "TRUST", "BANK", "NATIONAL", "COMPANY", "CORP", "LLC", "INC",
    "MIDDLEFIELD", "BANC", "SHAREHOLDERS", "LIST", "PAYROLL", "AMERICAN", "STOCK",
    "BOOSEY", "HAWKES", "ALFRED", "KNOPF", "PRINCIPAL", "FINANCIAL", "JANUARY",
    "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER",
    "OCTOBER", "NOVEMBER", "DECEMBER",
})


def discover_person_from_text(
    doc_path: str,
    onset: int,
    sample_pages: int = 5,
) -> tuple[list[dict], int]:
    """Scan nearby pages for name patterns when vision fails.

    Returns (pii_fields, best_page) where pii_fields are synthetic
    PERSON entries to inject into the vision result, and best_page
    is the page where names were found (may differ from onset).

    Parameters
    ----------
    doc_path: path to PDF file
    onset: current onset page number
    sample_pages: max pages to scan

    Returns
    -------
    tuple of (list of {"type": "PERSON", "value": ..., "label": "discovered"}, best_page_number)
    """
    import fitz

    try:
        doc = fitz.open(doc_path)
    except Exception:
        return [], onset

    n = doc.page_count

    # Candidate pages: near onset + spread through document
    candidate_pages = []
    for pn in [onset + 1, onset + 2, onset - 1, onset + 3, n // 4, n // 2, 3 * n // 4]:
        if 0 <= pn < n and pn != onset:
            candidate_pages.append(pn)

    found: list[dict] = []
    best_page = onset

    for pn in candidate_pages[:sample_pages]:
        text = doc[pn].get_text()
        page_names: list[dict] = []

        for line in text.split("\n"):
            line = line.strip()
            if len(line) < 5 or len(line) > 50:
                continue

            for pat, fmt in _PERSON_PATTERNS:
                m = pat.match(line)
                if m:
                    name = m.group(1)
                    words = name.upper().replace(",", "").split()
                    if any(w in _DISCOVERY_SKIP for w in words):
                        continue
                    if any(c.isdigit() for c in name):
                        continue
                    page_names.append({
                        "type": "PERSON",
                        "value": name,
                        "label": "discovered",
                    })
                    break

        # Need at least 2 names on a page to be confident
        if len(page_names) >= 2:
            found.extend(page_names[:3])
            best_page = pn
            break

    doc.close()

    if found:
        logger.info(
            "Discovered %d PERSON values from text on page %d: %s",
            len(found), best_page, [f["value"][:30] for f in found],
        )

    return found, best_page