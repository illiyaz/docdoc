"""LLM extraction prompt generator for template documents (Step 19).

Generates extraction prompts FROM the DocumentSchema's ``pii_fields_expected``.
NOT hardcoded to any document type — different schemas produce different prompts.

``ENTITY_EXTRACTION_GUIDE`` maps entity type names to human-readable extraction
instructions.  ``build_extraction_prompt()`` and ``build_batch_extraction_prompt()``
assemble prompts that tell the LLM exactly which fields to extract.

``ALWAYS_EXTRACT_IF_PRESENT`` ensures commonly-missed PII types (NI_NUMBER,
US_SSN, DATE_OF_BIRTH, etc.) are always requested even if the LLM's schema
analysis omitted them from ``pii_fields_expected``.
"""
from __future__ import annotations

from app.structure.document_schema import PageRole

# ---------------------------------------------------------------------------
# Fields to ALWAYS include in extraction prompts regardless of schema
# ---------------------------------------------------------------------------
# The LLM schema analysis sometimes misses government IDs and dates.
# By always asking for these, we catch NI_NUMBERs, SSNs, DOBs etc.

ALWAYS_EXTRACT_IF_PRESENT: frozenset[str] = frozenset({
    "PERSON",
    "LOCATION",
    "DATE_OF_BIRTH",
    "NI_NUMBER",
    "US_SSN",
    "AADHAAR",
    "PAN_CARD",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
})

# ---------------------------------------------------------------------------
# Entity type → extraction instruction mapping
# ---------------------------------------------------------------------------
# This is the ONLY configuration — it tells the LLM what each field type
# looks like.  It is NOT document-type-specific.

ENTITY_EXTRACTION_GUIDE: dict[str, str] = {
    "PERSON": (
        "Full name including title (Mr/Mrs/Dr) if present"
    ),
    "LOCATION": (
        "Complete address — street, area, city, county, postcode, country. "
        "Combine all address lines into one value."
    ),
    "DATE_OF_BIRTH": (
        "Date of birth in original format. "
        "Look for labels like 'Date of Birth', 'DOB', 'Born'."
    ),
    "US_SSN": "Social Security Number (XXX-XX-XXXX format)",
    "NI_NUMBER": (
        "UK National Insurance Number (2 letters + 6 digits + 1 letter, "
        "e.g., NE724362D). Look for labels like 'National Insurance Number', "
        "'NI No', 'NINO'."
    ),
    "NHS_NUMBER": "UK NHS Number (10 digits, usually formatted XXX XXX XXXX)",
    "EMAIL_ADDRESS": "Email address",
    "PHONE_NUMBER": "Phone number in any format",
    "CREDIT_CARD": "Credit/debit card number",
    "US_DRIVER_LICENSE": "Driver's license number",
    "US_PASSPORT": "Passport number",
    "AADHAAR": "Indian Aadhaar number (12 digits)",
    "PAN_CARD": "Indian PAN (5 letters + 4 digits + 1 letter)",
    "IBAN_CODE": "IBAN bank account number",
    "US_BANK_NUMBER": "Bank account or routing number",
    "MEDICAL_LICENSE": "Medical license or registration number",
    "NPI_NUMBER": "National Provider Identifier (10 digits)",
    "IP_ADDRESS": "IP address (IPv4 or IPv6)",
    "URL": "URL or web address",
    "CRYPTO": "Cryptocurrency wallet address",
}


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def build_extraction_prompt(
    page_texts: list[str],
    page_roles: list[PageRole],
    instance_index: int,
    document_type: str,
) -> str:
    """Build an LLM extraction prompt from the DocumentSchema.

    The prompt tells the LLM:
    1. What type of document this is
    2. How many pages to read
    3. What fields to extract (from ``page_roles.pii_fields_expected``)
    4. Expected format for each field type

    Returns a prompt string.  NOT hardcoded to any document type.
    """
    # Collect all expected PII fields across all page roles + always-extract set
    all_fields: set[str] = set()
    for role in page_roles:
        all_fields.update(role.pii_fields_expected)
    all_fields.update(ALWAYS_EXTRACT_IF_PRESENT)

    sorted_fields = sorted(all_fields)

    # Build field instructions
    field_instructions: list[str] = []
    for f in sorted_fields:
        instruction = ENTITY_EXTRACTION_GUIDE.get(f, f"Extract any {f} values")
        field_instructions.append(f"- {f}: {instruction}")

    # Build page text sections
    page_sections: list[str] = []
    for i, text in enumerate(page_texts):
        if text.strip():
            page_sections.append(f"--- PAGE {i + 1} ---\n{text}")

    # Build JSON template
    json_fields = "\n".join(
        f'  "{f}": "extracted value or null if not found"'
        for f in sorted_fields
    )

    return (
        f"You are extracting personal information from a {document_type}.\n"
        f"This is individual {instance_index + 1}. "
        f"Read the following pages and extract the requested fields.\n\n"
        + "\n\n".join(page_sections)
        + "\n\n"
        f"Extract these fields and return ONLY a JSON object:\n"
        f"{{\n{json_fields}\n}}\n\n"
        f"Field extraction guide:\n"
        + "\n".join(field_instructions)
        + "\n\n"
        "RULES:\n"
        "- Extract the EXACT value as it appears in the document\n"
        "- If a field is not present on any page, set it to null\n"
        "- For addresses, include the COMPLETE address "
        "(street, area, city, postcode, country)\n"
        "- For dates, preserve the original format (10-Aug-1959, not 1959-08-10)\n"
        "- For names, include title if present (Mr, Mrs, Dr)\n"
        "- Do NOT guess or infer values that are not explicitly stated\n"
        "- IMPORTANT: Names must contain ONLY the person's name, "
        "NOT reference numbers or IDs appended to it\n"
    )


