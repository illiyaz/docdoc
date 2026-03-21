#!/usr/bin/env python3
"""
Forentis AI — Unified PII Extraction Orchestrator
═══════════════════════════════════════════════════
Scans a folder, classifies every file, routes to the right extractor,
and produces a unified extraction report.

Supported: PDF, XLSX, XLS, CSV, TSV, DOCX, DOC, RTF, MSG, EML,
           TIF/PNG/JPG (OCR), MDB/ACCDB, DBF, PST, ZIP, XML, JSON,
           HTML, TXT/LOG

Usage:
    python3 forentis_extract.py /path/to/folder
    python3 forentis_extract.py /path/to/folder --output results.json
    python3 forentis_extract.py /path/to/single_file.xlsx
"""

import argparse, csv, io, json, os, re, shutil, subprocess, sys, tempfile, time, zipfile
from collections import Counter, defaultdict
from pathlib import Path

# ─── OPTIONAL IMPORTS (graceful degradation) ──────────────────
def _try_import(name):
    try: return __import__(name)
    except ImportError: return None

openpyxl = _try_import("openpyxl")
xlrd = _try_import("xlrd")
docx_mod = _try_import("docx")
extract_msg = _try_import("extract_msg")
pytesseract = _try_import("pytesseract")
PIL_Image = None
try:
    from PIL import Image as PIL_Image
except ImportError:
    pass
dbfread_mod = _try_import("dbfread")
olefile = _try_import("olefile")
fitz = _try_import("fitz")  # PyMuPDF

# ─── PII HEADER MAPPING (shared across all extractors) ────────
HEADER_MAP = [
    (re.compile(r"(?:full\s*)?name|employee|member|client|customer|student|patient|shareholder|owner|applicant|beneficiary|insured|claimant", re.I), "PERSON"),
    (re.compile(r"first\s*name|f\.?\s*name|given\s*name", re.I), "FIRST_NAME"),
    (re.compile(r"last\s*name|l\.?\s*name|surname|family\s*name", re.I), "LAST_NAME"),
    (re.compile(r"middle", re.I), "MIDDLE_NAME"),
    (re.compile(r"ss\s*n|soc\s*sec|social\s*security|tax\s*id|tin(?!\w)|ein(?!\w)", re.I), "US_SSN"),
    (re.compile(r"national\s*(?:ins|id)|ni\s*(?:number|no|#)", re.I), "GOVERNMENT_ID"),
    (re.compile(r"d\.?o\.?b\.?|date\s*of\s*birth|birth\s*date|born", re.I), "DATE_OF_BIRTH"),
    (re.compile(r"address|street|addr|mailing|residence", re.I), "LOCATION"),
    (re.compile(r"^city$", re.I), "CITY"),
    (re.compile(r"^state$|province", re.I), "STATE"),
    (re.compile(r"zip\s*(?:code)?|postal", re.I), "ZIP"),
    (re.compile(r"phone|tel(?:ephone)?|mobile|cell", re.I), "PHONE_NUMBER"),
    (re.compile(r"e[-\s]?mail", re.I), "EMAIL_ADDRESS"),
    (re.compile(r"account|acct|policy|member\s*(?:id|#)", re.I), "ACCOUNT_NUMBER"),
]

