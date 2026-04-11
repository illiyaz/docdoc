"""Coordinate-based PII extraction for fixed-layout documents (Step 21b).

For documents where every page has an identical layout (accounting statements,
payslips, labeled forms), the LLM analyzes the layout ONCE and produces a
``FieldMapping`` list.  This module then uses PyMuPDF word-level bounding boxes
to extract PII from every page in seconds — no LLM calls needed.

Flow:
  1. For each page, get word bounding boxes via ``page.get_text("words")``.
  2. For each ``FieldMapping``, find anchor text on the page.
  3. Compute a search region relative to the anchor based on ``spatial_relationship``.
  4. Collect words in that region and join them.
  5. Apply ``skip_pattern`` and ``value_pattern`` filters.
  6. Map the result to the appropriate ``PIIRecord`` field.

Pages where the PERSON field cannot be extracted are reported as failures
for LLM reconciliation.
"""
from __future__ import annotations

import logging
import re
from uuid import uuid4

import fitz  # PyMuPDF

from app.rra.entity_resolver import PIIRecord
from app.structure.document_schema import FieldMapping

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field type alias normalization (LLM may return domain-specific names)
# ---------------------------------------------------------------------------

FIELD_TYPE_ALIASES: dict[str, str] = {
    "CLIENT": "PERSON", "CLIENT_NAME": "PERSON",
    "EMPLOYEE": "PERSON", "EMPLOYEE_NAME": "PERSON",
    "MEMBER": "PERSON", "MEMBER_NAME": "PERSON",
    "PATIENT": "PERSON", "PATIENT_NAME": "PERSON",
    "NAME": "PERSON", "FULL_NAME": "PERSON",
    "TAX_NO": "US_SSN", "TAX_NUMBER": "US_SSN",
    "TAX_ID": "US_SSN", "SSN": "US_SSN",
    "EIN": "US_EIN", "EMPLOYER_ID": "US_EIN",
    "EMPLOYER_IDENTIFICATION": "US_EIN",
    "NATIONAL_INSURANCE": "NI_NUMBER", "NI_NO": "NI_NUMBER",
    "ADDRESS": "LOCATION", "ADDR": "LOCATION",
    "DOB": "DATE_OF_BIRTH", "BIRTH_DATE": "DATE_OF_BIRTH",
    "PHONE": "PHONE_NUMBER", "TEL": "PHONE_NUMBER",
    "EMAIL": "EMAIL_ADDRESS", "MAIL": "EMAIL_ADDRESS",
    "GOVERNMENT_ID": "US_SSN",
}


def _normalize_field_type(field_type: str) -> str:
    """Normalize domain-specific field types to standard entity types."""
    upper = field_type.upper().strip()
    return FIELD_TYPE_ALIASES.get(upper, upper)


# ---------------------------------------------------------------------------
# Phone format validation (Fix 4: reject dollar amounts, SSNs in phone field)
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(
    r"^[\s(]*"                         # optional leading space/paren
    r"(?:\+?1[\s.-]*)?"                # optional country code
    r"(?:\(?\d{3}\)?[\s.-]*)"          # area code
    r"\d{3}[\s.-]*\d{4}"              # subscriber number
    r"(?:\s*(?:x|ext|extension)[\s.]*\d{1,6})?"  # optional extension
    r"\s*$",
    re.IGNORECASE,
)

# Common non-phone patterns that slip through
_NOT_PHONE_RE = re.compile(
    r"\d+\.\d{2}\s",                   # dollar amounts like "526.56 "
)


def _is_valid_phone(value: str) -> bool:
    """Check if a string looks like a real phone number.

    Rejects dollar amounts, SSNs, and random digit sequences that get
    mis-mapped to the phone field by coordinate extraction.
    """
    if not value or not isinstance(value, str):
        return False
    v = value.strip()
    if not v:
        return False
    # Reject obvious non-phone patterns
    if _NOT_PHONE_RE.search(v):
        return False
    # Must have 10-11 digits (US phone)
    digits = re.sub(r"\D", "", v)
    if len(digits) < 10 or len(digits) > 15:
        return False
    # SSN pattern (9 digits with dashes) — not a phone
    if re.fullmatch(r"\d{3}-\d{2}-\d{4}", v):
        return False
    return True


# ---------------------------------------------------------------------------
# Name cleanup (Fix 5: strip leading commas, reject truncated names)
# ---------------------------------------------------------------------------

def _clean_name(name: str) -> str | None:
    """Clean and validate an extracted name.

    Strips leading/trailing commas and whitespace.  Returns None if the
    result is too short or obviously broken.
    """
    if not name or not isinstance(name, str):
        return None
    cleaned = name.strip().strip(",").strip()
    if len(cleaned) < 3:
        return None
    # If name still has a leading comma after strip (shouldn't happen), fix it
    if cleaned.startswith(","):
        cleaned = cleaned[1:].strip()
    return cleaned if len(cleaned) >= 3 else None


# ---------------------------------------------------------------------------
# Entity type → PIIRecord field mapping (matches llm_template_extractor)
# ---------------------------------------------------------------------------

_FIELD_TO_RAW: dict[str, str] = {
    "PERSON": "raw_name",
    "LOCATION": "raw_address",
    "DATE_OF_BIRTH": "raw_dob",
    "DATE_OF_BIRTH_DMY": "raw_dob",
    "DATE_OF_BIRTH_MDY": "raw_dob",
    "DATE_OF_BIRTH_ISO": "raw_dob",
    "EMAIL_ADDRESS": "raw_email",
    "EMAIL": "raw_email",
    "PHONE_NUMBER": "raw_phone",
    "PHONE_US": "raw_phone",
    "PHONE_INTL": "raw_phone",
    "US_SSN": "raw_government_id",
    "US_EIN": "raw_government_id",
    "NI_NUMBER": "raw_government_id",
    "AADHAAR": "raw_government_id",
    "US_DRIVER_LICENSE": "raw_government_id",
    "US_PASSPORT": "raw_government_id",
    "PAN_CARD": "raw_government_id",
    "NHS_NUMBER": "raw_government_id",
    "GOVERNMENT_ID": "raw_government_id",
    "IDENTIFICATION_NUMBER": "raw_government_id",
    "NATIONAL_INSURANCE_UK": "raw_government_id",
}

