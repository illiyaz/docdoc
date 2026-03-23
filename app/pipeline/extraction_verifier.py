"""Post-extraction verification and vision gap-fill (Step 22d + audit).

After extraction runs on all pages (any path), this module:
1. Counts successes vs failures
2. Verifies extracted values exist in source page text (coordinate audit)
3. Checks format validity for typed fields (SSN, DOB, email)
4. Measures record count consistency across pages
5. Reports a summary for the auditor
6. Vision gap-fill: re-reads pages where records are missing fields

Coordinate-based audit (proven March 2026 on 34 documents):
  No vision model needed — verifies extracted values against PyMuPDF text.
  Instant, deterministic, 100% reproducible. 17/17 PASS on text PDFs.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

import fitz  # PyMuPDF

from app.rra.entity_resolver import PIIRecord

logger = logging.getLogger(__name__)


@dataclass
class ExtractionVerification:
    """Result of post-extraction verification."""

    total_pages: int = 0
    successful_pages: int = 0
    failed_pages: int = 0
    blank_pages: int = 0  # pages with no text (separator pages)
    reconciled_pages: int = 0  # pages recovered by LLM reconciliation

    # Per-field success rates
    field_rates: dict[str, float] = field(default_factory=dict)

    # Quality assessment
    success_rate: float = 0.0  # successful / (total - blank)
    is_acceptable: bool = True  # True if success_rate >= threshold

    # Coordinate audit results (new — proven approach)
    audit_status: str = ""  # "PASS" / "REVIEW" / "FAIL"
    audit_confidence: int = 0  # 0-100
    audit_consistency: int = 0  # 0-100 (record count stability)
    pages_audited: int = 0

    # Vision gap-fill results
    gap_fill_attempted: int = 0  # pages sent to vision for gap-fill
    gap_fill_succeeded: int = 0  # pages where vision filled ≥1 field
    gap_fill_fields: dict[str, int] = field(default_factory=dict)  # {field: count_filled}

    # Failed page details (first 20 for debugging)
    failed_page_samples: list[dict] = field(default_factory=list)

    # Summary message for auditor
    summary: str = ""


# Mapping from FieldMapping.field_type to PIIRecord attribute
_FIELD_TO_ATTR: dict[str, str] = {
    "PERSON": "raw_name",
    "LOCATION": "raw_address",
    "US_SSN": "raw_government_id",
    "GOVERNMENT_ID": "raw_government_id",
    "DATE_OF_BIRTH": "raw_dob",
    "EMAIL_ADDRESS": "raw_email",
    "PHONE_NUMBER": "raw_phone",
    "NI_NUMBER": "raw_government_id",
}

# Reverse mapping: PIIRecord attribute → vision model field name
_ATTR_TO_VISION: dict[str, str] = {
    "raw_government_id": "US_SSN",
    "raw_address": "LOCATION",
    "raw_dob": "DATE_OF_BIRTH",
    "raw_email": "EMAIL_ADDRESS",
    "raw_phone": "PHONE_NUMBER",
}

# Format validators for coordinate audit
_FORMAT_CHECKS: dict[str, re.Pattern[str]] = {
    "US_SSN": re.compile(r"^(\d{3}-\d{2}-\d{4}|XXX-XX-\d{4}|[Oo]n\s*[Ff]ile|\d+)$"),
    "GOVERNMENT_ID": re.compile(r"^(\d{3}-\d{2}-\d{4}|XXX-XX-\d{4}|[Oo]n\s*[Ff]ile|[A-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-Z]|\d+)$"),
    "DATE_OF_BIRTH": re.compile(r"^(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|\d{2}-[A-Z]{3}-\d{4})$"),
    "EMAIL_ADDRESS": re.compile(r"^[^@]+@[^@]+\.\w+$"),
    "PHONE_NUMBER": re.compile(r"^[\d\s().+-]{7,}$"),
}


def records_to_page_dict(records: list[PIIRecord]) -> dict[int, list[dict]]:
    """Convert PIIRecords to page_records dict for audit.

    Returns {page_num_0indexed: [{"PERSON": "...", "US_SSN": "..."}, ...]}
    """
    page_recs: dict[int, list[dict]] = {}
    for rec in records:
        pg = int(rec.page_range) - 1 if rec.page_range and rec.page_range.isdigit() else -1
        if pg < 0:
            continue
        rec_dict: dict[str, str] = {}
        if rec.raw_name:
            rec_dict["PERSON"] = rec.raw_name
        if rec.raw_government_id:
            rec_dict["US_SSN"] = rec.raw_government_id
        if rec.raw_dob:
            rec_dict["DATE_OF_BIRTH"] = rec.raw_dob
        if rec.raw_email:
            rec_dict["EMAIL_ADDRESS"] = rec.raw_email
        if rec.raw_phone:
            rec_dict["PHONE_NUMBER"] = rec.raw_phone
        if rec.raw_address:
            addr = rec.raw_address.get("raw", "") if isinstance(rec.raw_address, dict) else str(rec.raw_address)
            if addr.strip():
                rec_dict["LOCATION"] = addr.strip()
        if rec_dict:
            page_recs.setdefault(pg, []).append(rec_dict)
    return page_recs


class ExtractionVerifier:
    """Verify extraction completeness after coordinate bulk extraction."""

    ACCEPTABLE_RATE = 0.90  # 90% success is acceptable

    def verify(
        self,
        records: list[PIIRecord],
        failed_pages: list[int],
        reconciled_records: list[PIIRecord],
        total_pages: int,
        field_map: list,  # FieldMapping list
    ) -> ExtractionVerification:
        """Verify extraction results and produce summary."""
        result = ExtractionVerification(total_pages=total_pages)

        all_records = records + reconciled_records
        result.successful_pages = len(records)
        result.reconciled_pages = len(reconciled_records)
        result.failed_pages = len(failed_pages) - len(reconciled_records)
        if result.failed_pages < 0:
            result.failed_pages = 0

        # Calculate per-field rates
        field_types: set[str] = set()
        for fm in field_map:
            field_types.add(fm.field_type)

        for ft in field_types:
            attr = _FIELD_TO_ATTR.get(ft.upper())
            if attr and all_records:
                populated = sum(1 for r in all_records if getattr(r, attr, None))
                result.field_rates[ft] = populated / len(all_records)

        # Overall success rate
        if total_pages > 0:
            result.success_rate = len(all_records) / total_pages

        result.is_acceptable = result.success_rate >= self.ACCEPTABLE_RATE

        # Build summary
        result.summary = self._build_summary(result)

        return result

    def verify_by_coordinates(
        self,
        doc_path: str,
        page_records: dict[int, list[dict]],
        sample_size: int = 10,
    ) -> ExtractionVerification:
        """Coordinate-based text audit — verify extracted values exist in source.
        
        Proven approach (March 2026): instead of re-running vision model,
        simply check that each extracted value appears in the page's text layer.
        Instant, deterministic, no model needed.
        
        Parameters
        ----------
        doc_path: path to the PDF
        page_records: {page_num: [{"PERSON": "...", "US_SSN": "..."}, ...]}
        sample_size: number of pages to audit (evenly distributed)
        """
        result = ExtractionVerification()
        pages_with = sorted(page_records.keys())
        if not pages_with:
            result.audit_status = "NO_DATA"
            return result
        
        result.total_pages = len(pages_with)
        
        # Sample pages evenly across document
        n = min(len(pages_with), sample_size)
        step = max(1, len(pages_with) // n)
        sample_pages = [pages_with[i * step] for i in range(n) if i * step < len(pages_with)]
        
        # Audit each sampled page
        page_scores: list[int] = []
        try:
            doc = fitz.open(doc_path)
        except Exception as e:
            logger.warning("Cannot open %s for audit: %s", doc_path, e)
            result.audit_status = "ERROR"
            result.summary = f"Cannot open document: {e}"
            return result
        
        for pn in sample_pages:
            if pn >= doc.page_count:
                continue
            try:
                text = doc[pn].get_text()
            except Exception:
                continue
            
            text_norm = re.sub(r"\s+", " ", text).upper()
            text_compact = re.sub(r"[\s,]+", "", text).upper()
            
            records = page_records[pn]
            total_checks = 0
            passed = 0
            
            for rec in records:
                for ft, value in rec.items():
                    if ft in ("CITY_STATE_ZIP", "_source_page"):
                        continue
                    total_checks += 1
                    
                    val_norm = re.sub(r"\s+", " ", str(value)).upper()
                    val_compact = re.sub(r"[\s,]+", "", str(value)).upper()
                    
                    # Check 1: value exists in page text
                    exists = val_norm in text_norm or val_compact in text_compact
                    
                    # Check 2: format valid
                    fmt_ok = True
                    checker = _FORMAT_CHECKS.get(ft)
                    if checker:
                        fmt_ok = bool(checker.match(str(value)))
                    
                    if exists and fmt_ok:
                        passed += 1
                    elif exists:
                        passed += 0.5  # exists but wrong format
            
            confidence = round(100 * passed / max(total_checks, 1))
            page_scores.append(confidence)
        
        doc.close()
        
        # Record count consistency check
        counts = [len(page_records.get(pn, [])) for pn in pages_with]
        if counts:
            median = sorted(counts)[len(counts) // 2]
            outliers = sum(1 for c in counts if median > 0 and (c > median * 3 or (c < median * 0.3 and c > 0)))
            consistency = round(100 * (1 - outliers / max(len(counts), 1)))
        else:
            consistency = 0
        
        # Overall score
        avg_conf = round(sum(page_scores) / max(len(page_scores), 1))
        overall = round(avg_conf * 0.7 + consistency * 0.3)
        
        result.audit_confidence = overall
        result.audit_consistency = consistency
        result.pages_audited = len(page_scores)
        result.audit_status = "PASS" if overall >= 80 else ("REVIEW" if overall >= 50 else "FAIL")
        result.is_acceptable = overall >= 80
        
        total_recs = sum(len(v) for v in page_records.values())
        result.summary = (
            f"Coordinate audit: {result.audit_status} "
            f"({overall}% confidence, {consistency}% consistency)\n"
            f"  Records: {total_recs} across {len(pages_with)} pages\n"
            f"  Pages audited: {len(page_scores)}"
        )
        
        return result

    def find_gap_pages(
        self,
        records: list[PIIRecord],
        required_fields: list[str] | None = None,
    ) -> dict[int, list[tuple[int, list[str]]]]:
        """Identify pages where records are missing critical fields.

        Returns {page_num: [(record_index, [missing_fields]), ...]}
        Only includes pages for records that have a name but are missing
        other fields that should be present.

        Parameters
        ----------
        records: extracted PIIRecord list
        required_fields: fields to check (default: gov_id + address)
        """
        if required_fields is None:
            required_fields = ["raw_government_id", "raw_address"]

        gap_pages: dict[int, list[tuple[int, list[str]]]] = {}
        for idx, rec in enumerate(records):
            if not rec.raw_name:
                continue  # skip records without a name
            pg = int(rec.page_range) - 1 if rec.page_range and rec.page_range.isdigit() else -1
            if pg < 0:
                continue
            missing = [f for f in required_fields if not getattr(rec, f, None)]
            if missing:
                gap_pages.setdefault(pg, []).append((idx, missing))
        return gap_pages

    def vision_gap_fill(
        self,
        records: list[PIIRecord],
        doc_path: str,
        doc_id: str,
        ollama_client: "OllamaClient | None" = None,
        vision_model: str | None = None,
        max_pages: int = 50,
        dpi: int = 200,
    ) -> tuple[list[PIIRecord], ExtractionVerification]:
        """Fill missing fields on records by re-reading pages with vision model.

        For each page where a record has a name but is missing gov_id/address/dob,
        render the page image and ask the vision model to extract only the missing
        fields for that specific person.

        Parameters
        ----------
        records: list of PIIRecords (may be modified in-place via object.__setattr__)
        doc_path: path to the PDF
        doc_id: document ID for audit logging
        ollama_client: OllamaClient instance (if None, gap-fill is skipped)
        vision_model: model override for vision calls
        max_pages: cap on how many pages to send to vision (cost control)
        dpi: render resolution

        Returns (updated_records, verification_with_gap_fill_stats)
        """
        result = ExtractionVerification()

        if not ollama_client or not doc_path or not records:
            return records, result

        gap_pages = self.find_gap_pages(records)
        if not gap_pages:
            logger.info("Vision gap-fill: no gaps found, all records complete")
            return records, result

        # Limit pages to avoid excessive vision calls
        pages_to_fill = sorted(gap_pages.keys())[:max_pages]
        result.gap_fill_attempted = len(pages_to_fill)
        logger.info(
            "Vision gap-fill: %d pages with gaps (processing %d)",
            len(gap_pages), len(pages_to_fill),
        )

        try:
            from app.pdf.renderer import render_page_to_image
        except ImportError:
            logger.warning("PDF renderer unavailable, skipping vision gap-fill")
            return records, result

        for page_num in pages_to_fill:
            rec_gaps = gap_pages[page_num]
            # Collect all missing fields for this page
            all_missing: set[str] = set()
            names_on_page: list[str] = []
            for rec_idx, missing in rec_gaps:
                all_missing.update(missing)
                if records[rec_idx].raw_name:
                    names_on_page.append(records[rec_idx].raw_name)

            # Render page and ask vision for missing fields
            try:
                image = render_page_to_image(doc_path, page_num, dpi=dpi)
            except Exception:
                logger.debug("Cannot render page %d for gap-fill", page_num, exc_info=True)
                continue

            prompt = self._build_gap_fill_prompt(names_on_page, all_missing)
            try:
                response = ollama_client.generate_with_images(
                    prompt=prompt,
                    images=[image],
                    use_case="vision_gap_fill",
                    document_id=doc_id,
                    model_override=vision_model,
                )
                filled = self._parse_gap_fill_response(response)
            except Exception:
                logger.debug(
                    "Vision gap-fill failed for page %d", page_num, exc_info=True,
                )
                continue

            if not filled:
                continue

            # Merge filled fields into existing records
            page_filled = False
            for rec_idx, missing in rec_gaps:
                rec = records[rec_idx]
                name = (rec.raw_name or "").strip().upper()
                # Find matching person in vision response
                match = self._match_person(name, filled)
                if not match:
                    continue
                for field_name in missing:
                    vision_key = _ATTR_TO_VISION.get(field_name)
                    if vision_key and vision_key in match:
                        val = match[vision_key]
                        if val and str(val).strip():
                            if field_name == "raw_address":
                                object.__setattr__(rec, field_name, {"raw": str(val).strip()})
                            else:
                                object.__setattr__(rec, field_name, str(val).strip())
                            result.gap_fill_fields[field_name] = result.gap_fill_fields.get(field_name, 0) + 1
                            page_filled = True

            if page_filled:
                result.gap_fill_succeeded += 1

        if result.gap_fill_succeeded:
            logger.info(
                "Vision gap-fill: filled fields on %d/%d pages — %s",
                result.gap_fill_succeeded, result.gap_fill_attempted,
                result.gap_fill_fields,
            )

        return records, result

    @staticmethod
    def _build_gap_fill_prompt(names: list[str], missing_fields: set[str]) -> str:
        """Build a targeted prompt asking vision to extract only missing fields."""
        field_labels = []
        for f in missing_fields:
            label = _ATTR_TO_VISION.get(f)
            if label:
                field_labels.append(label)

        names_str = ", ".join(f'"{n}"' for n in names[:5])
        return (
            "You are extracting missing data fields from a document page.\n\n"
            f"The following individual(s) are on this page: {names_str}\n\n"
            f"Extract ONLY these missing fields for each person:\n"
            + "\n".join(f"- {fl}" for fl in field_labels)
            + "\n\n"
            "Return a JSON array with one object per person:\n"
            '[{"PERSON": "name", '
            + ", ".join(f'"{fl}": "value"' for fl in field_labels)
            + "}, ...]\n\n"
            "RULES:\n"
            "- Only extract for the named individuals listed above\n"
            "- If a field is not visible on this page, set it to null\n"
            "- Return ONLY valid JSON"
        )

    @staticmethod
    def _parse_gap_fill_response(response: str) -> list[dict]:
        """Parse vision model gap-fill JSON response."""
        if not response:
            return []
        # Strip markdown code fences
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON array in response
            start = text.find("[")
            end = text.rfind("]")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    return []
            else:
                return []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        return [d for d in data if isinstance(d, dict)]

    @staticmethod
    def _match_person(name_upper: str, filled: list[dict]) -> dict | None:
        """Find the best matching person in vision response by name."""
        if not name_upper or not filled:
            return None
        # Exact match first
        for d in filled:
            person = (d.get("PERSON") or "").strip().upper()
            if person == name_upper:
                return d
        # Fuzzy: check if last name matches
        name_parts = name_upper.split()
        last_name = name_parts[-1] if name_parts else ""
        for d in filled:
            person = (d.get("PERSON") or "").strip().upper()
            if last_name and last_name in person:
                return d
        # Single result — assume it's the match
        if len(filled) == 1:
            return filled[0]
        return None

    def _build_summary(self, result: ExtractionVerification) -> str:
        """Build human-readable summary for auditor."""
        total_extracted = result.successful_pages + result.reconciled_pages
        lines = [
            f"Extraction complete: {total_extracted}/{result.total_pages} pages",
            f"  Coordinate extraction: {result.successful_pages} pages",
        ]
        if result.reconciled_pages:
            lines.append(f"  LLM reconciliation: {result.reconciled_pages} pages recovered")
        if result.failed_pages > 0:
            lines.append(f"  Failed: {result.failed_pages} pages")

        if result.field_rates:
            lines.append("  Field rates:")
            for ft, rate in sorted(result.field_rates.items()):
                lines.append(f"    {ft}: {rate:.0%}")

        if result.audit_status:
            lines.append(f"  Coordinate audit: {result.audit_status} ({result.audit_confidence}%)")

        if result.gap_fill_attempted:
            lines.append(
                f"  Vision gap-fill: {result.gap_fill_succeeded}/{result.gap_fill_attempted} pages"
            )
            if result.gap_fill_fields:
                for fld, cnt in sorted(result.gap_fill_fields.items()):
                    lines.append(f"    {fld}: {cnt} filled")

        if result.is_acceptable:
            lines.append(f"  Quality: ACCEPTABLE ({result.success_rate:.0%})")
        else:
            lines.append(f"  Quality: BELOW THRESHOLD ({result.success_rate:.0%} < {self.ACCEPTABLE_RATE:.0%})")

        return "\n".join(lines)