# Inline PII regex patterns (for text/narrative extraction)
INLINE_PII = [
    ("US_SSN", re.compile(r"\b(\d{3}-\d{2}-\d{4})\b")),
    ("US_SSN", re.compile(r"\bSSN[:\s]*(\d{3}-?\d{2}-?\d{4})\b", re.I)),
    ("DATE_OF_BIRTH", re.compile(r"\b(?:DOB|Date of Birth|Born)[:\s]*(\d{1,2}/\d{1,2}/\d{2,4})\b", re.I)),
    ("EMAIL_ADDRESS", re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")),
    ("PHONE_NUMBER", re.compile(r"\b(?:Phone|Tel|Mobile)[:\s]*([\d\s().+-]{7,})\b", re.I)),
]
NAME_PATTERN = re.compile(r"([A-ZÀ-Þ][a-zà-ÿ'-]+(?:\s+[A-ZÀ-Þ]\.?)?\s+[A-ZÀ-Þ][a-zà-ÿ'-]+)")


# ═══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def _map_header(text):
    for pat, pii_type in HEADER_MAP:
        if pat.search(text): return pii_type
    return None

def _combine_fields(rec):
    """Merge split name and address columns."""
    parts = []
    for k in ("FIRST_NAME","MIDDLE_NAME","LAST_NAME"):
        v = rec.pop(k, "")
        if v: parts.append(v.strip())
    if parts: rec["PERSON"] = " ".join(parts)
    addr = []
    for k in ("CITY","STATE","ZIP"):
        v = rec.pop(k, "")
        if v: addr.append(v.strip())
    if addr: rec["CITY_STATE_ZIP"] = ", ".join(addr)
    return rec

def _detect_header_row(rows, max_scan=20):
    best, best_s = 0, 0
    for i, row in enumerate(rows[:max_scan]):
        cells = [str(c).strip() for c in row if c is not None]
        s = sum(1 for c in cells if _map_header(c))
        if s > best_s: best_s = s; best = i
    return best if best_s >= 2 else 0

def _extract_text_pii(text):
    """Extract PII from plain text using regex. Returns list of records.
    
    Handles:
    - Inline PII near names (SSN, DOB, email near name)
    - Labeled fields (Patient: Karen Craft, Name: John Smith)
    - ALL_CAPS names (SHIELDS, GEORGE)
    - Addresses, phones, account numbers
    """
    hits = []
    
    # 1. Standard inline patterns (SSN, DOB, email, phone, account)
    for pt, pat in INLINE_PII:
        for m in pat.finditer(text):
            hits.append((m.start(), pt, m.group(1).strip()))
    
    # 2. Labeled name fields: "Patient: Karen Craft", "Account Holder: JOHNSON, ROBERT T"
    LABEL_PATS = [
        # Mixed case after label
        re.compile(r"(?:Patient|Name|Client|Customer|Employee|Member|Insured|Claimant|"
                   r"Bill\s*to|Ship\s*to|Attention|Beneficiary|Account\s*Holder|Holder|"
                   r"Owner|Subscriber|Applicant|Tenant|Borrower|Policyholder|Guarantor|"
                   r"Responsible\s*Party|Primary|Cardmember|Cardholder)"
                   r"\s*:\s*\n?\s*([A-ZÀ-Þ][a-zà-ÿ'-]+(?:\s+[A-ZÀ-Þ]\.?)?\s+[A-ZÀ-Þ][a-zà-ÿ'-]+)", re.I),
        # ALL_CAPS after label
        re.compile(r"(?:Patient|Name|Client|Customer|Employee|Member|Account\s*Holder|"
                   r"Bill\s*to|Ship\s*to|Holder|Owner|Subscriber|Policyholder)"
                   r"\s*:\s*\n?\s*([A-Z][A-Z '-]+,\s*[A-Z][A-Z .'-]+)", re.I),
    ]
    for pat in LABEL_PATS:
        for m in pat.finditer(text):
            name = m.group(1).strip()
            if len(name) > 3 and len(name) < 50:
                hits.append((m.start(), "PERSON", name))
    
    # 2b. Line-by-line tabular extraction: "Adams, Patricia  234-56-7890  04/12/1978"
    SSN_RE = re.compile(r"\d{3}-\d{2}-\d{4}")
    DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
    LNAME_RE = re.compile(r"([A-ZÀ-Þ][a-zà-ÿ'-]+(?:,\s*| )[A-ZÀ-Þ][a-zà-ÿ'-]+(?:\s+[A-ZÀ-Þ]\.?)?)")
    for line in text.split("\n"):
        ssn_m = SSN_RE.search(line)
        if ssn_m:
            name_m = LNAME_RE.search(line[:ssn_m.start()])
            date_m = DATE_RE.search(line[ssn_m.end():])
            if name_m:
                line_pos = text.find(line)
                hits.append((line_pos, "PERSON", name_m.group(1).strip()))
                hits.append((line_pos + ssn_m.start(), "US_SSN", ssn_m.group()))
                if date_m:
                    hits.append((line_pos + ssn_m.end() + date_m.start(), "DATE_OF_BIRTH", date_m.group()))
    
    # 3. Mixed-case names near any other PII hit (existing logic)
    for m in NAME_PATTERN.finditer(text):
        pos = m.start()
        if any(abs(pos - h[0]) < 200 for h in hits):
            words = m.group(1).split()
            skip = {"THE","AND","FOR","WITH","FROM","THIS","THAT","WILL","HAVE","BEEN",
                    "YOUR","THEIR","PLEASE","THANK","VISIT","TOTAL","PAYMENT","BALANCE",
                    "PRIMARY","TRANSACTION","DESCRIPTION","SUBTOTAL","PRODUCT","SERVICES",
                    "DENTAL","STATEMENT","REFERENCE","LICENSE","ORDER","DURATION"}
            if len(words) >= 2 and not any(w.upper() in skip for w in words):
                hits.append((pos, "PERSON", m.group(1)))
    
    # 4. ALL_CAPS names like "SHIELDS, GEORGE" or "KAREN CRAFT" (standalone)
    ALLCAP_NAME = re.compile(r"\b([A-Z][A-Z'-]+,\s*[A-Z][A-Z .'+-]+)\b")
    for m in ALLCAP_NAME.finditer(text):
        name = m.group(1).strip()
        words = name.replace(",", " ").split()
        skip_corp = {"INC","LLC","LLP","CORP","LTD","COMPANY","BANK","TRUST","THE","AND","FOR"}
        if 2 <= len(words) <= 5 and not any(w in skip_corp for w in words):
            if any(len(w) >= 3 for w in words):
                hits.append((m.start(), "PERSON", name))
    
    # 5. Addresses: number + street name + optional city/state/zip
    ADDR_PAT = re.compile(r"\b(\d{1,5}\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*\s+(?:ST|STREET|AVE|AVENUE|RD|ROAD|DR|DRIVE|LN|LANE|TRL|TRAIL|BLVD|CT|PL|WAY|CIR|HWY))\b", re.I)
    for m in ADDR_PAT.finditer(text):
        hits.append((m.start(), "LOCATION", m.group(1).strip()))
    
    # 6. Phone: (xxx) xxx-xxxx or xxx-xxx-xxxx
    PHONE_PAT = re.compile(r"\((\d{3})\)\s*(\d{3})[-.]\d{4}|\b(\d{3})[-.]\d{3}[-.]\d{4}\b")
    for m in PHONE_PAT.finditer(text):
        hits.append((m.start(), "PHONE_NUMBER", m.group(0).strip()))
    
    if not hits:
        return []
    
    # Deduplicate
    seen = set()
    deduped = []
    for pos, pt, val in hits:
        key = (pt, val)
        if key not in seen:
            seen.add(key)
            deduped.append((pos, pt, val))
    deduped.sort(key=lambda x: x[0])
    
    # Validate PERSON hits — reject labels, addresses, common words
    PERSON_REJECT = {"PATIENT","NAME","ACCOUNT","HOLDER","CLIENT","CUSTOMER","EMPLOYEE",
                     "MEMBER","INSURED","PRIMARY","DOCTOR","STATEMENT","DESCRIPTION",
                     "SERVICES","DENTAL","MEDICAL","TRANSACTION","PAYMENT","SUBTOTAL",
                     "PRODUCT","BALANCE","REFERENCE","LICENSE","LICENSED","ORDER","DURATION",
                     "TOTAL","INSURANCE","BANK","NATIONAL","FEDERAL","FIRST","COMPANY",
                     "TRUST","SOCIAL","SECURITY","STATE","CARD","TYPE","REPORT","SUMMARY",
                     "DATE","NUMBER","AMOUNT","PHONE","EMAIL","ADDRESS","INFORMATION",
                     "TREATMENT","PROMISE","DISCOUNT","ADJUSTMENT","METHOD","POLICY"}
    ADDR_WORDS = {"STREET","AVENUE","ROAD","DRIVE","LANE","TRAIL","BLVD","COURT","PLACE",
                  "WAY","CIRCLE","HWY","HIGHWAY","PARKWAY","TERRACE"}
    validated = []
    for pos, pt, val in deduped:
        if pt == "PERSON":
            words = val.replace(",","").upper().split()
            # Reject if any word is a common label/address word
            if any(w in PERSON_REJECT for w in words): continue
            if any(w in ADDR_WORDS for w in words): continue
            # Reject if looks like city+state: "LIVERPOOL, NY"
            if len(words) == 2 and len(words[-1]) == 2 and words[-1].isalpha(): continue
            # Reject single-word "names" (OCR artifacts)
            if len(words) < 2: continue
        validated.append((pos, pt, val))
    
    # Group into records: second PERSON starts new record, OR gap > 300 chars
    records, cur, last = [], {}, -999
    for pos, pt, val in validated:
        if cur and (pos - last > 300 or (pt == "PERSON" and "PERSON" in cur)):
            records.append(cur)
            cur = {}
        if pt not in cur:
            cur[pt] = val
        last = pos
    if cur:
        records.append(cur)
    return records

def _tabular_extract(rows):
    """Generic tabular extraction from list of row-lists."""
    if len(rows) < 2: return [], {}
    hi = _detect_header_row(rows)
    header = [str(c).strip() if c else "" for c in rows[hi]]
    cmap = {i: _map_header(h) for i, h in enumerate(header) if _map_header(h)}
    if not cmap: return [], {}
    records = []
    for row in rows[hi+1:]:
        rec = {}
        for ci, pt in cmap.items():
            if ci < len(row) and row[ci] is not None:
                v = str(row[ci]).strip()
                if v and v.lower() not in ("none","null","n/a",""): rec[pt] = v
        if rec:
            rec = _combine_fields(rec)
            if rec: records.append(rec)
    fields = Counter()
    for r in records:
        for k in r: fields[k] += 1
    return records, {"columns_mapped": {v: header[k] for k,v in cmap.items()},
                     "fields": dict(fields), "total_records": len(records)}


# ═══════════════════════════════════════════════════════════════
# FORMAT-SPECIFIC EXTRACTORS
# ═══════════════════════════════════════════════════════════════

# ─── PDF ──────────────────────────────────────────────────────
def _is_scanned_pdf(filepath, sample_pages=3):
    """Check if PDF is scanned (image-only, no text layer)."""
    try:
        doc = fitz.open(filepath)
        total_text = 0
        for pn in range(min(doc.page_count, sample_pages)):
            total_text += len(doc[pn].get_text().strip())
        doc.close()
        return total_text < 50  # less than 50 chars across sample pages = scanned
    except:
        return False

def _ocr_pdf(filepath, max_pages=50):
    """OCR a scanned PDF: render pages as images, Tesseract each one."""
    if not pytesseract or not PIL_Image:
        return "", {"error": "pytesseract/Pillow not installed for OCR"}
    
    doc = fitz.open(filepath)
    all_text = ""
    pages_ocrd = 0
    
    for pn in range(min(doc.page_count, max_pages)):
        try:
            pix = doc[pn].get_pixmap(matrix=fitz.Matrix(2, 2))  # 144 DPI
            img = PIL_Image.open(io.BytesIO(pix.tobytes("png")))
            page_text = pytesseract.image_to_string(img)
            all_text += page_text + "\n"
            pages_ocrd += 1
        except Exception:
            continue
    
    doc.close()
    return all_text, {"pages_ocrd": pages_ocrd, "ocr_chars": len(all_text)}

def _vision_ocr_pdf(filepath, vision_model, ollama_url, fallback_model=None, max_pages=20):
    """Send scanned PDF pages to vision model for PII extraction.
    Vision understands layout, context, labels — catches what regex misses.
    
    Returns list of PII records across all pages.
    """
    import base64, requests
    
    doc = fitz.open(filepath)
    all_records = []
    
    prompt = """Analyze this scanned document image. Extract ALL personal information (PII) you can find.

Return a JSON object with a "pii_records" array. Each record should contain the fields you find:
- PERSON (full name)
- US_SSN (social security number, even if masked like ###-##-1234)
- DATE_OF_BIRTH
- LOCATION (street address)
- CITY_STATE_ZIP
- PHONE_NUMBER
- EMAIL_ADDRESS
- ACCOUNT_NUMBER (credit card numbers, account numbers, even partial/masked)
- GOVERNMENT_ID (NPI, license numbers, tax IDs, any government-issued ID)

Include masked/partial values (like card ending in 2464). Include ALL people on the page.
Return ONLY valid JSON, no explanation."""

    for pn in range(min(doc.page_count, max_pages)):
        try:
            pix = doc[pn].get_pixmap(matrix=fitz.Matrix(2, 2))
            img_bytes = pix.tobytes("png")
            b64 = base64.b64encode(img_bytes).decode()
            
            payload = {
                "model": vision_model,
                "messages": [{"role": "user", "content": prompt, "images": [b64]}],
                "stream": False,
                "options": {"temperature": 0.1}
            }
            
            resp = requests.post(f"{ollama_url}/api/chat", json=payload, timeout=120)
            if resp.status_code != 200:
                if fallback_model:
                    payload["model"] = fallback_model
                    resp = requests.post(f"{ollama_url}/api/chat", json=payload, timeout=120)
                if resp.status_code != 200:
                    continue
            
            text = resp.json().get("message", {}).get("content", "")
            
            # Parse JSON from response
            import json as json_mod
            # Strip markdown fences
            text = re.sub(r"```json\s*", "", text)
            text = re.sub(r"```\s*", "", text)
            # Find JSON object
            match = re.search(r"\{.*\}", text, re.S)
            if match:
                parsed = json_mod.loads(match.group())
                recs = parsed.get("pii_records", [parsed] if "PERSON" in parsed else [])
                for r in recs:
                    # Normalize keys
                    clean = {}
                    for k, v in r.items():
                        k_up = k.upper().replace(" ", "_")
                        if v and str(v).strip() and str(v).lower() not in ("null","none","n/a",""):
                            clean[k_up] = str(v).strip()
                    if clean:
                        clean["_source_page"] = pn + 1
                        all_records.append(clean)
        except Exception:
            continue
    
    doc.close()
    return all_records

def extract_pdf(filepath, vision_model=None, ollama_url=None, fallback_model=None):
    """PDF extraction — handles text PDFs and scanned PDFs.
    
    Text PDFs:
      1. Full hybrid pipeline (vision+template) if ollama available
      2. Quick regex on text layer
    
    Scanned PDFs (three-tier):
      1. Vision model analysis (best — understands layout, context, labels)
      2. OCR + regex (fallback — Tesseract text + pattern matching)
      3. OCR + enhanced regex with labeled field detection
    """
    if not fitz: return [], {"error": "PyMuPDF not installed"}
    
    try:
        doc = fitz.open(filepath)
        total = doc.page_count
        doc.close()
    except Exception as e:
        return [], {"error": str(e)}

    scanned = _is_scanned_pdf(filepath)

    # ── SCANNED PDF PATH ──────────────────────────────
    if scanned:
        # Tier 1: Vision model (best for scanned docs)
        if vision_model and ollama_url:
            print(f"    🔍 Scanned PDF — vision analysis...", end=" ", flush=True)
            try:
                records = _vision_ocr_pdf(filepath, vision_model, ollama_url, fallback_model)
                if records:
                    fields = Counter()
                    for r in records:
                        for k in r:
                            if not k.startswith("_"): fields[k] += 1
                    print(f"{len(records)} records via vision")
                    return records, {"total_pages": total, "extraction_mode": "vision_ocr",
                                     "total_records": len(records), "fields": dict(fields)}
                print("no results, falling back to OCR")
            except Exception as e:
                print(f"failed ({e}), falling back to OCR")
        
        # Tier 2: Tesseract OCR + enhanced regex
        print(f"    🔍 Scanned PDF — running OCR...", end=" ", flush=True)
        ocr_text, ocr_meta = _ocr_pdf(filepath)
        if not ocr_text.strip():
            return [], {"error": "Scanned PDF — OCR returned no text", "total_pages": total}
        print(f"{ocr_meta.get('pages_ocrd',0)} pages, {ocr_meta.get('ocr_chars',0)} chars")
        
        records = _extract_text_pii(ocr_text)
        fields = Counter()
        for r in records:
            for k in r: fields[k] += 1
        return records, {"total_pages": total, "extraction_mode": "ocr_regex",
                         "total_records": len(records), "fields": dict(fields), **ocr_meta}

    # ── TEXT PDF PATH ─────────────────────────────────
    if vision_model and ollama_url:
        try:
            return _extract_pdf_full_pipeline(filepath, total, vision_model, ollama_url, fallback_model)
        except Exception as e:
            print(f"    ⚠ Full pipeline failed ({e}), falling back to regex")

    # Fallback: Quick regex on text layer
    try:
        doc = fitz.open(filepath)
        all_text = ""
        for pn in range(min(total, 100)):
            all_text += doc[pn].get_text() + "\n"
        doc.close()
    except Exception as e:
        return [], {"error": str(e)}

    records = _extract_text_pii(all_text)
    return records, {"total_pages": total, "extraction_mode": "quick_regex",
                     "total_records": len(records)}


def _extract_pdf_full_pipeline(filepath, total_pages, vision_model, ollama_url, fallback_model):
    """Run the full hybrid pipeline: onset → vision → template → extract → audit."""
    import importlib.util
    
    # Import pipeline module
    pipeline_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_hybrid_pipeline.py")
    if not os.path.exists(pipeline_path):
        # Try same directory as this script
        pipeline_path = os.path.join(os.path.dirname(__file__) or ".", "test_hybrid_pipeline.py")
    if not os.path.exists(pipeline_path):
        raise FileNotFoundError("test_hybrid_pipeline.py not found alongside forentis_extract.py")
    
    spec = importlib.util.spec_from_file_location("pipeline", pipeline_path)
    pipeline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipeline)

    # 1. Onset detection
    onset = pipeline.find_onset_page(filepath)
    
    # 2. Vision analysis
    vis = pipeline.vision_analyze(filepath, onset, total_pages, vision_model, ollama_url, fallback_model)
    pii_fields = []
    if vis.get("parsed"):
        pii_fields = vis["parsed"].get("pii_fields", [])
    
    if not pii_fields:
        raise RuntimeError("Vision returned no PII fields")
    
    # 3. Text-based PERSON discovery if vision found no PERSON
    has_person = any(f.get("type") == "PERSON" for f in pii_fields)
    if not has_person:
        discovered, disc_page = pipeline.discover_person_from_text(filepath, onset)
        if discovered:
            pii_fields.extend(discovered)
            onset = disc_page
    
    # 4. Template building
    lines, pw, ph = pipeline.get_page_lines(filepath, onset)
    template = pipeline.build_template(pii_fields, lines)
    
    if template["total_located"] == 0:
        raise RuntimeError("Template empty — no values located in coordinates")
    
    # 5. Content page detection + extraction
    doc = fitz.open(filepath)
    page_records = {}
    total_recs = 0
    fields = Counter()
    
    for pn in range(doc.page_count):
        text = doc[pn].get_text()
        if len(text.strip()) < 100:
            continue
        recs = pipeline.extract_page_with_template(filepath, pn, template)
        if recs:
            page_records[pn] = recs
            total_recs += len(recs)
            for r in recs:
                for k in r:
                    fields[k] += 1
    doc.close()
    
    # 6. Audit
    audit = pipeline.audit_document(filepath, page_records, sample_size=10)
    
    # 7. Flatten to record list
    all_records = []
    for pn in sorted(page_records):
        for r in page_records[pn]:
            r["_source_page"] = pn + 1
            all_records.append(r)
    
    return all_records, {
        "total_pages": total_recs,
        "extraction_mode": "full_pipeline",
        "total_records": total_recs,
        "fields": dict(fields),
        "onset_page": onset,
        "template_name_fmt": template.get("name_fmt"),
        "template_located": template.get("total_located"),
        "audit_status": audit.get("status"),
        "audit_confidence": audit.get("confidence"),
        "vision_model": vis.get("model_used"),
    }