_GOV_ID_TYPES: frozenset[str] = frozenset({
    "US_SSN", "US_EIN", "NI_NUMBER", "AADHAAR", "US_DRIVER_LICENSE",
    "US_PASSPORT", "PAN_CARD", "NHS_NUMBER", "GOVERNMENT_ID",
    "IDENTIFICATION_NUMBER", "NATIONAL_INSURANCE_UK",
})

# Tolerance in points for anchor word matching (nearby lines)
_LINE_TOLERANCE = 5
_ANCHOR_STRIP_CHARS = ":.,;!?"  # trailing punctuation stripped during anchor match

# Common noise patterns stripped from PERSON values (client codes, ref numbers)
_PERSON_CLEANUP_RE = re.compile(r"\(\d+\)\s*")

# Status codes stripped from end of names (e.g., "ADAMS,BRADLEY JAY A" → "ADAMS,BRADLEY JAY")
_STATUS_CODES = frozenset("ABCDEFST")


def _clean_name(name_str: str) -> str:
    """Strip trailing single-letter status codes from matched names.
    
    Only strips A-F, S, T (common status codes). Preserves real initials
    like V, W (could be middle name).
    """
    parts = name_str.strip().split()
    while len(parts) > 1 and len(parts[-1]) == 1 and parts[-1].upper() in _STATUS_CODES:
        parts.pop()
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Structural name matcher for ALL_CAPS embedded names (Step 23a)
#
# Proven on Complex1 (0→8,617 PERSON), PPACA (0→1,650 PERSON).
# When names are embedded in data lines (no separate anchor), the standard
# anchor-based extraction fails. This matcher learns name structures from
# vision samples (e.g., (WORD, INITIAL, WORD)) and finds matching patterns
# in each line of text.
# ---------------------------------------------------------------------------

# Comprehensive blocklist for ALL_CAPS word rejection (369 words)
# Ported from standalone _LOC_WORDS — proven on 34 documents.
# Shared source of truth: app/pipeline/person_discovery.py NAME_BLOCKLIST
_NAME_BLOCKLIST: frozenset[str] = frozenset({
    # Address words
    "ST", "AVE", "RD", "DR", "LN", "BLVD", "WAY", "PL", "CT", "STREET", "AVENUE",
    "ROAD", "DRIVE", "LANE", "BOULEVARD", "PLACE", "COURT", "NE", "NW", "SE", "SW",
    "APT", "SUITE", "STE", "FLOOR", "UNIT", "BOX",
    # Directions / geography
    "NORTH", "SOUTH", "EAST", "WEST", "PARK", "HILL", "BEACH", "SPRINGS", "FALLS",
    "CREEK", "LAKE", "VALLEY", "RIDGE", "HEIGHTS", "MANOR", "GROVE", "ACRES",
    "MEADOW", "HARBOR", "POINT", "HAVEN", "ISLAND", "CENTER", "CENTRE", "VILLAGE",
    "TOWN", "CITY", "COUNTY",
    # Place names
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
    # Document/form types (previously in _FIELD_MAP_BAD_NAMES)
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
    # Common English function words
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
    # US state abbreviations
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
    "STATION",
})

_SUFFIX_WORDS = frozenset({"JR", "SR", "II", "III", "IV", "V", "VI", "VII", "VIII", "ESQ", "MD", "PHD", "DDS"})


def _is_likely_name(name: str) -> bool:
    """Check if a string looks like a real person name (not header/boilerplate).

    Validates against the comprehensive _NAME_BLOCKLIST.  Rejects:
    - Too short (<3 chars) or too long (>80 chars)
    - Contains digits
    - Single-word "names"
    - All significant words are in blocklist
    - First word (likely surname) is a blocklisted word

    Proven on 34+ document types.  Used by both coordinate_extractor
    (inline) and two_phase.py (post-extraction).
    """
    if not name:
        return False
    t = name.strip()
    if len(t) < 3 or len(t) > 80:
        return False
    if any(c.isdigit() for c in t):
        return False
    words = t.split()
    if len(words) < 2:
        return False
    if not any(len(w) >= 3 for w in words):
        return False
    upper_words = [w.upper().rstrip(",.;:") for w in words]
    # All significant words in blocklist → not a name
    if all(w in _NAME_BLOCKLIST for w in upper_words if len(w) >= 2):
        return False
    # First word is blocklisted → likely a label/header
    if upper_words and upper_words[0] in _NAME_BLOCKLIST:
        return False
    return True


def _analyze_name_structure(name: str) -> tuple[str, ...]:
    """Break a name into structural components.
    
    Examples:
        "ADAMS,BRADLEY JAY" → ("WORD", "WORD", "WORD")
        "K BEVINGTON II" → ("INITIAL", "WORD", "SUFFIX")
        "JOHN SMITH" → ("WORD", "WORD")
    """
    parts = name.strip().replace(",", " ").split()
    structure: list[str] = []
    for p in parts:
        pc = p.rstrip(".")
        if pc.upper() in _SUFFIX_WORDS:
            structure.append("SUFFIX")
        elif len(pc) == 1 and pc.isupper():
            structure.append("INITIAL")
        elif len(pc) >= 2 and pc.isupper():
            structure.append("WORD")
        else:
            structure.append("OTHER")
    return tuple(structure)


def _build_name_structures(
    samples: list[str],
) -> tuple[set[tuple[str, ...]], int, int]:
    """Build set of acceptable name structures from vision samples.
    
    Returns (structures, min_words, max_words).
    """
    structures: set[tuple[str, ...]] = set()
    for s in samples:
        st = _analyze_name_structure(s)
        if "OTHER" not in st:
            structures.add(st)
            # Also accept without trailing suffix
            if st and st[-1] == "SUFFIX":
                structures.add(st[:-1])
    if not structures:
        return set(), 2, 4
    min_w = min(len(s) for s in structures)
    max_w = max(len(s) for s in structures)
    return structures, min_w, max_w


