"""Layer 1 regex pattern library: domain-specific custom PII recognisers.

These PatternDefinitions are loaded by PresidioEngine as custom Presidio
recognisers in addition to Presidio's built-in detectors.

Each pattern carries a geography tag and a regulatory_framework string so
that audit records can trace every detection decision back to a specific
rule and the regulation it satisfies.

Geography codes
---------------
GLOBAL  — active for every jurisdiction
US      — United States
IN      — India
UK      — United Kingdom
EU      — European Union
CA      — Canada
AU      — Australia

Score semantics
---------------
≥ 0.85  very specific format, low false-positive rate
  0.80  high confidence, minor ambiguity possible
  0.75  moderate confidence — context may be needed
  0.70  overlaps with non-PII patterns — Layer 2 required to confirm
< 0.70  low confidence — Layer 2 or Layer 3 mandatory
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Geography constants
# ---------------------------------------------------------------------------
GEOGRAPHY_GLOBAL = "GLOBAL"
GEOGRAPHY_US = "US"
GEOGRAPHY_IN = "IN"
GEOGRAPHY_UK = "UK"
GEOGRAPHY_EU = "EU"
GEOGRAPHY_CA = "CA"
GEOGRAPHY_AU = "AU"


# ---------------------------------------------------------------------------
# Pattern definition
# ---------------------------------------------------------------------------

@dataclass
class PatternDefinition:
    """A single custom PII pattern recogniser for Presidio.

    Attributes
    ----------
    name:                  Human-readable identifier used as the Presidio
                           recogniser name.
    entity_type:           Presidio entity type string (used in audit records).
    regex:                 Regular expression; matched with re.search unless
                           a word boundary is embedded in the pattern.
    score:                 Default confidence score when the pattern fires.
    geography:             Jurisdiction scope (see module-level constants).
    regulatory_framework:  Slash-separated list of frameworks (GDPR/HIPAA/…).
    """
    name: str
    entity_type: str
    regex: str
    score: float
    geography: str
    regulatory_framework: str


# ---------------------------------------------------------------------------
# Luhn (Mod-10) check — post-filter for credit/debit card matches
# ---------------------------------------------------------------------------

def luhn_check(number_str: str) -> bool:
    """Return True if *number_str* passes the Luhn (Mod-10) algorithm.

    Non-digit characters are stripped before checking so that card numbers
    with spaces or dashes are handled transparently.
    """
    digits = [int(c) for c in number_str if c.isdigit()]
    if not digits:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:          # double every second digit from the right
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Custom pattern catalogue
# ---------------------------------------------------------------------------

CUSTOM_PATTERNS: list[PatternDefinition] = [

    # =====================================================================
    # GLOBAL — always active regardless of geography setting
    # =====================================================================

    PatternDefinition(
        name="email",
        entity_type="EMAIL",
        regex=r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        score=0.85,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="GDPR/CCPA/PIPEDA",
    ),
    PatternDefinition(
        name="phone_international",
        entity_type="PHONE_INTL",
        regex=r"\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{1,4}[\s\-]?\d{1,9}",
        score=0.75,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="GLOBAL",
    ),
    PatternDefinition(
        # Post-filter: apply luhn_check() to eliminate false positives.
        name="credit_card",
        entity_type="CREDIT_CARD",
        regex=r"\b(?:\d[ \-]?){13,18}\d\b",
        score=0.80,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="PCI-DSS",
    ),
    PatternDefinition(
        name="iban",
        entity_type="IBAN",
        regex=r"\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b",
        score=0.85,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="GDPR/PCI-DSS",
    ),
    PatternDefinition(
        name="ipv4",
        entity_type="IPV4",
        regex=(
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){{3}}"
            r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
        ).replace("{{", "{").replace("}}", "}"),
        score=0.80,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="GDPR/CCPA",
    ),
    PatternDefinition(
        name="ipv6",
        entity_type="IPV6",
        regex=r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b",
        score=0.80,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="GDPR/CCPA",
    ),
    PatternDefinition(
        # Lower score — Layer 2 context required to confirm DOB vs random date.
        name="date_of_birth_dmy",
        entity_type="DATE_OF_BIRTH_DMY",
        regex=r"\b(?:0?[1-9]|[12]\d|3[01])/(?:0?[1-9]|1[0-2])/(?:19|20)\d{2}\b",
        score=0.70,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="GDPR/CCPA/PIPEDA",
    ),
    PatternDefinition(
        name="date_of_birth_mdy",
        entity_type="DATE_OF_BIRTH_MDY",
        regex=r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/(?:19|20)\d{2}\b",
        score=0.70,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="GDPR/CCPA/PIPEDA",
    ),
    PatternDefinition(
        name="date_of_birth_iso",
        entity_type="DATE_OF_BIRTH_ISO",
        regex=r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b",
        score=0.70,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="GDPR/CCPA/PIPEDA",
    ),
    PatternDefinition(
        name="gps_coordinates",
        entity_type="GPS_COORDINATES",
        regex=(
            r"\b-?(?:[1-8]?\d(?:\.\d+)?|90(?:\.0+)?),"
            r"\s*-?(?:1[0-7]\d(?:\.\d+)?|[1-9]?\d(?:\.\d+)?|180(?:\.0+)?)\b"
        ),
        score=0.75,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="GDPR/CCPA",
    ),
    PatternDefinition(
        # Needs Layer 2 — "passport" keyword must appear nearby.
        name="passport_icao",
        entity_type="PASSPORT_ICAO",
        regex=r"\b[A-Z]{1,2}[0-9]{6,9}\b",
        score=0.70,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="GLOBAL",
    ),

    # =====================================================================
    # UNITED STATES
    # =====================================================================

    PatternDefinition(
        name="ssn_us",
        entity_type="SSN",
        regex=r"\b\d{3}-\d{2}-\d{4}\b",
        score=0.90,
        geography=GEOGRAPHY_US,
        regulatory_framework="HIPAA/CCPA",
    ),
    PatternDefinition(
        # No-dash variant — low confidence; Layer 2 / col_header context needed.
        name="ssn_us_nodash",
        entity_type="SSN_NODASH",
        regex=r"\b(?!000|666|9\d{2})\d{3}(?!00)\d{2}(?!0000)\d{4}\b",
        score=0.60,
        geography=GEOGRAPHY_US,
        regulatory_framework="HIPAA/CCPA",
    ),
    PatternDefinition(
        name="phone_us",
        entity_type="PHONE_US",
        regex=r"(?<!\w)(?:1[\s\-.])?(?:\(\d{3}\)|\d{3})[\s\-.]?\d{3}[\s\-.]?\d{4}(?!\w)",
        score=0.80,
        geography=GEOGRAPHY_US,
        regulatory_framework="TCPA/CCPA",
    ),
    PatternDefinition(
        # Generic — state-specific formats vary widely; Layer 2 context needed.
        name="driver_license_us",
        entity_type="DRIVER_LICENSE_US",
        regex=r"\b[A-Z]{0,2}\d{6,8}\b",
        score=0.65,
        geography=GEOGRAPHY_US,
        regulatory_framework="DPPA/CCPA",
    ),
    PatternDefinition(
        name="ein_us",
        entity_type="EIN",
        regex=r"\b\d{2}-\d{7}\b",
        score=0.80,
        geography=GEOGRAPHY_US,
        regulatory_framework="IRS/CCPA",
    ),
    PatternDefinition(
        name="bank_routing_us",
        entity_type="BANK_ROUTING_US",
        regex=r"\b(?:0[0-9]|1[0-2]|2[1-9]|3[0-2])\d{7}\b",
        score=0.75,
        geography=GEOGRAPHY_US,
        regulatory_framework="GLBA",
    ),
    PatternDefinition(
        name="medicare_beneficiary_id",
        entity_type="MEDICARE_BENEFICIARY_ID",
        regex=r"\b[1-9][A-Z][A-Z0-9]\d[A-Z][A-Z0-9]\d[A-Z]{2}\d{2}\b",
        score=0.85,
        geography=GEOGRAPHY_US,
        regulatory_framework="HIPAA",
    ),

    # =====================================================================
    # INDIA
    # =====================================================================

    PatternDefinition(
        # First digit is always 2-9 per UIDAI spec (0 and 1 are never issued).
        name="aadhaar",
        entity_type="AADHAAR",
        regex=r"\b[2-9]\d{3}[\s\-]?\d{4}[\s\-]?\d{4}\b",
        score=0.85,
        geography=GEOGRAPHY_IN,
        regulatory_framework="DPDP/IT-Act",
    ),
    PatternDefinition(
        # 4th character encodes taxpayer type: [ABCFGHLJPTF].
        name="pan_card",
        entity_type="PAN",
        regex=r"\b[A-Z]{3}[ABCFGHLJPTF][A-Z]\d{4}[A-Z]\b",
        score=0.95,
        geography=GEOGRAPHY_IN,
        regulatory_framework="IT-Act/DPDP",
    ),
    PatternDefinition(
        name="passport_in",
        entity_type="PASSPORT_IN",
        regex=r"\b[A-Z][1-9]\d{6}\b",
        score=0.80,
        geography=GEOGRAPHY_IN,
        regulatory_framework="DPDP",
    ),
    PatternDefinition(
        name="mobile_in",
        entity_type="MOBILE_IN",
        regex=r"\b(?:\+91[\s\-]?)?[6-9]\d{9}\b",
        score=0.85,
        geography=GEOGRAPHY_IN,
        regulatory_framework="DPDP/TRAI",
    ),
    PatternDefinition(
        name="voter_id_in",
        entity_type="VOTER_ID_IN",
        regex=r"\b[A-Z]{3}\d{7}\b",
        score=0.75,
        geography=GEOGRAPHY_IN,
        regulatory_framework="DPDP",
    ),
    PatternDefinition(
        name="driver_license_in",
        entity_type="DRIVER_LICENSE_IN",
        regex=r"\b[A-Z]{2}\d{2}\s?\d{11}\b",
        score=0.75,
        geography=GEOGRAPHY_IN,
        regulatory_framework="DPDP",
    ),
    PatternDefinition(
        name="gst_in",
        entity_type="GST_NUMBER",
        regex=r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]\b",
        score=0.90,
        geography=GEOGRAPHY_IN,
        regulatory_framework="GST-Act",
    ),

    # =====================================================================
    # UNITED KINGDOM
    # =====================================================================

    PatternDefinition(
        name="national_insurance_uk",
        entity_type="NATIONAL_INSURANCE_UK",
        regex=r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z]{2}\d{6}[ABCD]\b",
        score=0.95,
        geography=GEOGRAPHY_UK,
        regulatory_framework="UK-GDPR",
    ),
    PatternDefinition(
        # Modulus 11 check not enforced by regex; Layer 2 required.
        # Overlaps with phone patterns — low score.
        name="nhs_number",
        entity_type="NHS_NUMBER",
        regex=r"\b\d{3}[\s\-]?\d{3}[\s\-]?\d{4}\b",
        score=0.70,
        geography=GEOGRAPHY_UK,
        regulatory_framework="UK-GDPR/DSPT",
    ),
    PatternDefinition(
        # Just 9 digits — Layer 2 "passport" keyword essential.
        name="passport_uk",
        entity_type="PASSPORT_UK",
        regex=r"\b\d{9}\b",
        score=0.60,
        geography=GEOGRAPHY_UK,
        regulatory_framework="UK-GDPR",
    ),
    PatternDefinition(
        name="sort_code_uk",
        entity_type="SORT_CODE_UK",
        regex=r"\b\d{2}-\d{2}-\d{2}\b",
        score=0.75,
        geography=GEOGRAPHY_UK,
        regulatory_framework="UK-GDPR/PSD2",
    ),
    PatternDefinition(
        name="company_number_uk",
        entity_type="COMPANY_NUMBER_UK",
        regex=r"\b(?:OC|NI|SC|NL|LP|R|IP|SP|RS|FC|GE|GS|IC|CE|CS|AC|SA|NA|SL|[A-Z]{2})?\d{6,8}\b",
        score=0.70,
        geography=GEOGRAPHY_UK,
        regulatory_framework="UK-GDPR",
    ),

    # =====================================================================
    # EUROPEAN UNION
    # =====================================================================

    PatternDefinition(
        # "VAT" keyword must be nearby — Layer 2 required.
        name="vat_eu",
        entity_type="VAT_EU",
        regex=r"\b[A-Z]{2}[\dA-Z]{8,12}\b",
        score=0.75,
        geography=GEOGRAPHY_EU,
        regulatory_framework="GDPR/VAT-Directive",
    ),
    PatternDefinition(
        name="personalausweis_de",
        entity_type="NATIONAL_ID_DE",
        regex=r"\b[LMNPRTVWXY][A-Z0-9]{3}\d{5}[A-Z0-9]\b",
        score=0.85,
        geography=GEOGRAPHY_EU,
        regulatory_framework="GDPR/BDSG",
    ),
    PatternDefinition(
        name="insee_fr",
        entity_type="INSEE_FR",
        regex=r"\b[12]\s?\d{2}\s?\d{2}\s?\d{2}\s?\d{3}\s?\d{3}\s?\d{2}\b",
        score=0.85,
        geography=GEOGRAPHY_EU,
        regulatory_framework="GDPR/CNIL",
    ),
    PatternDefinition(
        name="dni_nie_es",
        entity_type="DNI_NIE_ES",
        regex=r"\b(?:\d{8}[A-HJ-NP-TV-Z]|[XYZ]\d{7}[A-HJ-NP-TV-Z])\b",
        score=0.90,
        geography=GEOGRAPHY_EU,
        regulatory_framework="GDPR/LOPDGDD",
    ),
    PatternDefinition(
        name="codice_fiscale_it",
        entity_type="CODICE_FISCALE_IT",
        regex=r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b",
        score=0.90,
        geography=GEOGRAPHY_EU,
        regulatory_framework="GDPR/Codice-Privacy",
    ),

    # =====================================================================
    # CANADA
    # =====================================================================

    PatternDefinition(
        # Overlaps with phone; Layer 2 required.
        name="sin_ca",
        entity_type="SIN_CA",
        regex=r"\b\d{3}[\s\-]?\d{3}[\s\-]?\d{3}\b",
        score=0.80,
        geography=GEOGRAPHY_CA,
        regulatory_framework="PIPEDA",
    ),
    PatternDefinition(
        name="passport_ca",
        entity_type="PASSPORT_CA",
        regex=r"\b[A-Z]{2}\d{6}\b",
        score=0.80,
        geography=GEOGRAPHY_CA,
        regulatory_framework="PIPEDA",
    ),
    PatternDefinition(
        # "health card" context essential — very broad pattern.
        name="health_card_ca",
        entity_type="HEALTH_CARD_CA",
        regex=r"\b\d{10}\b",
        score=0.60,
        geography=GEOGRAPHY_CA,
        regulatory_framework="PHIPA/PIPEDA",
    ),

    # =====================================================================
    # AUSTRALIA
    # =====================================================================

    PatternDefinition(
        # Overlaps with phone and other 8-9 digit numbers; Layer 2 needed.
        name="tfn_au",
        entity_type="TAX_FILE_NUMBER_AU",
        regex=r"\b\d{3}[\s\-]?\d{3}[\s\-]?\d{2,3}\b",
        score=0.75,
        geography=GEOGRAPHY_AU,
        regulatory_framework="Privacy-Act-AU",
    ),
    PatternDefinition(
        name="medicare_au",
        entity_type="MEDICARE_AU",
        regex=r"\b\d{10}[\-/]\d\b",
        score=0.90,
        geography=GEOGRAPHY_AU,
        regulatory_framework="Privacy-Act-AU/My-Health-Records-Act",
    ),
    PatternDefinition(
        name="abn_au",
        entity_type="ABN_AU",
        regex=r"\b\d{2}\s?\d{3}\s?\d{3}\s?\d{3}\b",
        score=0.85,
        geography=GEOGRAPHY_AU,
        regulatory_framework="Privacy-Act-AU",
    ),
    PatternDefinition(
        name="passport_au",
        entity_type="PASSPORT_AU",
        regex=r"\b[A-Z]\d{8}\b",
        score=0.80,
        geography=GEOGRAPHY_AU,
        regulatory_framework="Privacy-Act-AU",
    ),

    # =====================================================================
    # PHI — Protected Health Information (US / HIPAA)
    # =====================================================================

    PatternDefinition(
        # Low score — "MRN" label or "medical record" keyword required nearby.
        name="mrn",
        entity_type="MRN",
        regex=r"\bMRN[:\s#]*\d{5,10}\b",
        score=0.70,
        geography=GEOGRAPHY_US,
        regulatory_framework="HIPAA",
    ),
    PatternDefinition(
        # National Provider Identifier — 10 digits, broad pattern.
        # Layer 2: "NPI" or "provider" keyword must appear in context window.
        name="npi",
        entity_type="NPI",
        regex=r"\b\d{10}\b",
        score=0.65,
        geography=GEOGRAPHY_US,
        regulatory_framework="HIPAA",
    ),
    PatternDefinition(
        # DEA registration number: 2 letters + 7 digits.
        name="dea_number",
        entity_type="DEA_NUMBER",
        regex=r"\b[A-Z]{2}\d{7}\b",
        score=0.80,
        geography=GEOGRAPHY_US,
        regulatory_framework="HIPAA",
    ),
    PatternDefinition(
        # Health Insurance Claim Number (legacy Medicare format).
        name="hicn",
        entity_type="HICN",
        regex=r"\b\d{9}[A-Z]\b",
        score=0.75,
        geography=GEOGRAPHY_US,
        regulatory_framework="HIPAA",
    ),
    PatternDefinition(
        # Health plan beneficiary identifier — plan-specific prefix + 8–12 alphanum chars.
        name="health_plan_beneficiary",
        entity_type="HEALTH_PLAN_BENEFICIARY",
        regex=r"\bHP[A-Z0-9]{8,12}\b",
        score=0.70,
        geography=GEOGRAPHY_US,
        regulatory_framework="HIPAA",
    ),
    PatternDefinition(
        # ICD-10 diagnosis code — low score; "diagnosis" or "dx" keyword essential.
        name="icd10_code",
        entity_type="ICD10_CODE",
        regex=r"\b[A-Z]\d{2}(?:\.\d{1,4})?\b",
        score=0.60,
        geography=GEOGRAPHY_US,
        regulatory_framework="HIPAA",
    ),

    # =====================================================================
    # FERPA — Family Educational Rights and Privacy Act (US)
    # =====================================================================

    PatternDefinition(
        # Student identifiers — low score; col_header "student" boosts via Layer 3.
        name="student_id",
        entity_type="STUDENT_ID",
        regex=r"\b(?:STU|SID|S)[A-Z0-9\-]{4,12}\b",
        score=0.65,
        geography=GEOGRAPHY_US,
        regulatory_framework="FERPA",
    ),

    # =====================================================================
    # SPI — Sensitive Personal Information (GLOBAL / CCPA-GDPR)
    # =====================================================================

    PatternDefinition(
        # Biometric descriptors — phrase-based, context driven.
        name="biometric_identifier",
        entity_type="BIOMETRIC_IDENTIFIER",
        regex=r"\b(?:fingerprint|retinal|iris|biometric)\s+(?:id|identifier|scan|record)\b",
        score=0.60,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="CCPA/GDPR",
    ),
    PatternDefinition(
        # Routing number immediately followed (within ~20 chars) by account number.
        # Detects the common "routing … account" pair in financial documents.
        name="financial_account_pair",
        entity_type="FINANCIAL_ACCOUNT_PAIR",
        regex=r"\b\d{9}\b.{0,20}\b\d{8,17}\b",
        score=0.70,
        geography=GEOGRAPHY_GLOBAL,
        regulatory_framework="CCPA/GDPR",
    ),

    # =====================================================================
    # PPRA — Protection of Pupil Rights Amendment (US)
    # =====================================================================

    PatternDefinition(
        # Survey / questionnaire response markers — extremely low confidence.
        # Always requires Layer 3 col_header confirmation ("survey", "response",
        # "questionnaire") before being acted upon; never fires alone.
        name="survey_response",
        entity_type="SURVEY_RESPONSE",
        regex=r"\b(?:survey|questionnaire|response|answer)\b",
        score=0.55,
        geography=GEOGRAPHY_US,
        regulatory_framework="PPRA",
    ),
]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_all_patterns(geographies: list[str] | None = None) -> list[PatternDefinition]:
    """Return PatternDefinition objects filtered by geography.

    Parameters
    ----------
    geographies:
        If None, return all patterns.
        Otherwise, return patterns whose geography is in the list OR is
        GLOBAL (GLOBAL patterns are always included).
    """
    if geographies is None:
        return list(CUSTOM_PATTERNS)
    geo_set = set(geographies)
    return [
        p for p in CUSTOM_PATTERNS
        if p.geography == GEOGRAPHY_GLOBAL or p.geography in geo_set
    ]


def get_pattern_geographies() -> list[str]:
    """Return sorted list of all distinct geography codes in CUSTOM_PATTERNS."""
    return sorted({p.geography for p in CUSTOM_PATTERNS})