def build_batch_extraction_prompt(
    batch_page_texts: list[list[str]],
    page_roles: list[PageRole],
    start_index: int,
    document_type: str,
) -> str:
    """Build a batched extraction prompt for multiple template instances.

    Sends multiple individuals' pages in one LLM call, expecting a JSON array
    response.  Reduces LLM calls from N to N/batch_size.

    Parameters
    ----------
    batch_page_texts:
        List of page text lists, one per template instance in the batch.
    page_roles:
        Page roles from the DocumentTemplate.
    start_index:
        0-based index of the first instance in this batch.
    document_type:
        Document type from the DocumentSchema.
    """
    all_fields: set[str] = set()
    for role in page_roles:
        all_fields.update(role.pii_fields_expected)
    all_fields.update(ALWAYS_EXTRACT_IF_PRESENT)

    sorted_fields = sorted(all_fields)

    field_instructions: list[str] = []
    for f in sorted_fields:
        instruction = ENTITY_EXTRACTION_GUIDE.get(f, f"Extract any {f} values")
        field_instructions.append(f"- {f}: {instruction}")

    # Build individual sections
    individual_sections: list[str] = []
    for batch_idx, pages in enumerate(batch_page_texts):
        instance_num = start_index + batch_idx + 1
        section = f"--- INDIVIDUAL {instance_num} ---"
        for page_idx, text in enumerate(pages):
            if text.strip():
                section += f"\n-- Page {page_idx + 1} --\n{text}"
        individual_sections.append(section)

    # JSON template for one instance
    json_obj = ", ".join(f'"{f}": "..."' for f in sorted_fields)

    return (
        f"You are extracting personal information from a {document_type}.\n"
        f"Extract information for {len(batch_page_texts)} individuals "
        f"from the following pages.\n\n"
        + "\n\n".join(individual_sections)
        + "\n\n"
        f"Return a JSON ARRAY with one object per individual:\n"
        f"[\n"
        f"  {{{json_obj}}},\n"
        f"  ...\n"
        f"]\n\n"
        f"Field extraction guide:\n"
        + "\n".join(field_instructions)
        + "\n\n"
        "RULES:\n"
        "- Extract the EXACT value as it appears in the document\n"
        "- If a field is not present, set it to null\n"
        "- For addresses, include the COMPLETE address\n"
        "- For dates, preserve the original format\n"
        "- For names, include title if present (Mr, Mrs, Dr)\n"
        "- Do NOT guess or infer values not explicitly stated\n"
        "- IMPORTANT: Names must contain ONLY the person's name, "
        "NOT reference numbers or IDs appended to it\n"
        f"- Return EXACTLY {len(batch_page_texts)} objects in the array\n"
    )