def find_structural_names(
    line: str,
    structures: set[tuple[str, ...]],
    min_words: int,
    max_words: int,
) -> list[str]:
    """Find ALL_CAPS names in a line using learned structures.
    
    Slides a window across the line words, checking each candidate
    against the learned structure patterns while rejecting blocklisted words.
    """
    line_norm = re.sub(r"\s+", " ", line).strip()
    words = line_norm.split()
    found: list[str] = []
    used: set[int] = set()

    for start in range(len(words)):
        if start in used:
            continue
        best = None
        for length in range(max_words, min_words - 1, -1):
            if start + length > len(words):
                continue
            cand = words[start:start + length]
            cand_text = " ".join(cand)
            # Reject if any word has digits
            if any(any(c.isdigit() for c in w) for w in cand):
                continue
            st = _analyze_name_structure(cand_text)
            if st not in structures:
                continue
            if any(w.upper() in _NAME_BLOCKLIST for w in cand):
                continue
            # Must have at least one word with 3+ chars (not all initials/suffixes)
            if not any(len(w) >= 3 and w.upper() not in _SUFFIX_WORDS for w in cand):
                continue
            best = (cand_text, start, length)
            break
        if best:
            found.append(_clean_name(best[0]))
            for i in range(best[1], best[1] + best[2]):
                used.add(i)

    return found


# ---------------------------------------------------------------------------
# Regex-based name format learning for mixed-case names (Gap 1 fix)
#
# When vision samples contain mixed-case names ("Smith, John", "John Smith",
# "Mr. John Smith"), the structural matcher (ALL_CAPS only) won't find them.
# This function learns a format-specific regex from the first sample and
# returns it for use as a second fallback after structural matching.
# ---------------------------------------------------------------------------

# Unicode character classes for name patterns
_U = r"A-Z\u00C0-\u00D6\u00D8-\u00DE"  # uppercase Latin + diacritics
_L = r"a-z\u00E0-\u00F6\u00F8-\u00FF"  # lowercase Latin + diacritics


def _learn_name_regex(
    samples: list[str],
) -> tuple[re.Pattern | None, str]:
    """Learn a format-specific regex from person name samples.

    Detects 5 name formats from the first non-empty sample:
    - ``"last_first"``: comma-separated (``"Smith, John"``)
    - ``"titled"``: title prefix (``"Mr. John Smith"``)
    - ``"first_last"``: mixed-case standard (``"John Smith"``)
    - ``"all_caps"``: uppercase (``"JOHN SMITH"``)
    - ``"generic"``: catch-all

    Unicode-aware: handles José, García, Müller, O'Brien-García.

    Returns ``(compiled_pattern, format_name)`` or ``(None, "unknown")``
    when samples are empty.
    """
    if not samples:
        return None, "unknown"

    # Find first non-empty sample
    sample = ""
    for s in samples:
        s = s.strip()
        if s:
            sample = s
            break
    if not sample:
        return None, "unknown"

    U, L = _U, _L

    if "," in sample:
        # Last, First — allows multi-word surnames: "DE LA CRUZ, JOHN"
        pattern = rf"[{U}][{U}{L}' -]{{1,30}},\s*[{U}][{U}{L} .'-]+"
        return re.compile(pattern), "last_first"

    if re.match(r"(?:Mr|Mrs|Ms|Dr|Miss)\b", sample):
        pattern = rf"(?:Mr|Mrs|Ms|Dr|Miss)\.?\s+[{U}][{U}{L}'-]*(?:\s+[{U}][{U}{L}'-]*)+"
        return re.compile(pattern), "titled"

    if re.match(rf"[{U}][{L}]", sample):
        # First Last — allows hyphens, diacritics, suffixes
        pattern = (
            rf"[{U}][{L}'-]+(?:[-\s]+[{U}]\.?)?(?:[-\s]+[{U}][{L}'-]+)+"
            r"(?:\s+(?:Jr|Sr|II|III|IV|V|VI)\.?)?"
        )
        return re.compile(pattern), "first_last"

    if re.match(r"[A-Z]+ [A-Z]", sample):
        # ALL_CAPS — structural matcher handles this too, but provide regex
        pattern = (
            r"(?:DR\s+)?[A-Z]{2,}(?:\s+[A-Z]\.?\s*)*[A-Z]{2,}"
            r"(?:\s+(?:JR|SR|II|III|IV|V|VI|VII|VIII|ESQ|MD|PHD))?"
        )
        return re.compile(pattern), "all_caps"

    # Generic fallback
    pattern = rf"[{U}][{U}{L}'-]{{1,25}}(?:[,\s]+[{U}][{U}{L} .'-]+)+"
    return re.compile(pattern), "generic"


# Address validation: US state abbreviations (2-letter), ZIP codes, country names
_US_STATE_ABBRS = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
})
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_UK_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.IGNORECASE)
_STREET_RE = re.compile(
    r"\b(?:ROAD|RD|STREET|ST|AVENUE|AVE|DRIVE|DR|LANE|LN|COURT|CT|"
    r"BOULEVARD|BLVD|WAY|PLACE|PL|CIRCLE|CIR|TERRACE|TER)\b",
    re.IGNORECASE,
)
_COUNTRY_NAMES = frozenset({
    "USA", "US", "UNITED STATES", "UK", "UNITED KINGDOM",
    "CANADA", "AUSTRALIA", "IRELAND", "INDIA",
})