# ─── XLSX ─────────────────────────────────────────────────────
def extract_xlsx(filepath):
    if not openpyxl: return [], {"error": "openpyxl not installed"}
    try:
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    except Exception as e:
        return [], {"error": str(e)}
    all_recs, all_info = [], []
    for sheet in wb.sheetnames:
        rows = [list(r) for r in wb[sheet].iter_rows(values_only=True)]
        recs, info = _tabular_extract(rows)
        if recs:
            for r in recs: r["_source_sheet"] = sheet
            all_recs.extend(recs)
            all_info.append({"sheet": sheet, **info})
    wb.close()
    fields = Counter()
    for r in all_recs:
        for k in r:
            if not k.startswith("_"): fields[k] += 1
    return all_recs, {"total_records": len(all_recs), "fields": dict(fields), "sheets": all_info}

# ─── XLS (legacy) ─────────────────────────────────────────────
def extract_xls(filepath):
    if not xlrd: return [], {"error": "xlrd not installed — pip install xlrd"}
    try:
        wb = xlrd.open_workbook(filepath)
    except Exception as e:
        return [], {"error": str(e)}
    all_recs = []
    for sheet in wb.sheet_names():
        ws = wb.sheet_by_name(sheet)
        rows = [[ws.cell_value(r, c) for c in range(ws.ncols)] for r in range(ws.nrows)]
        recs, _ = _tabular_extract(rows)
        all_recs.extend(recs)
    fields = Counter()
    for r in all_recs:
        for k in r: fields[k] += 1
    return all_recs, {"total_records": len(all_recs), "fields": dict(fields)}

