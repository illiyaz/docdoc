"""Canonical entity type to data category mapping.

Every Presidio entity type registered in our engine (both custom patterns
from ``app.pii.layer1_patterns`` and Presidio built-in recognisers) is mapped
to one or more data categories.

An entity type may belong to multiple categories. For example, ``US_SSN``
is both **PII** (personally identifiable) and **SPII** (sensitive PII).
``CREDIT_CARD`` is both **PFI** and **PCI**.

Eight categories
----------------
PII   — Personally Identifiable Information (catch-all baseline)
SPII  — Sensitive PII (SSN, biometrics, government IDs — higher risk tier)
PHI   — Protected Health Information (HIPAA)
PFI   — Personal Financial Information (GLBA / financial regs)
PCI   — Payment Card Industry data (PCI-DSS)
NPI   — Nonpublic Personal Information (GLBA / banking)
FTI   — Federal Tax Information (IRS 1075)
CREDENTIALS — Authentication secrets (passwords, API keys, tokens)
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Data category metadata
# ---------------------------------------------------------------------------

DATA_CATEGORIES: dict[str, dict[str, str]] = {
    "PII": {
        "label": "Personally Identifiable Information",
        "regulation": "CCPA/GDPR/PIPEDA/DPDP",
    },
    "SPII": {
        "label": "Sensitive Personally Identifiable Information",
        "regulation": "NIST SP 800-122 / OMB M-07-16",
    },
    "PHI": {
        "label": "Protected Health Information",
        "regulation": "HIPAA / HITECH",
    },
    "PFI": {
        "label": "Personal Financial Information",
        "regulation": "GLBA / CCPA / PCI-DSS",
    },
    "PCI": {
        "label": "Payment Card Industry Data",
        "regulation": "PCI-DSS",
    },
    "NPI": {
        "label": "Nonpublic Personal Information",
        "regulation": "GLBA / Reg S-P",
    },
    "FTI": {
        "label": "Federal Tax Information",
        "regulation": "IRS Publication 1075 / IRC 6103",
    },
    "CREDENTIALS": {
        "label": "Authentication Credentials",
        "regulation": "NIST SP 800-63 / SOC 2",
    },
}

VALID_CATEGORIES: frozenset[str] = frozenset(DATA_CATEGORIES.keys())

# ---------------------------------------------------------------------------
# Entity type -> category mapping
#
# Every entity type from:
#   1. app/pii/layer1_patterns.py  (custom PatternDefinitions)
#   2. Presidio built-in recognisers (EMAIL_ADDRESS, PHONE_NUMBER, etc.)
#
# Unmapped types default to ["PII"] via get_entity_categories().
# ---------------------------------------------------------------------------

ENTITY_CATEGORY_MAP: dict[str, list[str]] = {
    # ==================================================================
    # GLOBAL custom patterns
    # ==================================================================
    "EMAIL": ["PII"],
    "PHONE_INTL": ["PII"],
    "CREDIT_CARD": ["PFI", "PCI"],
    "IBAN": ["PFI", "NPI"],
    "IPV4": ["PII"],
    "IPV6": ["PII"],
    "DATE_OF_BIRTH_DMY": ["PII", "SPII"],
    "DATE_OF_BIRTH_MDY": ["PII", "SPII"],
    "DATE_OF_BIRTH_ISO": ["PII", "SPII"],
    "GPS_COORDINATES": ["PII"],
    "PASSPORT_ICAO": ["PII", "SPII"],

    # ==================================================================
    # UNITED STATES custom patterns
    # ==================================================================
    "SSN": ["PII", "SPII"],
    "SSN_NODASH": ["PII", "SPII"],
    "PHONE_US": ["PII"],
    "DRIVER_LICENSE_US": ["PII", "SPII"],
    "EIN": ["PII", "FTI"],
    "BANK_ROUTING_US": ["PFI", "NPI"],
    "MEDICARE_BENEFICIARY_ID": ["PHI"],

    # ==================================================================
    # INDIA custom patterns
    # ==================================================================
    "AADHAAR": ["PII", "SPII"],
    "PAN": ["PII", "FTI"],
    "PASSPORT_IN": ["PII", "SPII"],
    "MOBILE_IN": ["PII"],
    "VOTER_ID_IN": ["PII", "SPII"],
    "DRIVER_LICENSE_IN": ["PII", "SPII"],
    "GST_NUMBER": ["PII", "FTI"],

    # ==================================================================
    # UNITED KINGDOM custom patterns
    # ==================================================================
    "NATIONAL_INSURANCE_UK": ["PII", "SPII"],
    "NHS_NUMBER": ["PHI"],
    "PASSPORT_UK": ["PII", "SPII"],
    "SORT_CODE_UK": ["PFI", "NPI"],
    "COMPANY_NUMBER_UK": ["PII"],

    # ==================================================================
    # EUROPEAN UNION custom patterns
    # ==================================================================
    "VAT_EU": ["PII"],
    "NATIONAL_ID_DE": ["PII", "SPII"],
    "INSEE_FR": ["PII", "SPII"],
    "DNI_NIE_ES": ["PII", "SPII"],
    "CODICE_FISCALE_IT": ["PII", "SPII", "FTI"],

    # ==================================================================
    # CANADA custom patterns
    # ==================================================================
    "SIN_CA": ["PII", "SPII"],
    "PASSPORT_CA": ["PII", "SPII"],
    "HEALTH_CARD_CA": ["PHI"],

    # ==================================================================
    # AUSTRALIA custom patterns
    # ==================================================================
    "TAX_FILE_NUMBER_AU": ["PII", "FTI"],
    "MEDICARE_AU": ["PHI"],
    "ABN_AU": ["PII"],
    "PASSPORT_AU": ["PII", "SPII"],

    # ==================================================================
    # PHI — Protected Health Information (US / HIPAA)
    # ==================================================================
    "MRN": ["PHI"],
    "NPI": ["PHI"],
    "DEA_NUMBER": ["PHI"],
    "HICN": ["PHI"],
    "HEALTH_PLAN_BENEFICIARY": ["PHI"],
    "ICD10_CODE": ["PHI"],

    # ==================================================================
    # FERPA — student records
    # ==================================================================
    "STUDENT_ID": ["PII", "SPII"],

    # ==================================================================
    # SPI / PPRA
    # ==================================================================
    "BIOMETRIC_IDENTIFIER": ["PII", "SPII"],
    "FINANCIAL_ACCOUNT_PAIR": ["PFI", "NPI"],
    "SURVEY_RESPONSE": ["PII"],

    # ==================================================================
    # Presidio built-in recogniser entity types
    # ==================================================================
    "EMAIL_ADDRESS": ["PII"],
    "PHONE_NUMBER": ["PII"],
    "PERSON": ["PII"],
    "LOCATION": ["PII"],
    "DATE_TIME": ["PII"],
    "NRP": ["PII", "SPII"],  # nationality / religious / political group
    "URL": ["PII"],
    "IP_ADDRESS": ["PII"],
    "CRYPTO": ["PFI"],  # cryptocurrency wallet address
    "MEDICAL_LICENSE": ["PHI"],
    "US_SSN": ["PII", "SPII"],
    "US_BANK_NUMBER": ["PFI", "NPI"],
    "US_DRIVER_LICENSE": ["PII", "SPII"],
    "US_ITIN": ["PII", "FTI"],
    "US_PASSPORT": ["PII", "SPII"],
    "UK_NHS": ["PHI"],

    # ==================================================================
    # CREDENTIALS — authentication secrets
    # ==================================================================
    "PASSWORD": ["CREDENTIALS"],
    "API_KEY": ["CREDENTIALS"],
    "AWS_ACCESS_KEY": ["CREDENTIALS"],
    "AZURE_KEY": ["CREDENTIALS"],
}


def get_entity_categories(entity_type: str) -> list[str]:
    """Return the list of data categories for the given entity type.

    Parameters
    ----------
    entity_type:
        The Presidio entity type string (e.g. ``"US_SSN"``, ``"MRN"``).
        Lookup is case-insensitive.

    Returns
    -------
    list[str]
        One or more category codes from :data:`VALID_CATEGORIES`.
        Unmapped entity types default to ``["PII"]``.
    """
    # Try exact match first (most entity types are uppercase)
    cats = ENTITY_CATEGORY_MAP.get(entity_type)
    if cats is not None:
        return list(cats)

    # Case-insensitive fallback
    upper = entity_type.upper()
    for key, value in ENTITY_CATEGORY_MAP.items():
        if key.upper() == upper:
            return list(value)

    return ["PII"]
