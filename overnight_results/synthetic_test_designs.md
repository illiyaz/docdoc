# Synthetic Test File Designs — Overnight Pipeline Phase 4

**Date:** 2026-04-06
**Purpose:** Target specific weaknesses found during real-file extraction.
**Status:** DESIGN ONLY — not generated. Awaiting human review of real results.

---

## Design 1: Letterhead + Footer Repetition Test

**Format:** PDF (text, 20 pages)
**Content:** Each page has identical letterhead (company name, address, phone, fax) and footer (email, website). Body contains 1-2 unique subject names per page with SSN and DOB.
**Expected extraction:** 20-40 subject records, 0 organizational metadata records.
**Edge case tested:** ValueFrequencyFilter should suppress the letterhead/footer values while keeping unique subject PII.
**Weakness targeted:** The 52x `____` placeholder and repeated org phone/email false positives.

## Design 2: Email Thread with Multiple Senders

**Format:** MSG
**Content:** A forwarded email chain with 4 different senders in "From:" headers and signatures. Body mentions 6 breach subjects with mixed PII (name + SSN for some, name + DOB for others).
**Expected extraction:** 6 subject records only — all 4 sender names/emails should be suppressed.
**Edge case tested:** Sender-role filtering across multiple forwarding levels.
**Weakness targeted:** Ann Maynard appearing 5x as false positive in batch 5.

## Design 3: Mixed Table + Narrative PDF

**Format:** PDF (text, 10 pages)
**Content:** Pages 1-3 are narrative (legal notice text with labeled PII like "Patient: John Smith, DOB: 01/15/1980"). Pages 4-10 are a tabular list (Name | SSN | DOB | Address columns).
**Expected extraction:** Narrative PII from pages 1-3 + tabular PII from pages 4-10, with consistent field types.
**Edge case tested:** Transition from narrative to tabular extraction within same document.
**Weakness targeted:** Some PDFs yielded very low records despite having PII in narrative sections.

## Design 4: Scanned PDF with OCR Artifacts

**Format:** PDF (image-only, 5 pages)
**Content:** Simulated OCR output with: intentional character substitutions (0→O, 1→l), skewed alignment, partially redacted SSNs (###-##-1234), and a consistent organization watermark on every page.
**Expected extraction:** 10-15 records with partial SSNs preserved, watermark text suppressed.
**Edge case tested:** OCR quality handling, partial/masked value extraction, watermark suppression.
**Weakness targeted:** Batch 7 OCR results had artifacts like "Albany New" and very low yields.

## Design 5: Multi-Sheet XLSX with PII and Non-PII Sheets

**Format:** XLSX (4 sheets)
**Content:** Sheet 1 "Summary" has aggregate stats (no PII). Sheet 2 "Employees" has Name, SSN, DOB, Address columns (500 rows). Sheet 3 "Departments" has department names and headcounts (no PII). Sheet 4 "Contacts" has Name, Phone, Email columns (200 rows).
**Expected extraction:** 500 records from Sheet 2 + 200 from Sheet 4, nothing from Sheets 1 and 3.
**Edge case tested:** Selective sheet extraction, header mapping across varied column names.
**Weakness targeted:** XLS batch 6 yielded only 4 records — header mapping may be too narrow.

## Design 6: XLS with Merged Cells and Irregular Headers

**Format:** XLS (legacy)
**Content:** Header row uses merged cells spanning 2-3 columns. Some columns have two-line headers ("Social Security\nNumber"). Data rows have inconsistent formatting (some cells have leading spaces, some have trailing "N/A").
**Expected extraction:** 50 records with properly mapped fields despite irregular headers.
**Edge case tested:** Merged cell handling, multi-line header parsing, "N/A" filtering.
**Weakness targeted:** Batch 6 XLS yielded only 4 records.

## Design 7: Dense Multi-Format PDF (Forms + Tables + Narrative)

**Format:** PDF (text, 50 pages)
**Content:** Pages 1-5 are a cover letter (narrative). Pages 6-10 are a form (label: value pairs). Pages 11-50 are a data table with 800 records.
**Expected extraction:** Cover letter PII (2-3 records) + form PII (1 record) + table PII (800 records).
**Edge case tested:** Three extraction modes within one document, onset detection accuracy.
**Weakness targeted:** Complex1.pdf (4,200 pages, 318 records) may have missed many records.

## Design 8: HEIC Image of ID Card

**Format:** HEIC
**Content:** Simulated driver's license photo with: full name, DOB, address, license number, photo. Slight rotation and glare artifacts.
**Expected extraction:** 1 record with PERSON, DATE_OF_BIRTH, LOCATION, GOVERNMENT_ID.
**Edge case tested:** HEIC format handling (requires pillow-heif), ID card layout recognition.
**Weakness targeted:** Batch 6 HEIC file failed due to missing pillow-heif dependency.

## Design 9: PDF with Repeated Names in Different Roles

**Format:** PDF (text, 15 pages)
**Content:** "Dr. Sarah Johnson" appears on every page as the treating physician. "Sarah Johnson" also appears once as a patient on page 7 (different person, same name). 20 other patients appear once each.
**Expected extraction:** 20 unique patient records + 1 "Sarah Johnson" patient record. The physician "Dr. Sarah Johnson" should ideally be suppressed or flagged.
**Edge case tested:** Same name in different roles (provider vs. subject), title-based disambiguation.
**Weakness targeted:** Need for role-aware extraction to distinguish organizational contacts from subjects.

## Design 10: MSG with PDF Attachment Containing PII

**Format:** MSG with embedded PDF attachment
**Content:** Email body mentions 2 people by name. Attached PDF contains a 100-row table of breach subjects.
**Expected extraction:** 2 body records + 100 attachment records.
**Edge case tested:** Attachment extraction pipeline, email-to-PDF handoff.
**Weakness targeted:** Batch 5 MSG files only extracted body text — attachment PII was not processed.

---

## Priority Order for Implementation

1. **Design 5** (XLSX multi-sheet) — validates the most common input format after PDF
2. **Design 3** (mixed table + narrative) — addresses the narrative-to-tabular transition gap
3. **Design 1** (letterhead repetition) — validates the new ValueFrequencyFilter
4. **Design 2** (email thread) — validates sender-role filtering
5. **Design 7** (dense multi-format) — stress test for large documents
6. Remaining designs as time permits