# ─── CSV / TSV ────────────────────────────────────────────────
def extract_csv(filepath, delimiter=None):
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            sample = f.read(4096); f.seek(0)
            if delimiter is None:
                delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
            rows = list(csv.reader(f, delimiter=delimiter))
    except Exception as e:
        return [], {"error": str(e)}
    recs, info = _tabular_extract(rows)
    return recs, info

# ─── DOCX ─────────────────────────────────────────────────────
def extract_docx(filepath):
    if not docx_mod: return [], {"error": "python-docx not installed"}
    try:
        doc = docx_mod.Document(filepath)
    except Exception as e:
        return [], {"error": str(e)}

    # Phase 1: tables
    all_recs = []
    for table in doc.tables:
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        recs, _ = _tabular_extract(rows)
        all_recs.extend(recs)

    # Phase 2: narrative fallback
    if not all_recs:
        text = "\n".join(p.text for p in doc.paragraphs)
        all_recs = _extract_text_pii(text)

    fields = Counter()
    for r in all_recs:
        for k in r: fields[k] += 1
    method = "tables" if any(doc.tables) and all_recs else "narrative"
    return all_recs, {"total_records": len(all_recs), "fields": dict(fields),
                      "method": method, "tables": len(doc.tables), "paragraphs": len(doc.paragraphs)}