def _looks_like_address(text: str) -> bool:
    """Check if text looks like it contains address content."""
    if not text:
        return False
    upper = text.upper()
    # Check for street-number start (e.g., "3708 GRAHAM ROAD")
    if re.search(r"^\d+\s+\w+", text, re.MULTILINE):
        return True
    # Check for ZIP code
    if _ZIP_RE.search(text):
        return True
    # Check for UK postcode
    if _UK_POSTCODE_RE.search(text):
        return True
    # Check for street type keywords
    if _STREET_RE.search(text):
        return True
    # Check for US state abbreviation as a standalone word
    for word in upper.split():
        if word.strip(",. ") in _US_STATE_ABBRS:
            return True
    # Check for country names
    for country in _COUNTRY_NAMES:
        if country in upper:
            return True
    return False


# ---------------------------------------------------------------------------
# CoordinateExtractor
# ---------------------------------------------------------------------------


class CoordinateExtractor:
    """Fast extraction for fixed-layout documents.

    The LLM provides the field map (anchor text + spatial relationships),
    Python extracts from every page using word-level bounding boxes.
    Processes 1000+ pages in seconds.

    Parameters
    ----------
    field_map:
        List of ``FieldMapping`` from the ``DocumentSchema.layout_field_map``.
    doc_path:
        Path to the PDF file.
    doc_id:
        Document ID for ``PIIRecord.source_document_id``.
    name_samples:
        Optional list of PERSON name strings from vision analysis.
        Used for structural name matching fallback when anchor-based
        PERSON extraction fails (e.g., ALL_CAPS names embedded in data lines).
    """

    def __init__(
        self,
        field_map: list[FieldMapping],
        doc_path: str,
        doc_id: str,
        name_samples: list[str] | None = None,
    ) -> None:
        self.field_map = field_map
        self.doc_path = doc_path
        self.doc_id = doc_id
        
        # Build structural name patterns from vision samples (Step 23a)
        self._name_structures: set[tuple[str, ...]] = set()
        self._struct_min_w = 2
        self._struct_max_w = 4
        # Regex-based name pattern for mixed-case names (Gap 1 fix)
        self._name_regex: re.Pattern | None = None
        self._name_format: str = "unknown"

        if name_samples:
            self._name_structures, self._struct_min_w, self._struct_max_w = (
                _build_name_structures(name_samples)
            )
            if self._name_structures:
                logger.info(
                    "Structural name matcher: %d patterns from %d samples",
                    len(self._name_structures), len(name_samples),
                )
            # Also learn regex for mixed-case name fallback
            self._name_regex, self._name_format = _learn_name_regex(name_samples)
            if self._name_regex:
                logger.info(
                    "Name regex matcher: format=%s from %d samples",
                    self._name_format, len(name_samples),
                )

    def extract_all_pages(
        self,
        page_range: list[int] | None = None,
    ) -> tuple[list[PIIRecord], list[int]]:
        """Extract PII from all (or specified) pages.

        Returns
        -------
        tuple[list[PIIRecord], list[int]]
            ``(records, failed_pages)`` — successfully extracted records and
            page numbers (0-based) where extraction failed.
        """
        doc = fitz.open(self.doc_path)
        records: list[PIIRecord] = []
        failed_pages: list[int] = []

        pages_to_process = page_range if page_range is not None else list(range(doc.page_count))

        for page_num in pages_to_process:
            if page_num < 0 or page_num >= doc.page_count:
                failed_pages.append(page_num)
                continue

            page = doc[page_num]
            words = page.get_text("words")
            # Use page.rotation for region computation.  PyMuPDF does NOT
            # always derotate word coordinates — some PDFs (especially those
            # with /Rotate 90/270) return raw coordinates where same visual
            # line = same x-band.  The rotation-aware _compute_region,
            # _find_anchor, and _words_to_text handle this correctly.
            rotation = page.rotation

            # Pre-scan all anchor positions for anchor-bounded extraction
            all_anchors = self._find_all_anchors(words, rotation)

            # Collect fields into a dict, then construct frozen PIIRecord
            fields: dict[str, str | dict] = {}
            entity_types_found: list[str] = []
            success = True
            person_anchor_fm: FieldMapping | None = None

            for fm in self.field_map:
                norm_type = _normalize_field_type(fm.field_type)
                value = self._extract_field(words, fm, page, rotation, norm_type, all_anchors=all_anchors)
                if value:
                    raw_field = _FIELD_TO_RAW.get(norm_type)
                    if raw_field:
                        if raw_field == "raw_address":
                            fields[raw_field] = {"full": value}
                        else:
                            fields[raw_field] = value
                        entity_types_found.append(norm_type)
                    # Remember the PERSON anchor for address fallback
                    if norm_type == "PERSON":
                        person_anchor_fm = fm
                elif norm_type == "PERSON":
                    # PERSON is mandatory — page fails without it
                    success = False

            # Name fallback: get page text once for both structural and regex
            page_text: str | None = None
            if not success and (self._name_structures or self._name_regex):
                page_text = page.get_text()

            # Structural name fallback (Step 23a):
            # When anchor-based PERSON extraction fails but we have structural
            # patterns from vision samples, scan the page text for ALL_CAPS names.
            # Proven on Complex1 (0→8,617) and PPACA (0→1,650).
            if not success and self._name_structures and page_text:
                for line in page_text.split("\n"):
                    line_stripped = line.strip()
                    if len(line_stripped) < 4:
                        continue
                    names = find_structural_names(
                        line_stripped,
                        self._name_structures,
                        self._struct_min_w,
                        self._struct_max_w,
                    )
                    if names:
                        fields["raw_name"] = names[0]
                        entity_types_found.append("PERSON")
                        success = True
                        break  # Use first name found on page

            # Regex name fallback (Gap 1 fix):
            # When both anchor-based and structural (ALL_CAPS) matching fail,
            # try format-specific regex for mixed-case names (first_last,
            # last_first, titled).
            if not success and self._name_regex and page_text:
                for line in page_text.split("\n"):
                    line_stripped = line.strip()
                    if len(line_stripped) < 4:
                        continue
                    match = self._name_regex.search(line_stripped)
                    if match:
                        candidate = match.group(0).strip()
                        if _is_likely_name(candidate):
                            fields["raw_name"] = candidate
                            entity_types_found.append("PERSON")
                            success = True
                            break

            # Address fallback: if PERSON found but no LOCATION in field_map,
            # look for address lines below the PERSON anchor.  Common in
            # accounting statements where "In Account with : NAME\nADDRESS".
            if (
                success
                and "raw_address" not in fields
                and person_anchor_fm is not None
                and not self._has_location_field()
            ):
                addr = self._extract_address_below_person(
                    words, person_anchor_fm, page, rotation,
                )
                if addr:
                    fields["raw_address"] = {"full": addr}
                    entity_types_found.append("LOCATION")

            raw_name = fields.get("raw_name")

            # Fix 5: Clean name — strip leading commas, reject truncated
            if raw_name and isinstance(raw_name, str):
                raw_name = _clean_name(raw_name)
                if raw_name:
                    fields["raw_name"] = raw_name

            # Inline name validation: reject header text, single words,
            # digits, and blocklisted words before creating a record.
            if raw_name and isinstance(raw_name, str) and not _is_likely_name(raw_name):
                logger.debug(
                    "Name validation rejected '%s' on page %d", raw_name, page_num,
                )
                raw_name = None
                success = False

            # Fix 4: Validate phone — reject dollar amounts, SSNs
            raw_phone = fields.get("raw_phone")
            if raw_phone and isinstance(raw_phone, str) and not _is_valid_phone(raw_phone):
                logger.debug(
                    "Phone validation rejected '%s' on page %d", raw_phone, page_num,
                )
                fields["raw_phone"] = None

            if success and raw_name and isinstance(raw_name, str):
                # Derive entity_role from the PERSON field in the field map
                _coord_role = None
                for _fm in self.field_map:
                    if _fm.field_type.upper() in ("PERSON", "PERSON_NAME"):
                        _coord_role = getattr(_fm, "entity_role", None)
                        break
                rec = PIIRecord(
                    record_id=str(uuid4()),
                    entity_type="PERSON",
                    normalized_value=raw_name,
                    raw_name=raw_name,
                    raw_address=fields.get("raw_address"),
                    raw_phone=fields.get("raw_phone") if isinstance(fields.get("raw_phone"), str) else None,
                    raw_email=fields.get("raw_email") if isinstance(fields.get("raw_email"), str) else None,
                    raw_dob=fields.get("raw_dob") if isinstance(fields.get("raw_dob"), str) else None,
                    raw_government_id=fields.get("raw_government_id") if isinstance(fields.get("raw_government_id"), str) else None,
                    source_document_id=self.doc_id,
                    page_range=str(page_num + 1),
                    entity_types_found=tuple(entity_types_found),
                    entity_role=_coord_role,
                )
                records.append(rec)
            else:
                failed_pages.append(page_num)

            # Free page memory (PyMuPDF page streaming)
            doc._forget_page(page)

        doc.close()

        logger.info(
            "Coordinate extraction: %d records, %d failed pages (doc=%s)",
            len(records), len(failed_pages), self.doc_id,
        )
        return records, failed_pages

    # -- Anchor stability ---------------------------------------------------

    def check_anchor_stability(
        self,
        page_range: list[int],
        drift_threshold: float = 20.0,
    ) -> dict[str, float]:
        """Check if anchor positions are consistent across pages.

        Returns ``{field_type: max_drift_in_points}`` for fields found on
        at least 2 pages.  20 points is approximately 7 mm — enough to
        absorb minor rendering differences but catches real layout shifts.
        """
        doc = fitz.open(self.doc_path)
        # anchor_positions: {field_type: [(center_x, center_y), ...]}
        anchor_positions: dict[str, list[tuple[float, float]]] = {}

        for page_num in page_range:
            if page_num < 0 or page_num >= doc.page_count:
                continue
            page = doc[page_num]
            words = page.get_text("words")
            rotation = page.rotation

            for fm in self.field_map:
                norm_type = _normalize_field_type(fm.field_type)
                anchor_words = self._find_anchor(words, fm.anchor_text, rotation)
                if anchor_words:
                    bbox = self._merge_bboxes(anchor_words)
                    cx = (bbox[0] + bbox[2]) / 2
                    cy = (bbox[1] + bbox[3]) / 2
                    anchor_positions.setdefault(norm_type, []).append((cx, cy))

            doc._forget_page(page)

        doc.close()

        # Compute max drift per field (Euclidean from first position)
        drift_map: dict[str, float] = {}
        for field_type, positions in anchor_positions.items():
            if len(positions) < 2:
                continue
            base_x, base_y = positions[0]
            max_drift = max(
                ((px - base_x) ** 2 + (py - base_y) ** 2) ** 0.5
                for px, py in positions[1:]
            )
            drift_map[field_type] = max_drift

        return drift_map

    # -- Anchor pre-scanning for bounded extraction -------------------------

    def _find_all_anchors(
        self,
        words: list[tuple],
        rotation: int,
    ) -> list[tuple[str, tuple[float, float, float, float]]]:
        """Find positions of ALL anchors in the field map on a page.

        Returns list of ``(normalized_field_type, anchor_bbox)`` for each
        field whose anchor was found on this page.  Used by ``_clip_region``
        to prevent extraction regions from bleeding into adjacent fields.
        """
        result: list[tuple[str, tuple[float, float, float, float]]] = []
        for fm in self.field_map:
            anchor_words = self._find_anchor(words, fm.anchor_text, rotation)
            if anchor_words:
                bbox = self._merge_bboxes(anchor_words)
                norm_type = _normalize_field_type(fm.field_type)
                result.append((norm_type, bbox))
        return result

    @staticmethod
    def _clip_region(
        region: tuple[float, float, float, float],
        field_type: str,
        all_anchors: list[tuple[str, tuple[float, float, float, float]]],
        current_anchor_bbox: tuple[float, float, float, float],
        spatial_rel: str,
        rotation: int,
    ) -> tuple[float, float, float, float]:
        """Clip region so it stops before the next anchor in the reading direction.

        For ``same_line_right`` (rotation=0): clip ``x1`` at the ``x0`` of the
        next anchor on the same y-band.
        For ``same_line_right`` (rotation=270): clip ``y1`` at the ``y0`` of the
        next anchor on the same x-band.
        Analogous logic for rotation=90 and rotation=180.

        Only clips ``same_line_right`` and ``same_line_left`` relationships
        (the main source of text bleed).  Other relationships (``line_below``,
        ``lines_below_N``, ``region_right``) are returned unchanged.

        A 5-point gap is left before the next anchor to avoid capturing its
        label text.
        """
        if spatial_rel not in ("same_line_right", "same_line_left"):
            return region

        rx0, ry0, rx1, ry1 = region
        ax0, ay0, ax1, ay1 = current_anchor_bbox

        GAP = 5  # points gap before next anchor

        for other_type, other_bbox in all_anchors:
            if other_type == field_type:
                continue  # Skip self
            ox0, oy0, ox1, oy1 = other_bbox

            if rotation == 270:
                # same_line_right: reading direction is increasing y, same x-band
                x_overlap = (ox0 < ax1 + 10) and (ox1 > ax0 - 10)
                if x_overlap and oy0 > ay1:
                    ry1 = min(ry1, oy0 - GAP)
            elif rotation == 90:
                # same_line_right: reading direction is decreasing y, same x-band
                x_overlap = (ox0 < ax1 + 10) and (ox1 > ax0 - 10)
                if x_overlap and oy1 < ay0:
                    ry0 = max(ry0, oy1 + GAP)
            elif rotation == 180:
                # same_line_right: reading direction is decreasing x, same y-band
                y_overlap = (oy0 < ay1 + 10) and (oy1 > ay0 - 10)
                if y_overlap and ox1 < ax0:
                    rx0 = max(rx0, ox1 + GAP)
            else:  # rotation == 0
                # same_line_right: reading direction is increasing x, same y-band
                y_overlap = (oy0 < ay1 + 10) and (oy1 > ay0 - 10)
                if y_overlap and ox0 > ax1:
                    rx1 = min(rx1, ox0 - GAP)

        return (rx0, ry0, rx1, ry1)

    # -- Field extraction ---------------------------------------------------

    def _extract_field(
        self,
        words: list[tuple],
        field: FieldMapping,
        page: object,
        rotation: int = 0,
        norm_type: str | None = None,
        *,
        all_anchors: list[tuple[str, tuple[float, float, float, float]]] | None = None,
    ) -> str | None:
        """Find anchor text and extract the value at the relative position.

        Parameters
        ----------
        words:
            PyMuPDF word tuples: ``(x0, y0, x1, y1, text, block_no, line_no, word_no)``.
        field:
            The field mapping defining anchor + spatial relationship.
        page:
            PyMuPDF page object (for dimensions).
        rotation:
            Page rotation in degrees (0, 90, 180, 270).
        norm_type:
            Normalized field type (after alias resolution). Falls back to
            ``field.field_type`` if not provided.
        all_anchors:
            Pre-scanned anchor positions from ``_find_all_anchors()``.
            When provided, the extraction region is clipped at the nearest
            subsequent anchor to prevent text bleed between fields.

        Returns
        -------
        str | None
            Extracted text value, or ``None`` if anchor not found or
            value validation failed.
        """
        anchor_words = self._find_anchor(words, field.anchor_text, rotation)
        if not anchor_words:
            return None

        anchor_bbox = self._merge_bboxes(anchor_words)

        # Define search region based on spatial_relationship + rotation
        region = self._compute_region(anchor_bbox, field, page, rotation)
        if region is None:
            return None

        # Clip region at next anchor boundary to prevent text bleed
        if all_anchors:
            effective_type = norm_type or _normalize_field_type(field.field_type)
            region = self._clip_region(
                region, effective_type, all_anchors,
                anchor_bbox, field.spatial_relationship, rotation,
            )

        # Collect words in the region, excluding standalone punctuation
        region_words_raw = [
            w for w in words
            if self._in_region(w, region) and w[4].strip(_ANCHOR_STRIP_CHARS + " ")
        ]

        # Sort order depends on rotation
        if rotation == 270:
            # Visual top→bottom = increasing y, left→right = decreasing x
            region_words = sorted(region_words_raw, key=lambda w: (w[0], w[1]))
        elif rotation == 90:
            # Visual top→bottom = decreasing y, left→right = increasing x
            region_words = sorted(region_words_raw, key=lambda w: (-w[0], -w[1]))
        elif rotation == 180:
            # Visual left→right = decreasing x, top→bottom = decreasing y
            region_words = sorted(region_words_raw, key=lambda w: (-w[1], -w[0]))
        else:
            # Standard: top→bottom, left→right
            region_words = sorted(region_words_raw, key=lambda w: (w[1], w[0]))

        value = self._words_to_text(region_words, field.line_count, rotation)

        # Apply skip_pattern (remove matching text from value)
        if field.skip_pattern and value:
            try:
                value = re.sub(field.skip_pattern, "", value).strip()
            except re.error:
                pass

        # Strip common noise from PERSON values (e.g. "(001968) ADELINE CHANDLER")
        effective_type = norm_type or _normalize_field_type(field.field_type)
        if effective_type == "PERSON" and value:
            value = _PERSON_CLEANUP_RE.sub("", value).strip()
            value = _clean_name(value)

        # Skip value_pattern validation for PERSON fields — names are too
        # variable for regex validation.
        if effective_type != "PERSON" and field.value_pattern and value:
            try:
                if not re.search(field.value_pattern, value):
                    return None
            except re.error:
                pass

        return value or None

    # -- Address fallback ---------------------------------------------------

    def _has_location_field(self) -> bool:
        """Check if the field map already has a LOCATION field."""
        return any(
            _normalize_field_type(fm.field_type) == "LOCATION"
            for fm in self.field_map
        )

    def _extract_address_below_person(
        self,
        words: list[tuple],
        person_fm: FieldMapping,
        page: object,
        rotation: int,
    ) -> str | None:
        """Extract address lines below the PERSON anchor.

        Many fixed-layout documents (accounting statements, royalty reports)
        have a single labeled section like "In Account with : NAME" followed
        by address lines underneath.  When the LLM doesn't create a separate
        LOCATION field mapping, this fallback extracts 4 lines below the
        PERSON anchor and validates them as address content.
        """
        anchor_words = self._find_anchor(words, person_fm.anchor_text, rotation)
        if not anchor_words:
            return None

        anchor_bbox = self._merge_bboxes(anchor_words)

        # Create a synthetic field for "lines_below_4" (up to 4 address lines)
        synth = FieldMapping(
            field_type="LOCATION",
            anchor_text=person_fm.anchor_text,
            spatial_relationship="lines_below_4",
            line_count=4,
        )
        region = self._compute_region(anchor_bbox, synth, page, rotation)
        if region is None:
            return None

        region_words_raw = [
            w for w in words
            if self._in_region(w, region) and w[4].strip(_ANCHOR_STRIP_CHARS + " ")
        ]

        # Sort by reading order
        if rotation == 270:
            region_words = sorted(region_words_raw, key=lambda w: (w[0], w[1]))
        elif rotation == 90:
            region_words = sorted(region_words_raw, key=lambda w: (-w[0], -w[1]))
        elif rotation == 180:
            region_words = sorted(region_words_raw, key=lambda w: (-w[1], -w[0]))
        else:
            region_words = sorted(region_words_raw, key=lambda w: (w[1], w[0]))

        text = self._words_to_text(region_words, 4, rotation)
        if not text:
            return None

        # Validate: at least one line should look like address content
        # (digit-starting street, state abbreviation, ZIP, country name)
        if _looks_like_address(text):
            return text

        return None

    # -- Anchor finding -----------------------------------------------------

    @staticmethod
    def _find_anchor(
        words: list[tuple],
        anchor_text: str,
        rotation: int = 0,
    ) -> list[tuple] | None:
        """Find the word(s) matching the anchor text on the page.

        Handles multi-word anchors (e.g., "Tax No") by finding consecutive
        words whose concatenation matches.  For rotated pages (90/270),
        "same line" means similar x values rather than similar y values.

        Returns the matched word tuples, or ``None`` if not found.
        """
        if not anchor_text:
            return None

        anchor_parts = anchor_text.strip().split()
        if not anchor_parts:
            return None

        # For rotated pages, "same line" uses x-axis instead of y-axis
        same_line_idx = 0 if rotation in (90, 270) else 1

        # Single-word anchor
        if len(anchor_parts) == 1:
            target = anchor_parts[0].lower().rstrip(_ANCHOR_STRIP_CHARS)
            for w in words:
                word_text = w[4].lower().rstrip(_ANCHOR_STRIP_CHARS)
                if word_text == target:
                    return [w]
            return None

        # Multi-word anchor: find consecutive words on the same line
        target_parts = [p.lower().rstrip(_ANCHOR_STRIP_CHARS) for p in anchor_parts]
        for i in range(len(words) - len(target_parts) + 1):
            candidate = words[i : i + len(target_parts)]
            # Check all words are on roughly the same line
            if not all(
                abs(candidate[0][same_line_idx] - c[same_line_idx]) < _LINE_TOLERANCE
                for c in candidate
            ):
                continue
            texts = [c[4].lower().rstrip(_ANCHOR_STRIP_CHARS) for c in candidate]
            if texts == target_parts:
                return list(candidate)

        return None

    # -- Region computation -------------------------------------------------

    @staticmethod
    def _compute_region(
        anchor_bbox: tuple[float, float, float, float],
        field: FieldMapping,
        page: object,
        rotation: int = 0,
    ) -> tuple[float, float, float, float] | None:
        """Compute the search region based on spatial_relationship + rotation.

        For rotation=0 (standard layout): "right" = +x, "below" = +y.
        For rotation=270: visual "right" = +y at same x band.
        For rotation=90:  visual "right" = -y at same x band.
        For rotation=180: visual "right" = -x, "below" = -y.

        Returns ``(x0, y0, x1, y1)`` or ``None`` if the relationship
        is not recognized.
        """
        ax0, ay0, ax1, ay1 = anchor_bbox
        page_width = page.rect.width
        page_height = page.rect.height

        rel = field.spatial_relationship

        # Parse lines_below_N
        lines_n = None
        if rel.startswith("lines_below_"):
            try:
                lines_n = int(rel.split("_")[-1])
            except ValueError:
                lines_n = 2

        # --- Rotation 0 (standard) ---
        if rotation == 0:
            line_height = (ay1 - ay0) or 15

            if rel == "same_line_right":
                # Limit height to 1.5x anchor line height to avoid
                # capturing words from the next record on the same page.
                max_h = line_height * 1.5
                return (ax1 + 5, ay0 - 5, page_width - 20,
                        min(ay1 + 5, ay0 + max_h))
            if rel == "same_line_left":
                return (20, ay0 - 5, ax0 - 5, ay1 + 5)
            if rel == "line_below":
                return (ax0 - 50, ay1, page_width - 20, ay1 + line_height * 1.8)
            if lines_n is not None:
                return (ax0 - 50, ay1, page_width - 20, ay1 + line_height * lines_n * 1.5)
            if rel == "region_right":
                return (ax1 + 5, ay0 - 5, page_width - 20,
                        ay1 + line_height * max(field.line_count, 1) * 1.5)

        # --- Rotation 270 ---
        # Visual "right" = increasing y at same x band
        # Visual "below" = decreasing x
        elif rotation == 270:
            line_height = (ax1 - ax0) or 15  # "line height" is x-extent
            # Tight x-band padding (< 0.5 line_height) to avoid adjacent lines
            xpad = min(2, line_height * 0.3)

            if rel == "same_line_right":
                return (ax0 - xpad, ay1 + 5, ax1 + xpad, page_height - 20)
            if rel == "same_line_left":
                return (ax0 - xpad, 20, ax1 + xpad, ay0 - 5)
            if rel == "line_below":
                # Visual "below" = decreasing x for rotation 270
                return (ax0 - line_height * 1.8, ay0 - 50, ax0, page_height - 20)
            if lines_n is not None:
                return (ax0 - line_height * lines_n * 1.5, ay0 - 50, ax0, page_height - 20)
            if rel == "region_right":
                return (ax0 - xpad, ay1 + 5,
                        ax1 + line_height * max(field.line_count, 1) * 1.5,
                        page_height - 20)

        # --- Rotation 90 ---
        # Visual "right" = decreasing y at same x band
        # Visual "below" = increasing x
        elif rotation == 90:
            line_height = (ax1 - ax0) or 15
            xpad = min(2, line_height * 0.3)

            if rel == "same_line_right":
                return (ax0 - xpad, 20, ax1 + xpad, ay0 - 5)
            if rel == "same_line_left":
                return (ax0 - xpad, ay1 + 5, ax1 + xpad, page_height - 20)
            if rel == "line_below":
                # Visual "below" = increasing x for rotation 90
                return (ax1, ay0 - 50, ax1 + line_height * 1.8, page_height - 20)
            if lines_n is not None:
                return (ax1, ay0 - 50, ax1 + line_height * lines_n * 1.5, page_height - 20)
            if rel == "region_right":
                return (ax0 - line_height * max(field.line_count, 1) * 1.5,
                        20, ax1 + xpad, ay0 - 5)

        # --- Rotation 180 ---
        # Visual "right" = -x, "below" = -y
        elif rotation == 180:
            line_height = (ay1 - ay0) or 15

            if rel == "same_line_right":
                return (20, ay0 - 5, ax0 - 5, ay1 + 5)
            if rel == "same_line_left":
                return (ax1 + 5, ay0 - 5, page_width - 20, ay1 + 5)
            if rel == "line_below":
                return (20, ay0 - line_height * 1.8, page_width - 20, ay0)
            if lines_n is not None:
                return (20, ay0 - line_height * lines_n * 1.5, page_width - 20, ay0)
            if rel == "region_right":
                return (20, ay0 - line_height * max(field.line_count, 1) * 1.5,
                        ax0 - 5, ay1 + 5)

        # Fallback: treat unknown relationship as same_line_right at rotation 0
        logger.warning(
            "Unknown spatial_relationship %r (rotation=%d), defaulting to same_line_right",
            rel, rotation,
        )
        return (ax1 + 5, ay0 - 5, page_width - 20, ay1 + 5)

    # -- Geometry helpers ---------------------------------------------------

    @staticmethod
    def _merge_bboxes(
        word_tuples: list[tuple],
    ) -> tuple[float, float, float, float]:
        """Merge multiple word bounding boxes into one encompassing bbox."""
        x0 = min(w[0] for w in word_tuples)
        y0 = min(w[1] for w in word_tuples)
        x1 = max(w[2] for w in word_tuples)
        y1 = max(w[3] for w in word_tuples)
        return (x0, y0, x1, y1)

    @staticmethod
    def _in_region(
        word: tuple,
        region: tuple[float, float, float, float],
    ) -> bool:
        """Check if a word's center point falls within the region."""
        wx = (word[0] + word[2]) / 2
        wy = (word[1] + word[3]) / 2
        return (
            region[0] <= wx <= region[2]
            and region[1] <= wy <= region[3]
        )

    @staticmethod
    def _words_to_text(
        region_words: list[tuple],
        line_count: int,
        rotation: int = 0,
    ) -> str:
        """Join words into text, respecting line boundaries.

        Groups words by vertical position (or x-position for rotated pages)
        and joins with spaces (within line) and newlines (between lines).
        Limits output to ``line_count`` lines.

        Large gaps (>50pt) in the reading direction within a line are
        treated as line breaks to avoid capturing unrelated text from
        the same row on rotated pages.
        """
        if not region_words:
            return ""

        # For rotated pages, "same line" = same x band; reading order within
        # a line is by y-position.  For standard pages, same y band, reading
        # order by x-position.
        if rotation in (90, 270):
            group_idx = 0   # group by x
            sort_idx = 1    # sort within line by y
        else:
            group_idx = 1   # group by y
            sort_idx = 0    # sort within line by x

        # Group by cross-line axis
        raw_lines: list[list[tuple]] = []
        current_line: list[tuple] = [region_words[0]]
        for w in region_words[1:]:
            if abs(w[group_idx] - current_line[0][group_idx]) > _LINE_TOLERANCE:
                raw_lines.append(current_line)
                current_line = [w]
            else:
                current_line.append(w)
        raw_lines.append(current_line)

        # Split each line at large gaps in the reading direction.
        # On rotated pages, a single x-band can span the full page height,
        # so a 50pt+ gap indicates unrelated text (e.g., a second section).
        _GAP_THRESHOLD = 50
        lines: list[list[tuple]] = []
        for raw_line in raw_lines:
            sorted_words = sorted(raw_line, key=lambda w: w[sort_idx])
            current: list[tuple] = [sorted_words[0]]
            for w in sorted_words[1:]:
                # gap = start of next word - end of previous word
                gap = w[sort_idx] - current[-1][sort_idx + 2]
                if gap > _GAP_THRESHOLD:
                    lines.append(current)
                    current = [w]
                else:
                    current.append(w)
            lines.append(current)

        # Sort words within each line by reading-order axis
        text_lines = []
        for line_words in lines[:line_count]:
            sorted_words = sorted(line_words, key=lambda w: w[sort_idx])
            text_lines.append(" ".join(w[4] for w in sorted_words))

        return "\n".join(text_lines).strip()