# ─── DOC (legacy Word) ────────────────────────────────────────
def extract_doc(filepath):
    """Convert .doc to text via antiword, then extract PII."""
    try:
        result = subprocess.run(["antiword", filepath], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            # Fallback: libreoffice convert to docx
            tmp_dir = tempfile.mkdtemp()
            subprocess.run(["libreoffice", "--headless", "--convert-to", "docx",
                          "--outdir", tmp_dir, filepath], capture_output=True, timeout=60)
            docx_file = os.path.join(tmp_dir, Path(filepath).stem + ".docx")
            if os.path.exists(docx_file):
                recs, meta = extract_docx(docx_file)
                shutil.rmtree(tmp_dir, ignore_errors=True)
                meta["conversion"] = "libreoffice→docx"
                return recs, meta
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return [], {"error": "antiword and libreoffice both failed"}
        text = result.stdout
    except FileNotFoundError:
        return [], {"error": "antiword not installed — apt install antiword"}
    except Exception as e:
        return [], {"error": str(e)}

    records = _extract_text_pii(text)
    fields = Counter()
    for r in records:
        for k in r: fields[k] += 1
    return records, {"total_records": len(records), "fields": dict(fields), "conversion": "antiword→text"}

# ─── RTF ──────────────────────────────────────────────────────
def extract_rtf(filepath):
    """Convert RTF to text via libreoffice, then extract PII."""
    try:
        tmp_dir = tempfile.mkdtemp()
        subprocess.run(["libreoffice", "--headless", "--convert-to", "txt:Text",
                       "--outdir", tmp_dir, filepath], capture_output=True, timeout=60)
        txt_file = os.path.join(tmp_dir, Path(filepath).stem + ".txt")
        if os.path.exists(txt_file):
            with open(txt_file, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            shutil.rmtree(tmp_dir, ignore_errors=True)
            records = _extract_text_pii(text)
            fields = Counter()
            for r in records:
                for k in r: fields[k] += 1
            return records, {"total_records": len(records), "fields": dict(fields)}
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return [], {"error": "libreoffice conversion failed"}
    except Exception as e:
        return [], {"error": str(e)}

# ─── MSG / EML ────────────────────────────────────────────────
def extract_email(filepath):
    """Extract PII from email files — both attachments AND body text."""
    ext = Path(filepath).suffix.lower()
    tmp_dir = tempfile.mkdtemp(prefix="forentis_email_")
    attachments = []
    body_text = ""
    meta = {}

    try:
        if ext == ".msg" and extract_msg:
            msg = extract_msg.Message(filepath)
            meta = {"subject": msg.subject or "", "sender": msg.sender or "",
                    "date": str(msg.date or "")}
            
            # Get body text (try HTML first, then plain, then RTF)
            if msg.htmlBody:
                html = msg.htmlBody.decode("utf-8","replace") if isinstance(msg.htmlBody, bytes) else msg.htmlBody
                body_text = re.sub(r"<[^>]+>", " ", html)
                body_text = re.sub(r"&\w+;", " ", body_text)
                body_text = re.sub(r"\s+", " ", body_text).strip()
            elif msg.body:
                body_text = msg.body
            elif msg.rtfBody:
                rtf = msg.rtfBody.decode("utf-8","replace") if isinstance(msg.rtfBody, bytes) else str(msg.rtfBody)
                body_text = re.sub(r"\\[a-z]+\d*\s?|[{}]", "", rtf)
                body_text = re.sub(r"\s+", " ", body_text).strip()
            
            # Extract attachments (skip logos/tiny images)
            for att in msg.attachments:
                fn = att.longFilename or att.shortFilename or "unknown"
                fn = fn.replace("\x00", "")  # strip null bytes from filenames
                att_ext = Path(fn).suffix.lower()
                data = att.data if att.data else b""
                # Skip tiny files (<10KB) and pure image logos
                if len(data) < 10000 and att_ext in (".jpg",".jpeg",".png",".gif"):
                    continue
                if data:
                    att_path = os.path.join(tmp_dir, fn)
                    try:
                        with open(att_path, "wb") as f: f.write(data)
                        attachments.append(att_path)
                    except: pass
            msg.close()
            
        elif ext == ".eml":
            import email as email_mod
            with open(filepath, "rb") as f:
                msg = email_mod.message_from_bytes(f.read())
            meta = {"subject": msg.get("Subject",""), "sender": msg.get("From",""),
                    "date": msg.get("Date","")}
            
            # Get body text
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        html = payload.decode("utf-8","replace")
                        body_text = re.sub(r"<[^>]+>", " ", html)
                        body_text = re.sub(r"&\w+;", " ", body_text)
                        body_text = re.sub(r"\s+", " ", body_text).strip()
                        break
                elif ct == "text/plain" and not body_text:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body_text = payload.decode("utf-8","replace")
            
            # Extract attachments
            for part in msg.walk():
                if "attachment" in (part.get("Content-Disposition") or ""):
                    fn = part.get_filename() or "unknown"
                    data = part.get_payload(decode=True)
                    if data and len(data) > 10000:  # skip tiny logos
                        att_path = os.path.join(tmp_dir, fn)
                        with open(att_path, "wb") as f: f.write(data)
                        attachments.append(att_path)
        else:
            return [], {"error": f"Unsupported email format {ext}"}
    except Exception as e:
        return [], {"error": str(e)}

    # Extract PII from body text
    body_records = _extract_text_pii(body_text) if body_text else []
    
    # Also extract PII from subject line
    subject = meta.get("subject", "")
    if subject:
        subj_records = _extract_text_pii(subject)
        body_records.extend(subj_records)
    
    if body_records and not attachments:
        # No attachments worth processing — return body PII directly
        fields = Counter()
        for r in body_records:
            for k in r: fields[k] += 1
        return body_records, {"total_records": len(body_records), "fields": dict(fields),
                              "extraction_mode": "email_body", "metadata": meta,
                              "body_chars": len(body_text)}
    
    if attachments:
        return [], {"handler": "email", "metadata": meta, "attachments": attachments,
                    "attachment_count": len(attachments), "temp_dir": tmp_dir,
                    "body_records": body_records}
    
    # No attachments AND no body PII
    if body_text:
        return [], {"handler": "email", "metadata": meta, "attachments": [],
                    "body_chars": len(body_text), "total_records": 0,
                    "note": "Email body found but no PII patterns detected"}
    
    return [], {"handler": "email", "metadata": meta, "attachments": [],
                "total_records": 0}

# ─── IMAGE (OCR) ──────────────────────────────────────────────
def extract_image(filepath, vision_model=None, ollama_url=None, fallback_model=None):
    """Extract PII from image files. Vision-first for ID cards/complex docs,
    OCR fallback for simple scanned text."""
    if not pytesseract or not PIL_Image:
        return [], {"error": "pytesseract/Pillow not installed"}
    
    # Tier 1: Vision model (best for ID cards, receipts, complex layouts)
    if vision_model and ollama_url:
        try:
            import base64, requests, json as json_mod
            img = PIL_Image.open(filepath).convert("RGB")
            buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
            b64 = base64.b64encode(buf.read()).decode()
            
            prompt = """Analyze this image. Extract ALL personal information (PII) you can find.
Return a JSON object with a "pii_records" array. Each record should have fields like:
PERSON, US_SSN, DATE_OF_BIRTH, LOCATION, CITY_STATE_ZIP, PHONE_NUMBER,
EMAIL_ADDRESS, ACCOUNT_NUMBER, GOVERNMENT_ID (driver license #, NPI, passport #, any govt ID).
Include ALL values you can read. Return ONLY valid JSON."""
            
            payload = {"model": vision_model, "stream": False, "options": {"temperature": 0.1},
                       "messages": [{"role": "user", "content": prompt, "images": [b64]}]}
            resp = requests.post(f"{ollama_url}/api/chat", json=payload, timeout=120)
            if resp.status_code != 200 and fallback_model:
                payload["model"] = fallback_model
                resp = requests.post(f"{ollama_url}/api/chat", json=payload, timeout=120)
            if resp.status_code == 200:
                text = resp.json().get("message", {}).get("content", "")
                text = re.sub(r"```json\s*|```\s*", "", text)
                match = re.search(r"\{.*\}", text, re.S)
                if match:
                    parsed = json_mod.loads(match.group())
                    recs = parsed.get("pii_records", [parsed] if any(k in parsed for k in ("PERSON","US_SSN","GOVERNMENT_ID")) else [])
                    records = []
                    for r in recs:
                        clean = {k.upper().replace(" ","_"): str(v).strip() for k,v in r.items()
                                 if v and str(v).strip().lower() not in ("null","none","n/a","")}
                        if clean: records.append(clean)
                    if records:
                        fields = Counter()
                        for r in records:
                            for k in r: fields[k] += 1
                        return records, {"total_records": len(records), "fields": dict(fields),
                                         "method": "vision"}
        except Exception:
            pass  # Fall through to OCR
    
    # Tier 2: Tesseract OCR
    try:
        img = PIL_Image.open(filepath).convert("RGB")
        # Upscale for better OCR on phone photos
        if max(img.size) > 1000:  # likely a photo, not a thumbnail
            img = img.resize((img.width*2, img.height*2), PIL_Image.LANCZOS)
        buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
        text = pytesseract.image_to_string(PIL_Image.open(buf))
    except Exception as e:
        return [], {"error": str(e)}

    records = _extract_text_pii(text)
    fields = Counter()
    for r in records:
        for k in r: fields[k] += 1
    return records, {"total_records": len(records), "fields": dict(fields),
                     "ocr_chars": len(text), "method": "tesseract_ocr"}

# ─── DBF ──────────────────────────────────────────────────────
def extract_dbf(filepath):
    if not dbfread_mod: return [], {"error": "dbfread not installed"}
    try:
        table = dbfread_mod.DBF(filepath, encoding="utf-8", ignore_missing_memofile=True)
        rows = [list(table.field_names)]  # header
        for rec in table:
            rows.append([rec.get(f,"") for f in table.field_names])
    except Exception as e:
        return [], {"error": str(e)}
    return _tabular_extract(rows)

# ─── MDB / ACCDB ──────────────────────────────────────────────
def extract_mdb(filepath):
    """Extract from Access database using mdb-tools."""
    try:
        result = subprocess.run(["mdb-tables", "-1", filepath], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return [], {"error": "mdb-tables failed"}
        tables = [t.strip() for t in result.stdout.strip().split("\n") if t.strip()]
    except FileNotFoundError:
        return [], {"error": "mdb-tools not installed — apt install mdbtools"}
    except Exception as e:
        return [], {"error": str(e)}

    all_recs, all_info = [], []
    for tbl in tables:
        try:
            result = subprocess.run(["mdb-export", filepath, tbl], capture_output=True, text=True, timeout=30)
            if result.returncode != 0: continue
            rows = list(csv.reader(io.StringIO(result.stdout)))
            recs, info = _tabular_extract(rows)
            if recs:
                all_recs.extend(recs)
                all_info.append({"table": tbl, **info})
        except: pass

    fields = Counter()
    for r in all_recs:
        for k in r: fields[k] += 1
    return all_recs, {"total_records": len(all_recs), "fields": dict(fields),
                      "tables_scanned": len(tables), "tables_with_pii": len(all_info), "table_info": all_info}

# ─── PST ──────────────────────────────────────────────────────
def extract_pst(filepath):
    """Extract emails from Outlook PST using readpst, then process attachments."""
    try:
        tmp_dir = tempfile.mkdtemp(prefix="forentis_pst_")
        result = subprocess.run(["readpst", "-o", tmp_dir, "-e", filepath],
                               capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return [], {"error": f"readpst failed: {result.stderr[:200]}"}
    except FileNotFoundError:
        return [], {"error": "readpst not installed — apt install pst-utils"}
    except Exception as e:
        return [], {"error": str(e)}

    # Find all extracted files recursively
    extracted = []
    for root, dirs, files in os.walk(tmp_dir):
        for f in files:
            extracted.append(os.path.join(root, f))

    return [], {"handler": "pst", "extracted_dir": tmp_dir,
                "extracted_files": len(extracted), "files": extracted[:50]}  # cap listing

# ─── XML ──────────────────────────────────────────────────────
def extract_xml(filepath):
    """Extract PII from XML — look for record-like elements."""
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(filepath)
        root = tree.getroot()
    except Exception as e:
        return [], {"error": str(e)}

    # Short tag name map (XML tags are often abbreviated)
    SHORT_TAGS = {"n":"PERSON","name":"PERSON","fullname":"PERSON","employee":"PERSON",
                  "ssn":"US_SSN","sin":"US_SSN","tin":"US_SSN","taxid":"US_SSN",
                  "dob":"DATE_OF_BIRTH","dateofbirth":"DATE_OF_BIRTH","date_of_birth":"DATE_OF_BIRTH",
                  "birthdate":"DATE_OF_BIRTH","born":"DATE_OF_BIRTH",
                  "addr":"LOCATION","address":"LOCATION","street":"LOCATION",
                  "phone":"PHONE_NUMBER","tel":"PHONE_NUMBER","mobile":"PHONE_NUMBER",
                  "email":"EMAIL_ADDRESS","mail":"EMAIL_ADDRESS",
                  "acct":"ACCOUNT_NUMBER","account":"ACCOUNT_NUMBER","id":"GOVERNMENT_ID",
                  "city":"CITY","state":"STATE","zip":"ZIP","zipcode":"ZIP",
                  "firstname":"FIRST_NAME","first_name":"FIRST_NAME",
                  "lastname":"LAST_NAME","last_name":"LAST_NAME",}

    def _xml_tag_to_pii(tag):
        tag_clean = re.sub(r"\{.*\}", "", tag).lower().strip()
        # Try short map first, then header map
        pt = SHORT_TAGS.get(tag_clean)
        if pt: return pt
        return _map_header(tag_clean)

    # Find repeating elements (likely records)
    tag_counts = Counter()
    for elem in root.iter():
        tag_counts[elem.tag] += 1
    record_tags = [tag for tag, count in tag_counts.items() if count >= 3]

    records = []
    for rtag in record_tags:
        for elem in root.iter(rtag):
            rec = {}
            for child in elem:
                pii_type = _xml_tag_to_pii(child.tag)
                if pii_type and child.text and child.text.strip():
                    rec[pii_type] = child.text.strip()
            for attr, val in elem.attrib.items():
                pii_type = _xml_tag_to_pii(attr)
                if pii_type and val.strip():
                    rec[pii_type] = val.strip()
            if rec:
                rec = _combine_fields(rec)
                if rec: records.append(rec)
        if records: break

    fields = Counter()
    for r in records:
        for k in r: fields[k] += 1
    return records, {"total_records": len(records), "fields": dict(fields)}

# ─── JSON ─────────────────────────────────────────────────────
def extract_json(filepath):
    """Extract PII from JSON — handle arrays of objects."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return [], {"error": str(e)}

    # Find the array of records
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Look for the largest array value
        for k, v in data.items():
            if isinstance(v, list) and len(v) > len(items):
                items = v

    if not items or not isinstance(items[0], dict):
        return [], {"error": "No record array found", "total_records": 0}

    # Map keys to PII types
    sample = items[0]
    key_map = {}
    for key in sample.keys():
        pt = _map_header(key)
        if pt: key_map[key] = pt

    records = []
    for item in items:
        rec = {}
        for key, pt in key_map.items():
            val = item.get(key)
            if val and str(val).strip():
                rec[pt] = str(val).strip()
        if rec:
            rec = _combine_fields(rec)
            if rec: records.append(rec)

    fields = Counter()
    for r in records:
        for k in r: fields[k] += 1
    return records, {"total_records": len(records), "fields": dict(fields),
                     "items_in_source": len(items), "keys_mapped": key_map}

# ─── HTML ─────────────────────────────────────────────────────
def extract_html(filepath):
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
    except Exception as e:
        return [], {"error": str(e)}

    # Try table extraction first
    table_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S|re.I)
    if table_rows:
        rows = []
        for tr in table_rows:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S|re.I)
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if cells: rows.append(cells)
        recs, info = _tabular_extract(rows)
        if recs:
            info["method"] = "html_table"
            return recs, info

    # Fallback: strip tags, extract text PII
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&\w+;", " ", text)
    records = _extract_text_pii(text)
    fields = Counter()
    for r in records:
        for k in r: fields[k] += 1
    return records, {"total_records": len(records), "fields": dict(fields), "method": "html_text"}

# ─── TXT / LOG ────────────────────────────────────────────────
def extract_text(filepath):
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return [], {"error": str(e)}

    # Try as delimited first (many .txt files are actually tab/pipe delimited)
    lines = text.split("\n")
    if len(lines) > 2:
        for delim in ["\t", "|", ";"]:
            if lines[0].count(delim) >= 2:
                rows = [line.split(delim) for line in lines if line.strip()]
                recs, info = _tabular_extract(rows)
                if recs:
                    info["method"] = f"delimited({delim!r})"
                    return recs, info

    # Fallback: regex PII
    records = _extract_text_pii(text)
    fields = Counter()
    for r in records:
        for k in r: fields[k] += 1
    return records, {"total_records": len(records), "fields": dict(fields), "method": "regex"}

# ─── ZIP / Archive ────────────────────────────────────────────
def extract_archive(filepath):
    """Extract archive, return list of inner file paths for recursive processing."""
    tmp_dir = tempfile.mkdtemp(prefix="forentis_zip_")
    ext = Path(filepath).suffix.lower()
    try:
        if ext == ".zip":
            with zipfile.ZipFile(filepath, "r") as zf:
                zf.extractall(tmp_dir)
        elif ext in (".gz", ".tar", ".tgz"):
            import tarfile
            with tarfile.open(filepath, "r:*") as tf:
                tf.extractall(tmp_dir)
        elif ext == ".7z":
            import py7zr
            with py7zr.SevenZipFile(filepath, "r") as sz:
                sz.extractall(tmp_dir)
        elif ext == ".rar":
            try:
                import rarfile
                with rarfile.RarFile(filepath, "r") as rf:
                    rf.extractall(tmp_dir)
            except ImportError:
                return [], {"error": "rarfile not installed — pip install rarfile (also needs unrar)"}
        else:
            return [], {"error": f"Archive type {ext} not supported"}
    except Exception as e:
        return [], {"error": str(e)}

    files = []
    for root, dirs, fnames in os.walk(tmp_dir):
        for fn in fnames:
            files.append(os.path.join(root, fn))
    return [], {"handler": "archive", "extracted_dir": tmp_dir, "files": files}


# ─── HEIC / WEBP (phone photos) ──────────────────────────────
def extract_heic(filepath, vision_model=None, ollama_url=None, fallback_model=None):
    """Extract PII from HEIC/HEIF images (iPhone photos).
    Converts to PNG then routes through extract_image (which has vision+OCR)."""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        return [], {"error": "pillow-heif not installed — pip install pillow-heif"}
    if not PIL_Image:
        return [], {"error": "Pillow not installed"}
    try:
        img = PIL_Image.open(filepath).convert("RGB")
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        img.save(tmp.name, format="PNG")
        tmp.close()
        records, meta = extract_image(tmp.name, vision_model, ollama_url, fallback_model)
        os.unlink(tmp.name)
        meta["method"] = "heic→" + meta.get("method", "unknown")
        return records, meta
    except Exception as e:
        return [], {"error": str(e)}

# .webp is already supported by Pillow natively — just route to extract_image

# ─── XLSB (Excel Binary) ─────────────────────────────────────
def extract_xlsb(filepath):
    """Extract PII from .xlsb Excel Binary Workbook."""
    try:
        from pyxlsb import open_workbook
    except ImportError:
        return [], {"error": "pyxlsb not installed — pip install pyxlsb"}
    try:
        all_recs = []
        with open_workbook(filepath) as wb:
            for sheet in wb.sheets:
                with wb.get_sheet(sheet) as ws:
                    rows = []
                    for row in ws.rows():
                        rows.append([c.v for c in row])
                    recs, _ = _tabular_extract(rows)
                    all_recs.extend(recs)
        fields = Counter()
        for r in all_recs:
            for k in r: fields[k] += 1
        return all_recs, {"total_records": len(all_recs), "fields": dict(fields)}
    except Exception as e:
        return [], {"error": str(e)}

# ─── ODS / ODT (LibreOffice) ─────────────────────────────────
def extract_libreoffice_convert(filepath, target_ext):
    """Convert via LibreOffice then process the converted file."""
    try:
        tmp_dir = tempfile.mkdtemp()
        result = subprocess.run(
            ["libreoffice", "--headless", "--convert-to", target_ext, "--outdir", tmp_dir, filepath],
            capture_output=True, timeout=60)
        converted = os.path.join(tmp_dir, Path(filepath).stem + "." + target_ext)
        if not os.path.exists(converted):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return [], {"error": f"LibreOffice conversion to {target_ext} failed"}
        handler = DISPATCH.get("." + target_ext)
        if handler:
            recs, meta = handler(converted)
        else:
            recs, meta = [], {"error": f"No handler for .{target_ext}"}
        shutil.rmtree(tmp_dir, ignore_errors=True)
        meta["conversion"] = f"libreoffice→{target_ext}"
        return recs, meta
    except FileNotFoundError:
        return [], {"error": "libreoffice not installed"}
    except Exception as e:
        return [], {"error": str(e)}

def extract_ods(filepath):
    return extract_libreoffice_convert(filepath, "xlsx")

def extract_odt(filepath):
    return extract_libreoffice_convert(filepath, "docx")

def extract_numbers(filepath):
    return extract_libreoffice_convert(filepath, "xlsx")

def extract_pages(filepath):
    return extract_libreoffice_convert(filepath, "docx")

# ─── SQLite ───────────────────────────────────────────────────
def extract_sqlite(filepath):
    """Extract PII from SQLite database."""
    import sqlite3
    try:
        conn = sqlite3.connect(filepath)
        cursor = conn.cursor()
        tables = [r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    except Exception as e:
        return [], {"error": str(e)}

    all_recs, all_info = [], []
    for tbl in tables:
        try:
            cursor.execute(f'SELECT * FROM "{tbl}" LIMIT 5000')
            cols = [desc[0] for desc in cursor.description]
            rows_data = cursor.fetchall()
            rows = [cols] + [list(r) for r in rows_data]
            recs, info = _tabular_extract(rows)
            if recs:
                all_recs.extend(recs)
                all_info.append({"table": tbl, **info})
        except: pass

    conn.close()
    fields = Counter()
    for r in all_recs:
        for k in r: fields[k] += 1
    return all_recs, {"total_records": len(all_recs), "fields": dict(fields),
                      "tables_scanned": len(tables), "table_info": all_info}

# ─── SQL dump ─────────────────────────────────────────────────
def extract_sql(filepath):
    """Extract PII from SQL dump files (INSERT INTO statements)."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(10_000_000)  # cap at 10MB
    except Exception as e:
        return [], {"error": str(e)}

    # Find INSERT statements and extract values
    insert_re = re.compile(r"INSERT\s+INTO\s+\S+\s*(?:\([^)]+\))?\s*VALUES\s*\(([^)]+)\)", re.I)
    records = []
    for m in insert_re.finditer(text):
        vals = [v.strip().strip("'\"") for v in m.group(1).split(",")]
        # Try to detect PII in values
        rec = {}
        for v in vals:
            v = v.strip()
            if re.match(r"^\d{3}-\d{2}-\d{4}$", v): rec.setdefault("US_SSN", v)
            elif re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", v): rec.setdefault("DATE_OF_BIRTH", v)
            elif re.match(r"^[^@]+@[^@]+\.\w{2,}$", v): rec.setdefault("EMAIL_ADDRESS", v)
            elif re.match(r"^[A-Z][a-z]+ [A-Z][a-z]+$", v): rec.setdefault("PERSON", v)
        if rec: records.append(rec)

    # Also try regex on full text
    if not records:
        records = _extract_text_pii(text)

    fields = Counter()
    for r in records:
        for k in r: fields[k] += 1
    return records, {"total_records": len(records), "fields": dict(fields)}

# ─── vCard ────────────────────────────────────────────────────
def extract_vcf(filepath):
    """Extract PII from vCard (.vcf) files — these ARE PII directly."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return [], {"error": str(e)}

    records = []
    # Split into individual vCards
    cards = re.split(r"BEGIN:VCARD", text, flags=re.I)
    for card in cards:
        if not card.strip(): continue
        rec = {}
        fn = re.search(r"^FN[;:](.+)$", card, re.M)
        if fn: rec["PERSON"] = fn.group(1).strip()
        email = re.search(r"^EMAIL[;:](.+)$", card, re.M)
        if email: rec["EMAIL_ADDRESS"] = email.group(1).strip().split(";")[-1]
        tel = re.search(r"^TEL[;:](.+)$", card, re.M)
        if tel: rec["PHONE_NUMBER"] = re.sub(r"[^0-9+()-]", "", tel.group(1))
        addr = re.search(r"^ADR[;:](.+)$", card, re.M)
        if addr: rec["LOCATION"] = addr.group(1).replace(";", " ").strip()
        if rec: records.append(rec)

    fields = Counter()
    for r in records:
        for k in r: fields[k] += 1
    return records, {"total_records": len(records), "fields": dict(fields)}

# ─── MBOX ─────────────────────────────────────────────────────
def extract_mbox(filepath):
    """Extract emails from Unix mbox format, return attachments for processing."""
    import mailbox as mbox_mod
    try:
        mb = mbox_mod.mbox(filepath)
    except Exception as e:
        return [], {"error": str(e)}

    tmp_dir = tempfile.mkdtemp(prefix="forentis_mbox_")
    attachments = []
    for msg in mb:
        for part in msg.walk():
            if part.get_content_maintype() == "multipart": continue
            fn = part.get_filename()
            if fn:
                att_path = os.path.join(tmp_dir, fn)
                data = part.get_payload(decode=True)
                if data:
                    with open(att_path, "wb") as f: f.write(data)
                    attachments.append(att_path)

    return [], {"handler": "mbox", "extracted_dir": tmp_dir, "files": attachments,
                "message_count": len(mb), "attachment_count": len(attachments)}

# ─── Parquet ──────────────────────────────────────────────────
def extract_parquet(filepath):
    """Extract PII from Parquet columnar files."""
    try:
        import pandas as pd
        df = pd.read_parquet(filepath)
    except ImportError:
        return [], {"error": "pandas/pyarrow not installed — pip install pandas pyarrow"}
    except Exception as e:
        return [], {"error": str(e)}

    rows = [list(df.columns)] + df.head(10000).values.tolist()
    return _tabular_extract(rows)


# ═══════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════

DISPATCH = {
    # Documents
    ".pdf": extract_pdf,
    ".docx": extract_docx, ".doc": extract_doc, ".rtf": extract_rtf,
    ".odt": extract_odt, ".pages": extract_pages,
    # Spreadsheets
    ".xlsx": extract_xlsx, ".xlsm": extract_xlsx,  # openpyxl handles xlsm
    ".xls": extract_xls, ".xlsb": extract_xlsb,
    ".csv": extract_csv, ".tsv": lambda fp: extract_csv(fp, "\t"),
    ".ods": extract_ods, ".numbers": extract_numbers,
    ".parquet": extract_parquet,
    # Email
    ".msg": extract_email, ".eml": extract_email,
    ".pst": extract_pst, ".mbox": extract_mbox,
    # Images (including phone cameras)
    ".tif": extract_image, ".tiff": extract_image,
    ".png": extract_image, ".jpg": extract_image, ".jpeg": extract_image,
    ".bmp": extract_image, ".webp": extract_image,
    ".heic": extract_heic, ".heif": extract_heic,
    # Databases
    ".dbf": extract_dbf,
    ".mdb": extract_mdb, ".accdb": extract_mdb,
    ".sqlite": extract_sqlite, ".db": extract_sqlite,
    ".sql": extract_sql,
    # Structured data
    ".xml": extract_xml, ".json": extract_json,
    # Contacts
    ".vcf": extract_vcf,
    # Web
    ".html": extract_html, ".htm": extract_html,
    # Text
    ".txt": extract_text, ".log": extract_text,
    # Archives
    ".zip": extract_archive, ".7z": extract_archive,
    ".gz": extract_archive, ".tar": extract_archive, ".tgz": extract_archive,
    ".rar": extract_archive,
}

def process_file(filepath, vision_model=None, ollama_url=None, fallback_model=None):
    """Process a single file through the appropriate extractor."""
    ext = Path(filepath).suffix.lower()
    handler = DISPATCH.get(ext)
    if not handler:
        return [], {"error": f"Unsupported format: {ext}", "handler": "unknown"}

    t0 = time.time()
    try:
        if ext == ".pdf":
            records, meta = extract_pdf(filepath, vision_model, ollama_url, fallback_model)
        elif ext in (".jpg",".jpeg",".png",".bmp",".tif",".tiff",".webp"):
            records, meta = extract_image(filepath, vision_model, ollama_url, fallback_model)
        elif ext in (".heic",".heif"):
            records, meta = extract_heic(filepath, vision_model, ollama_url, fallback_model)
        else:
            records, meta = handler(filepath)
    except Exception as e:
        records, meta = [], {"error": str(e)}
    elapsed = time.time() - t0

    meta["filename"] = Path(filepath).name
    meta["extension"] = ext
    meta["size_bytes"] = os.path.getsize(filepath) if os.path.exists(filepath) else 0
    meta["time_seconds"] = round(elapsed, 2)
    return records, meta


def process_folder(folder_path, recursive=True, vision_model=None, ollama_url=None, fallback_model=None):
    """Process all files in a folder. Handles archives and emails recursively."""
    results = []
    all_records = []
    temp_dirs = []

    # Collect all files
    files = []
    skip_patterns = {"forentis_extraction_results.json", "hybrid_pipeline_results.json"}
    if recursive:
        for root, dirs, fnames in os.walk(folder_path):
            for fn in fnames:
                if fn in skip_patterns: continue
                files.append(os.path.join(root, fn))
    else:
        files = [os.path.join(folder_path, f) for f in os.listdir(folder_path)
                 if os.path.isfile(os.path.join(folder_path, f)) and f not in skip_patterns]

    # Count PDFs to show pipeline mode
    pdf_count = sum(1 for f in files if Path(f).suffix.lower() == ".pdf")
    pdf_mode = "full_pipeline (vision+template)" if vision_model and ollama_url else "quick_regex"

    print(f"\n{'═'*70}")
    print(f"FORENTIS AI — Unified Extraction")
    print(f"{'═'*70}")
    print(f"Folder: {folder_path}")
    print(f"Files found: {len(files)}")
    if pdf_count:
        print(f"PDF mode: {pdf_mode}")
        if vision_model: print(f"  Vision: {vision_model}, Fallback: {fallback_model or 'none'}")

    # Classify
    by_ext = Counter(Path(f).suffix.lower() for f in files)
    for ext, count in by_ext.most_common():
        supported = ext in DISPATCH
        print(f"  {'✅' if supported else '⬜'} {ext:8s}: {count} files")

    print(f"\n{'─'*70}")

    # Process each file
    for filepath in sorted(files):
        ext = Path(filepath).suffix.lower()
        if ext not in DISPATCH:
            results.append({"filename": Path(filepath).name, "extension": ext,
                           "error": "unsupported format", "total_records": 0})
            continue

        print(f"  📄 {Path(filepath).name:50s}", end=" ", flush=True)
        records, meta = process_file(filepath, vision_model, ollama_url, fallback_model)

        # Handle special cases that produce more files
        if meta.get("handler") in ("archive", "email", "pst"):
            sub_files = meta.get("files") or meta.get("attachments") or []
            body_recs = meta.get("body_records") or []
            if meta.get("temp_dir"): temp_dirs.append(meta["temp_dir"])
            if meta.get("extracted_dir"): temp_dirs.append(meta["extracted_dir"])

            # Include PII from email body
            if body_recs:
                all_records.extend(body_recs)
                print(f"→ {len(body_recs)} records from email body", end="")
                if sub_files: print(f" + {len(sub_files)} attachments")
                else: print()
            elif sub_files:
                print(f"→ {len(sub_files)} inner files")
            else:
                print(f"→ 0 records")

            for sf in sub_files:
                sf_ext = Path(sf).suffix.lower()
                if sf_ext in DISPATCH:
                    print(f"    📎 {Path(sf).name:46s}", end=" ", flush=True)
                    sub_recs, sub_meta = process_file(sf, vision_model, ollama_url, fallback_model)
                    sub_meta["source_archive"] = Path(filepath).name
                    n = sub_meta.get("total_records", len(sub_recs))
                    print(f"→ {n} records")
                    all_records.extend(sub_recs)
                    results.append(sub_meta)
        else:
            n = meta.get("total_records", len(records))
            print(f"→ {n} records ({meta.get('time_seconds',0):.1f}s)")
            all_records.extend(records)

        results.append(meta)

    # Cleanup temp dirs
    for td in temp_dirs:
        shutil.rmtree(td, ignore_errors=True)

    # Summary
    total = sum(r.get("total_records", 0) for r in results)
    all_fields = Counter()
    for r in all_records:
        for k in r:
            if not k.startswith("_"): all_fields[k] += 1

    successful = sum(1 for r in results if r.get("total_records", 0) > 0)
    failed = sum(1 for r in results if "error" in r)
    empty = len(results) - successful - failed

    print(f"\n{'═'*70}")
    print(f"SUMMARY")
    print(f"{'═'*70}")
    print(f"  Files processed: {len(results)}")
    print(f"  Successful: {successful}, Empty: {empty}, Failed: {failed}")
    print(f"  Total records: {total}")
    print(f"  Fields: {dict(all_fields)}")

    if failed:
        print(f"\n  Failures:")
        for r in results:
            if "error" in r:
                print(f"    {r.get('filename','?')}: {r['error'][:60]}")

    return all_records, results


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="Forentis AI — Unified PII Extraction")
    ap.add_argument("target", help="File or folder to process")
    ap.add_argument("--output", "-o", default="", help="Output JSON path")
    ap.add_argument("--max-rows", type=int, default=0, help="Max rows per file (0=all)")
    ap.add_argument("--vision-model", default="", help="Ollama vision model for PDF (e.g. qwen2.5vl:32b)")
    ap.add_argument("--fallback-model", default="", help="Fallback vision model (e.g. llama3.2-vision:latest)")
    ap.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama API URL")
    ap.add_argument("--quick", action="store_true", help="Skip vision, use quick regex for PDFs")
    args = ap.parse_args()

    # Vision config: use full pipeline if model specified and not --quick
    vision_model = args.vision_model if args.vision_model and not args.quick else None
    ollama_url = args.ollama_url if vision_model else None
    fallback_model = args.fallback_model if vision_model else None

    if os.path.isdir(args.target):
        records, results = process_folder(args.target, vision_model=vision_model,
                                          ollama_url=ollama_url, fallback_model=fallback_model)
    elif os.path.isfile(args.target):
        records, meta = process_file(args.target, vision_model, ollama_url, fallback_model)
        results = [meta]
        n = meta.get("total_records", len(records))
        print(f"\n📄 {meta.get('filename','?')}")
        print(f"   Records: {n}")
        print(f"   Fields: {meta.get('fields', {})}")
        if "error" in meta:
            print(f"   Error: {meta['error']}")
        for r in records[:5]:
            nm = r.get("PERSON", "?")
            other = {k: str(v)[:25] for k, v in r.items() if k != "PERSON" and not k.startswith("_")}
            print(f"   {nm:30s} {other}")
    else:
        print(f"❌ Not found: {args.target}")
        sys.exit(1)

    # Save results
    out_path = args.output or os.path.join(
        args.target if os.path.isdir(args.target) else os.path.dirname(args.target),
        "forentis_extraction_results.json"
    )
    try:
        output = {"results": results, "total_records": len(records),
                  "records_sample": records[:100]}
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"\n💾 {out_path}")
    except Exception as e:
        print(f"\n⚠ Could not save: {e}")

if __name__ == "__main__":
    main()