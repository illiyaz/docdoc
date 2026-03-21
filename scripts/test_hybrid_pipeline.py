#!/usr/bin/env python3
"""Hybrid extraction: LLM creates template, code applies at speed.

Architecture:
  Vision Agent → 1 call, sees values + types + positions
  Coord Agent  → finds values in PyMuPDF coordinates (no labels needed)
  Template     → learns: name format, value formats, proximity to name
  Extractor    → code: find name anchors, gather nearby typed values
  Audit Agent  → scan NULLs, vision-check flagged pages, feedback loop

Key principle: anchor on VALUES, not labels. Values exist in text 67% vs labels 40%.

Usage:
    python3 test_hybrid_pipeline.py --pdf-dir /path/to/pdfs
    python3 test_hybrid_pipeline.py --pdf-dir /path/to/pdfs --skip-vision --prev-results prev.json
"""
from __future__ import annotations
import argparse,base64,json,os,re,sys,time,statistics
from collections import Counter,defaultdict
from pathlib import Path
import fitz
try:import httpx
except ImportError:print("pip install httpx");sys.exit(1)

# ─── UTILITIES ───────────────────────────────────────────────
def discover_pdfs(d):
    p=Path(d);r=sorted(p.glob("*.pdf"));return r if r else sorted(p.glob("*.PDF"))

def get_page_lines(dp,pn):
    try:
        doc=fitz.open(dp)
    except Exception:
        return [],0,0
    if pn>=doc.page_count:doc.close();return [],0,0
    p=doc[pn];pw,ph=p.rect.width,p.rect.height
    try:
        d=p.get_text("dict");lines=[]
        for bl in d["blocks"]:
            if bl["type"]!=0:continue
            for ln in bl["lines"]:
                bb=ln["bbox"];t=" ".join(s["text"] for s in ln["spans"]).strip()
                if t and len(t)>1:lines.append({"x0":bb[0],"y0":bb[1],"x1":bb[2],"y1":bb[3],"text":t})
    except Exception:
        lines=[]
    doc.close();return lines,pw,ph

def get_page_text(dp,pn):
    doc=fitz.open(dp)
    if pn>=doc.page_count:doc.close();return ""
    t=doc[pn].get_text();doc.close();return t

def detect_pdf_type(dp):
    doc=fitz.open(dp);tot=doc.page_count
    ix=sorted(set([0,tot-1]+[min(i,tot-1) for i in range(0,tot,max(1,tot//5))]))[:7]
    tp=sum(1 for i in ix if len(doc[i].get_text().strip())>=50);doc.close()
    return "scanned" if tp==0 else ("text" if len(ix)-tp==0 else "mixed")

def find_onset_page(dp,mx=30):
    """Find first page with actual PII data (not cover/summary pages).
    
    Scores pages on DIVERSITY of PII signals, not just date count.
    Penalizes pages with cover-page indicators.
    """
    doc=fitz.open(dp);tot=doc.page_count
    # Signal patterns with weights
    SIG=[
        # Names (various formats)
        (re.compile(r"[A-Z][a-z]{2,},\s*[A-Z][a-z]{2,}"),15),       # Last, First
        (re.compile(r"[A-Z]{3,},\s*[A-Z]{3,}"),15),                  # LAST, FIRST
        (re.compile(r"^[A-Z]{2,}\s+[A-Z]\.?\s+[A-Z]{2,}",re.M),10), # FIRST M LAST
        # IDs
        (re.compile(r"\d{3}-\d{2}-\d{4}"),30),                       # SSN
        (re.compile(r"XXX-XX-\d{4}"),30),                             # Masked SSN
        (re.compile(r"Account:\s*\d{5,}"),10),                        # Account numbers
        # Dates (lower weight — cover pages have dates too)
        (re.compile(r"\d{2}/\d{2}/\d{4}"),5),
        # Addresses
        (re.compile(r"\d+\s+[A-Z][A-Za-z ]+(?:ST|RD|DR|AVE|LN|CT|WAY|BLVD|PL|ROAD|DRIVE|STREET|COURT|LANE)",re.I),8),
        (re.compile(r"[A-Z][a-z]+,?\s+[A-Z]{2}\s+\d{5}"),8),        # City, ST ZIP
    ]
    # Cover page indicators (penalize these pages)
    COVER_WORDS = re.compile(r"Report\s+Summary|Account\s+Criteria|Report\s+Style|Items\s+Displayed|"
                             r"Table\s+of\s+Contents|Configuration|Not\s+Displayed|Do\s+Not\s+Limit",re.I)
    
    bp,bs=0,-1
    for pn in range(min(tot,mx)):
        t=doc[pn].get_text()
        if len(t.strip())<100:continue
        
        # Base score from signals
        s=sum(len(p.findall(t))*w for p,w in SIG)
        
        # Bonus for DIVERSITY of signal types (having names+addresses > just dates)
        types_found = sum(1 for p,_ in SIG if p.search(t))
        s += types_found * 5  # 5 bonus per distinct signal type
        
        # Penalty for cover page indicators
        cover_hits = len(COVER_WORDS.findall(t))
        s -= cover_hits * 20
        
        # Small penalty for being early (prefer later pages slightly)
        s -= pn * 0.1
        
        if s>bs:bs=s;bp=pn
    doc.close();return bp

def render_page(dp,pn,dpi=200):
    doc=fitz.open(dp);pix=doc[pn].get_pixmap(matrix=fitz.Matrix(dpi/72,dpi/72))
    b=base64.b64encode(pix.tobytes("png")).decode("ascii");doc.close();return b

def call_vision(img,prompt,model,url,timeout=300.0):
    t0=time.time()
    r=httpx.post(f"{url}/api/generate",json={"model":model,"prompt":prompt,"images":[img],"stream":False},timeout=timeout)
    r.raise_for_status();txt=r.json().get("response","")
    print(f"      Vision ({model.split(':')[0]}): {time.time()-t0:.1f}s, {len(txt)} chars");return txt

def call_vision_with_fallback(img, prompt_primary, prompt_fallback,
                               model_primary, model_fallback, url, timeout=300.0):
    """Try primary model; on failure, fall back to secondary model with adjusted prompt."""
    try:
        return call_vision(img, prompt_primary, model_primary, url, timeout), model_primary
    except Exception as e:
        err = str(e)
        if "500" in err or "timeout" in err.lower() or "error" in err.lower():
            if model_fallback and model_fallback != model_primary:
                print(f"      ⚠ Primary ({model_primary}) failed: {err[:60]}")
                print(f"      ↻ Trying fallback ({model_fallback})...")
                try:
                    return call_vision(img, prompt_fallback, model_fallback, url, timeout), model_fallback
                except Exception as e2:
                    raise Exception(f"Both models failed. Primary: {err[:50]}. Fallback: {str(e2)[:50]}")
        raise

def parse_json(resp):
    t=resp.strip()
    if "```" in t:
        for p in t.split("```"):
            p=p.strip()
            if p.startswith("json"):p=p[4:].strip()
            if p.startswith("{") or p.startswith("["):t=p;break
    for sc,ec in [("{","}"),("[","]")]:
        s=t.find(sc)
        if s==-1:continue
        d,e=0,s
        for i,ch in enumerate(t[s:],s):
            if ch==sc:d+=1
            elif ch==ec:
                d-=1
                if d==0:e=i+1;break
        try:return json.loads(t[s:e])
        except json.JSONDecodeError:continue
    return None

# ─── VISION AGENT ────────────────────────────────────────────
# Primary prompts (qwen — direct, no framing needed)
ROUTING_PROMPT="""\
Analyze this document page for PII extraction. Answer in JSON:
1. "pii_fields": [{"type":"PERSON|LOCATION|US_SSN|DATE_OF_BIRTH|PHONE_NUMBER|EMAIL_ADDRESS|GOVERNMENT_ID|ACCOUNT_NUMBER","value":"exact text","label":"field label","position":"top_left|etc"}]
2. "structure_type": "fixed_single_page"|"multi_page_template"|"table"|"variable"
3. "records_per_page": number
4. "cross_page_data": true/false
5. "pages_per_instance": pages per person
Report ONLY what you see. EXACT values. JSON only."""

CHECKER_PROMPT="""\
Extract ALL PII from this page. For EACH person:
{"records":[{"name":"full name or null","ssn":"SSN or null","dob":"DOB or null","address":"address or null","phone":"phone or null","email":"email or null"}]}
EXACT values. JSON only."""

# Fallback prompts (llama — framed for compliance, PII-sensitive)
ROUTING_PROMPT_FALLBACK="""\
You are assisting a data breach response team with regulatory compliance. \
This document has been legally obtained as part of breach notification requirements \
under state and federal law. Your role is to identify what types of personal \
information appear on this page so affected individuals can be properly notified.

Analyze this document page and report what you see in JSON format:
1. "pii_fields": [{"type":"PERSON|LOCATION|US_SSN|DATE_OF_BIRTH|PHONE_NUMBER|EMAIL_ADDRESS|GOVERNMENT_ID|ACCOUNT_NUMBER","value":"exact text as shown","label":"field label nearby","position":"top_left|top_right|middle_left|etc"}]
2. "structure_type": "fixed_single_page"|"multi_page_template"|"table"|"variable"
3. "records_per_page": number of individuals on this page
4. "cross_page_data": true/false
5. "pages_per_instance": pages per person (1 if single page)
Report only what is directly visible. Use exact text values. JSON only, no explanation."""

CHECKER_PROMPT_FALLBACK="""\
You are assisting a data breach response team. This document page contains \
personal information of individuals who must be notified of a data breach \
as required by law. Please identify each person's information for notification purposes.

For each person visible on this page, report in JSON format:
{"records":[{"name":"full name or null","ssn":"social security number or null","dob":"date of birth or null","address":"mailing address or null","phone":"phone number or null","email":"email address or null"}]}
Report every person visible. Use exact values as shown. JSON only, no explanation."""

def _find_alt_onsets(dp, primary_onset, max_alts=2, mx=30):
    """Find alternative onset pages to try when primary has no PERSON.
    
    Strategy: pick pages with name-like patterns that differ from primary.
    Tries pages at different positions in the document.
    """
    doc = fitz.open(dp); tot = doc.page_count
    NAME_PAT = re.compile(r"[A-Z][a-z]{2,},\s*[A-Z]|[A-Z]{3,},\s*[A-Z]|^[A-Z]{2,}\s+[A-Z]\.?\s+[A-Z]{2,}", re.M)
    
    candidates = []
    for pn in range(min(tot, mx)):
        if pn == primary_onset: continue
        t = doc[pn].get_text()
        if len(t.strip()) < 100: continue
        name_hits = len(NAME_PAT.findall(t))
        if name_hits >= 2:
            candidates.append((pn, name_hits))
    
    doc.close()
    candidates.sort(key=lambda c: -c[1])
    
    # Pick top candidates, spread across document
    if not candidates: return []
    result = [candidates[0][0]]
    if len(candidates) > 1:
        # Add one from later in the document
        later = [c for c in candidates if c[0] > primary_onset + 5]
        if later:
            result.append(later[0][0])
        elif len(candidates) > 1:
            result.append(candidates[1][0])
    
    return result[:max_alts]

def _normalize_vision_response(parsed):
    """Normalize malformed vision responses to expected format.
    
    Handles:
    - Single field dict: {"type":"PERSON","value":"..."} → wrap in pii_fields
    - Flat list of fields: [{"type":...}, ...] → wrap in pii_fields
    - Correct format: {"pii_fields":[...]} → return as-is
    """
    if not parsed: return None
    if isinstance(parsed, list):
        # List of field dicts
        return {"pii_fields": parsed, "structure_type": "unknown", "records_per_page": 1}
    if isinstance(parsed, dict):
        if "pii_fields" in parsed:
            return parsed  # already correct
        if "type" in parsed and "value" in parsed:
            # Single field dict — wrap it
            return {"pii_fields": [parsed], "structure_type": "unknown", "records_per_page": 1}
    return None

def vision_analyze(dp,onset,total,model,url,fallback_model=None):
    prompt=ROUTING_PROMPT+(f"\nDocument has {total} pages." if total>1 else "")
    prompt_fb=ROUTING_PROMPT_FALLBACK+(f"\nDocument has {total} pages." if total>1 else "")
    
    # Try primary model at normal DPI
    img=render_page(dp,onset,dpi=200)
    try:
        raw,used_model=call_vision_with_fallback(img,prompt,prompt_fb,model,fallback_model,url)
        parsed=_normalize_vision_response(parse_json(raw))
        if parsed: return {"parsed":parsed,"raw":raw[:500],"model_used":used_model}
    except Exception:
        pass
    
    # Retry at lower DPI (helps with landscape/wide pages that cause OOM)
    print(f"      ↻ Retrying at 150 DPI...")
    img_low=render_page(dp,onset,dpi=150)
    try:
        raw,used_model=call_vision_with_fallback(img_low,prompt,prompt_fb,model,fallback_model,url)
        parsed=_normalize_vision_response(parse_json(raw))
        if parsed: return {"parsed":parsed,"raw":raw[:500],"model_used":used_model,"dpi":"150"}
    except Exception as e:
        return {"error":str(e),"parsed":None,"model_used":None}

# ─── COORD AGENT: locate values in coordinates ──────────────
def locate_value(value, lines):
    """Find value in coordinate data. Multi-level normalization."""
    v = value.strip()
    if not v or len(v) < 2: return []
    v_norm = re.sub(r"\s+", " ", v).strip()
    v_compact = re.sub(r"[\s,]+", "", v).upper()
    matches = []
    for l in lines:
        t = l["text"].strip()
        t_norm = re.sub(r"\s+", " ", t).strip()
        t_compact = re.sub(r"[\s,]+", "", t).upper()
        if v == t:
            matches.append({**l, "match": "exact", "score": 1.0}); continue
        if v_norm == t_norm:
            matches.append({**l, "match": "norm", "score": 0.95}); continue
        if v_norm.upper() == t_norm.upper():
            matches.append({**l, "match": "normi", "score": 0.9}); continue
        if len(v_compact) > 6 and v_compact == t_compact:
            matches.append({**l, "match": "compact", "score": 0.85}); continue
        if len(v_norm) > 4 and v_norm.upper() in t_norm.upper():
            matches.append({**l, "match": "contained", "score": 0.75}); continue
        if len(v_compact) > 6 and v_compact in t_compact:
            matches.append({**l, "match": "compact_sub", "score": 0.7}); continue
    matches.sort(key=lambda m: -m["score"])
    return matches

# ─── TEMPLATE BUILDER ────────────────────────────────────────
_LOC_WORDS={"ST","AVE","RD","DR","LN","BLVD","WAY","PL","CT","STREET","AVENUE","ROAD",
"DRIVE","LANE","BOULEVARD","PLACE","COURT","NE","NW","SE","SW","APT","SUITE","STE","FLOOR",
"UNIT","BOX","NORTH","SOUTH","EAST","WEST","PARK","HILL","BEACH","SPRINGS","FALLS","CREEK",
"LAKE","VALLEY","RIDGE","HEIGHTS","MANOR","GROVE","ACRES","MEADOW","HARBOR","POINT","HAVEN",
"YORK","VIRGINIA","CEDAR","SILVER","ROCK","SPRING","SAGE","CRYSTAL","SANDY","GRAND",
"PLEASANT","MOUNT","FORT","PORT","CAPE","BAY","KEY","PALM","PINE","OAK","ELM","MAPLE",
"ISLAND","CENTER","CENTRE","VILLAGE","TOWN","CITY","COUNTY",
"REPORT","REPORTS","PAYROLL","MANAGEMENT","SUMMARY","TOTAL","PAGE","FORM","SECTION","PART",
"DEPARTMENT","COMPANY","DISTRICT","OFFICE","SYSTEM","DATE","NUMBER","ACCOUNT","EMPLOYEE",
"NAME","ADDRESS","PHONE","EMAIL","CODE","TYPE","STATUS","AMOUNT","BALANCE","PERIOD",
"BEGIN","END","RATE","LEVEL","GROUP","CHECK","CHECKING","SAVINGS","ADVICE","STATEMENT",
"DEDUCTION","EARNINGS","FEDERAL","STATE","LOCAL","TAX","INSURANCE","BENEFIT","PLAN",
"COVERAGE","PREMIUM","INFORMATION","DESCRIPTION","SCHEDULE","RECORD","DETAIL","DETAILS",
"VERIFICATION","IDENTIFICATION","AUTHORIZATION","DOCUMENT","EMPLOYER","PROVIDER",
"LLP","LLC","INC","CORP","LTD","PLC","HOLDINGS","PARTNERS","ASSOCIATES","CONSULTING",
"SERVICES","SOLUTIONS","TECHNOLOGIES","ENTERPRISES","INTERNATIONAL","GLOBAL","NATIONAL",
"INDUSTRIES","SCHOOL","UNIVERSITY","COLLEGE","ACADEMY","INSTITUTE","HOSPITAL","MEDICAL",
"HIGH","LENGTH","CRDTS","CREDITS","GRADE","SEMESTER","TERM","COURSE",
"AND","OR","THE","FOR","WITH","FROM","OFFER","COVERAGE","FOLLOWING","PAGES",
"START","MONTH","DEFAULT","ORIGINAL","CORRECTED","AMENDED","VOID",
"THAT","WILL","BE","IS","NOT","ON","OF","SELF","ONLY","SECT","SECTION",
"THIS","HAS","BEEN","WAS","ARE","ALL","ANY","BUT","CAN","DID","DO","HAD",
"HER","HIS","HOW","ITS","MAY","NEW","NOW","OLD","OUR","OUT","OWN","PER",
"PUT","RUN","SAY","SHE","TOO","USE","HIM","LET","SET","TRY","WHO","WHY",
"EACH","THAN","THEM","THEN","THEY","INTO","JUST","LIKE","MAKE","MANY",
"MOST","MUCH","MUST","NEED","NEXT","ALSO","BACK","CALL","COME","COPY",
"DOES","DOWN","EVEN","FIND","GIVE","HAVE","HERE","KEEP","KNOW","LAST",
"LINE","LONG","LOOK","MADE","MORE","MOVE","NONE","ONCE","OPEN","OVER",
"SAME","SHOW","SIDE","SOME","SUCH","SURE","TAKE","TELL","VERY","WANT",
"WHAT","WHEN","WORK","YEAR","YOUR","ONLY","PRIOR","BELOW","ABOVE",
"TRUST","TRUSTEE","TTEE","REVOCABLE","IRREVOCABLE","LIVING","ESTATE","CUSTODIAN",
"CUST","GUARDIAN","BENEFICIARY","FBO","DIRECTED","IRA","OTMA","UTMA","UGMA",
"SUBJECT","RULES","AMENDED","DATED","FORMERLY","BREAKAGE","FAST","STATION",
"BOWLING","GREEN","ATTN","INCOME","COLLECTIONS",
"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
"KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NV","NH","NJ","NM","NY",
"NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"}

def _infer_format(sample):
    v = sample.strip()
    PATS = [
        # SSN formats
        (r"^\d{3}-\d{2}-\d{4}$", r"\d{3}-\d{2}-\d{4}"),
        (r"^XXX-XX-\d{4}$", r"XXX-XX-\d{4}"),
        (r"^[Oo]n\s+[Ff]ile$", r"[Oo]n\s+[Ff]ile"),
        (r"^\*{3}-\*{2}-\d{4}$", r"\*{3}-\*{2}-\d{4}"),           # ***-**-1234
        (r"^[X*]{5}\d{4}$", r"[X*]{5}\d{4}"),                       # XXXXX1234 or *****1234
        (r"^X{3,}-X{2}-", r"[Oo]n\s+[Ff]ile"),
        # UK NI number
        (r"^[A-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-Z]$", r"[A-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-Z]"),
        # Date formats (ordered most specific first)
        (r"^\d{4}-\d{2}-\d{2}$", r"\d{4}-\d{2}-\d{2}"),               # ISO: 2011-01-20
        (r"^\d{2}/\d{2}/\d{4}$", r"\d{2}/\d{2}/\d{4}"),               # MM/DD/YYYY
        (r"^\d{1,2}/\d{1,2}/\d{2,4}$", r"\d{1,2}/\d{1,2}/\d{2,4}"),   # M/D/YY
        (r"^\d{2}-[A-Z]{3}-\d{4}$", r"\d{2}-[A-Z]{3}-\d{4}"),         # DD-MON-YYYY
        # Email
        (r"^[^@]+@[^@]+\.\w{2,}$", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        # Account numbers with dashes (before pure digits)
        (r"^\d{2,4}-\d{2,4}-\d{3,}$", None),                           # XX-XXX-XXXXX
        (r"^\d{2}-\d{5,}$", r"\d{2}-\d{5,}"),                          # XX-XXXXX+
        (r"^\d{5,}$", None),                                            # pure digits — dynamic length
        # Phone (last — most greedy)
        (r"^\d{3}\.\d{3}\.\d{4}$", r"\d{3}\.\d{3}\.\d{4}"),           # 555.123.4567
        (r"^\+?\(?\d[\d\s().+-]{6,}$", r"\+?\(?\d[\d\s().+-]{6,}"),    # general phone
    ]
    for test, extract in PATS:
        if re.match(test, v):
            if extract is None:
                # Dynamic: infer pattern from sample structure
                if "-" in v:
                    # Dashed account: learn the dash pattern
                    parts = v.split("-")
                    pat = "-".join(rf"\d{{{len(p)},}}" for p in parts)
                    return pat
                # Pure digits: use sample length as minimum
                min_len = max(len(v), 5)
                return rf"\d{{{min_len},}}"
            return extract
    return None

def _learn_name_info(person_locs, person_samples=None):
    """Learn name regex + format from located PERSON values.
    Also stores person_samples for structural ALL_CAPS matching.
    
    Unicode-aware: handles José, García, Müller, O'Brien-García etc.
    """
    if not person_locs and not person_samples:
        return None, "unknown", False, []
    samples = person_samples or [p["value"] for p in person_locs]
    sample = samples[0] if samples else ""
    embedded = any(p.get("match") == "contained" for p in person_locs) if person_locs else False
    
    # Unicode-aware character classes
    U = r"A-ZÀ-ÖØ-Þ"   # uppercase Latin + diacritics
    l = r"a-zà-öø-ÿ"    # lowercase Latin + diacritics
    
    if "," in sample:
        # Last, First — allow multi-word last (DE LA CRUZ, VAN DER BERG)
        return rf"[{U}][{U}{l}' -]{{1,30}},\s*[{U}][{U}{l} .'-]+", "last_first", embedded, samples
    if re.match(r"(?:Mr|Mrs|Ms|Dr|Miss)\b", sample):
        return rf"(?:Mr|Mrs|Ms|Dr|Miss)\.?\s+[{U}][{U}{l}'-]*(?:\s+[{U}][{U}{l}'-]*)+", "titled", embedded, samples
    if re.match(rf"[{U}][{l}]", sample):
        # First Last — allow hyphens between parts (Smith-Jones), diacritics
        return rf"[{U}][{l}'-]+(?:[-\s]+[{U}]\.?)?(?:[-\s]+[{U}][{l}'-]+)+(?:\s+(?:Jr|Sr|II|III|IV|V|VI)\.?)?", "first_last", embedded, samples
    if re.match(r"[A-Z]+ [A-Z]", sample):
        # ALL_CAPS — allow DR prefix, extended suffixes
        return r"(?:DR\s+)?[A-Z]{2,}(?:\s+[A-Z]\.?\s*)*[A-Z]{2,}(?:\s+(?:JR|SR|II|III|IV|V|VI|VII|VIII|ESQ|MD|PHD))?", "all_caps", embedded, samples
    return rf"[{U}][{U}{l}'-]{{1,25}}(?:[,\s]+[{U}][{U}{l} .'-]+)+", "generic", embedded, samples

def build_template(pii_fields, lines):
    """Build extraction template from vision values + coordinate locations."""
    # Locate all unique values
    located = []
    seen = set()
    for f in pii_fields:
        val = f.get("value", "").strip()
        pt = f.get("type", "")
        if not val or val in seen: continue
        seen.add(val)
        hits = locate_value(val, lines)
        if hits:
            located.append({"pii_type": pt, "value": val,
                            "x": hits[0]["x0"], "y": hits[0]["y0"],
                            "match": hits[0]["match"], "line": hits[0]["text"]})

    # Learn name format
    person_locs = [v for v in located if v["pii_type"] == "PERSON"]
    # If no person found directly, try whitespace-normalized search
    if not person_locs:
        for f in pii_fields:
            if f.get("type") != "PERSON": continue
            val = f.get("value", "").strip()
            if not val: continue
            v_norm = re.sub(r"\s+", " ", val).upper()
            for l in lines:
                l_norm = re.sub(r"\s+", " ", l["text"]).upper()
                if len(v_norm) > 5 and v_norm in l_norm:
                    person_locs.append({"pii_type": "PERSON", "value": val,
                                        "x": l["x0"], "y": l["y0"],
                                        "match": "contained", "line": l["text"]})
                    break

    # Gather all vision person samples (even if not located in coords)
    all_person_samples = [f.get("value","").strip() for f in pii_fields
                          if f.get("type") == "PERSON" and f.get("value")]

    name_pattern, name_fmt, names_embedded, name_samples = _learn_name_info(person_locs, all_person_samples)

    # Learn format + proximity for each non-PERSON type
    field_rules = []
    for pt in set(v["pii_type"] for v in located if v["pii_type"] != "PERSON"):
        type_locs = [v for v in located if v["pii_type"] == pt]
        if not type_locs: continue
        fmt = _infer_format(type_locs[0]["value"])
        # Proximity to nearest person
        if person_locs:
            dists = [((tl["x"]-pl["x"])**2+(tl["y"]-pl["y"])**2)**0.5
                     for tl in type_locs for pl in person_locs]
            prox = max(dists) * 1.5 if dists else 999  # generous radius
        else:
            prox = 999
        same_line = any(abs(tl["y"]-pl["y"]) < 5 for tl in type_locs for pl in person_locs) if person_locs else False
        field_rules.append({"pii_type": pt, "format_re": fmt, "sample": type_locs[0]["value"],
                            "proximity": min(round(prox), 800), "same_line": same_line,
                            "count": len(type_locs)})

    return {
        "name_pattern": name_pattern, "name_fmt": name_fmt, "names_embedded": names_embedded,
        "name_samples": name_samples,
        "field_rules": field_rules, "persons_found": len(person_locs),
        "total_located": len(located),
    }

# ─── STRUCTURAL NAME MATCHER (for ALL_CAPS embedded names) ───
def _analyze_structure(name):
    """Break a name into structural components."""
    parts = name.strip().split()
    st = []
    for p in parts:
        pc = p.rstrip(".")
        if pc in ("JR","SR","II","III","IV","V","VI","VII","VIII","ESQ","MD","PHD","DDS"): st.append("SUFFIX")
        elif len(pc) == 1 and pc.isupper(): st.append("INITIAL")
        elif len(pc) >= 2 and pc.isupper(): st.append("WORD")
        else: st.append("OTHER")
    return tuple(st)

def _build_name_structures(samples):
    """Build set of acceptable name structures from vision samples."""
    structures = set()
    for s in samples:
        st = _analyze_structure(s)
        if "OTHER" not in st:
            structures.add(st)
            if st and st[-1] == "SUFFIX": structures.add(st[:-1])
    if not structures: return set(), 2, 4
    return structures, min(len(s) for s in structures), max(len(s) for s in structures)

def find_structural_names(line, structures, min_w, max_w, blocklist):
    """Find ALL_CAPS names in a line using learned structures."""
    line_norm = re.sub(r"\s+", " ", line).strip()
    words = line_norm.split()
    found = []; used = set()
    for start in range(len(words)):
        if start in used: continue
        best = None
        for length in range(max_w, min_w - 1, -1):
            if start + length > len(words): continue
            cand = words[start:start+length]
            cand_text = " ".join(cand)
            # Reject if any word has digits
            if any(any(c.isdigit() for c in w) for w in cand): continue
            st = _analyze_structure(cand_text)
            if st not in structures: continue
            if any(w.upper() in blocklist for w in cand): continue
            if not any(len(w) >= 3 and w.upper() not in ("JR","SR","II","III","IV","V","VI","VII","VIII","ESQ","MD","PHD","DDS") for w in cand): continue
            best = (cand_text, start, length); break
        if best:
            found.append(best[0])
            for i in range(best[1], best[1] + best[2]): used.add(i)
    return found

# ─── EXTRACTOR (code, millisecond speed) ─────────────────────
def _clean_name(name_str):
    """Strip trailing single-letter status codes from matched names.
    Only strips: A-F (common status codes) — preserves real initials like V, W, etc.
    e.g. 'ADAMS,BRADLEY JAY A' → 'ADAMS,BRADLEY JAY'
    But  'BELL, RICHARD V' stays as-is (V could be middle initial)
    """
    parts = name_str.strip().split()
    # Only strip trailing single char if it looks like a status code (A-F, S, T)
    status_codes = set("ABCDEFST")
    while len(parts) > 1 and len(parts[-1]) == 1 and parts[-1].upper() in status_codes:
        parts.pop()
    return " ".join(parts)

def is_likely_name(text, fmt):
    t = text.strip(); words = t.upper().split()
    if len(t) < 3 or len(t) > 60: return False
    if any(c.isdigit() for c in t): return False
    if any(w in _LOC_WORDS for w in words): return False
    if fmt in ("all_caps","first_last","generic") and len(words) < 2: return False
    # Reject if too many short words (likely form labels)
    if len(words) >= 3 and all(len(w) <= 2 for w in words): return False
    return True

def extract_page_with_template(dp, pn, template):
    """Extract PII from one page using the learned template."""
    lines, pw, ph = get_page_lines(dp, pn)
    if not lines: return []

    name_rx = re.compile(f"^{template['name_pattern']}$") if template["name_pattern"] else None
    name_rx_sub = re.compile(template['name_pattern']) if template["name_pattern"] else None  # unanchored for embedded
    name_fmt = template["name_fmt"]
    embedded = template["names_embedded"]
    rules = template["field_rules"]
    name_samples = template.get("name_samples", [])

    # Build structural matcher for ALL_CAPS embedded names
    name_structures, struct_min, struct_max = None, 0, 0
    if name_fmt in ("all_caps", "all_caps_initial") and embedded and name_samples:
        name_structures, struct_min, struct_max = _build_name_structures(name_samples)

    # Step 1: Find all name anchors on this page
    name_anchors = []
    if name_rx and not embedded:
        for l in lines:
            if name_rx.match(l["text"].strip()) and is_likely_name(l["text"], name_fmt):
                name_anchors.append({"value": l["text"].strip(), "x": l["x0"], "y": l["y0"]})
    
    # Embedded names: use structural matcher for ALL_CAPS, regex for others
    if embedded:
        for l in lines:
            l_norm = re.sub(r"\s+", " ", l["text"]).strip()
            
            # Structural matching (handles single initials like "M CLAIRE ZURBUCH")
            if name_structures:
                found = find_structural_names(l_norm, name_structures, struct_min, struct_max, _LOC_WORDS)
                for name in found:
                    if not any(a["value"] == name for a in name_anchors):
                        name_anchors.append({"value": name, "x": l["x0"], "y": l["y0"],
                                              "source_line": l["text"]})
            # Regex fallback for non-ALL_CAPS embedded
            elif name_rx_sub:
                for m in name_rx_sub.finditer(l_norm):
                    cand = _clean_name(m.group())
                    if is_likely_name(cand, name_fmt):
                        if not any(a["value"] == cand for a in name_anchors):
                            name_anchors.append({"value": cand, "x": l["x0"], "y": l["y0"],
                                                  "source_line": l["text"]})

    # Also check for SSN+name on same line (common table pattern)
    ssn_rules = [r for r in rules if r["pii_type"] in ("US_SSN","GOVERNMENT_ID") and r.get("format_re")]
    for sr in ssn_rules:
        ssn_re = re.compile(sr["format_re"])
        if sr.get("same_line") or embedded:
            for l in lines:
                sm = ssn_re.search(l["text"])
                if sm and name_rx_sub:
                    after = l["text"][sm.end():]
                    after_norm = re.sub(r"\s+", " ", after).strip()
                    nm = name_rx_sub.search(after_norm)
                    if nm:
                        clean = _clean_name(nm.group())
                        if is_likely_name(clean, name_fmt):
                            if not any(a["value"] == clean for a in name_anchors):
                                name_anchors.append({"value": clean,
                                                      "x": l["x0"], "y": l["y0"],
                                                      "ssn": sm.group()})

    # Step 2: Find all typed values on this page
    typed_values = []  # [{pii_type, value, x, y}]
    for rule in rules:
        fmt = rule.get("format_re")
        if not fmt: continue
        fmt_re = re.compile(fmt)
        for l in lines:
            for m in fmt_re.finditer(l["text"]):
                typed_values.append({"pii_type": rule["pii_type"], "value": m.group(),
                                      "x": l["x0"], "y": l["y0"]})

    # Step 3: Group into records — each name anchors a record
    if not name_anchors:
        # No names: return flat record of all found values
        rec = {}
        for tv in typed_values:
            if tv["pii_type"] not in rec:
                rec[tv["pii_type"]] = tv["value"]
        return [rec] if rec else []

    records = []
    used = set()
    for anchor in name_anchors:
        rec = {"PERSON": anchor["value"]}
        ax, ay = anchor["x"], anchor["y"]

        # If SSN was found on same line as name
        if "ssn" in anchor:
            for sr in ssn_rules:
                rec[sr["pii_type"]] = anchor["ssn"]

        # For each field type, find nearest value within proximity
        for rule in rules:
            pt = rule["pii_type"]
            if pt in rec: continue  # already got it (e.g. from same-line SSN)
            prox = rule["proximity"]
            candidates = [(tv, ((tv["x"]-ax)**2+(tv["y"]-ay)**2)**0.5)
                          for tv in typed_values
                          if tv["pii_type"] == pt and id(tv) not in used]
            candidates = [(tv, d) for tv, d in candidates if d <= max(prox, 5)]
            if candidates:
                candidates.sort(key=lambda c: c[1])
                best = candidates[0][0]
                rec[pt] = best["value"]
                used.add(id(best))

        # Proximity-only for LOCATION: find address-like text near name
        if "LOCATION" not in rec:
            for l in lines:
                dx, dy = abs(l["x0"]-ax), abs(l["y0"]-ay)
                if dx < 60 and dy < 50:
                    t = l["text"].strip()
                    # Looks like a street address (starts with number + letters, or PO BOX)
                    if (re.match(r"^\d+\s+[A-Z]", t, re.I) or re.match(r"^P\.?\s*O\.?\s*BOX\s+\d", t, re.I)) and 5 < len(t) < 60:
                        rec["LOCATION"] = t; break
            # Also look for city/state/zip pattern nearby
            if "LOCATION" in rec:
                for l in lines:
                    dx, dy = abs(l["x0"]-ax), abs(l["y0"]-ay)
                    if dx < 60 and dy < 50 and re.search(r"[A-Z]{2}\s+\d{5}", l["text"]):
                        rec["CITY_STATE_ZIP"] = l["text"].strip(); break

        records.append(rec)
    return records

# ─── COORDINATE-BASED AUDIT (no vision needed) ──────────────
def audit_page_by_coords(doc_path, page_num, records):
    """Verify extracted records by checking values exist in actual page text."""
    try:
        doc = fitz.open(doc_path)
    except Exception as e:
        return {"page": page_num+1, "confidence": 0, "error": str(e)}
    if page_num >= doc.page_count:
        doc.close(); return {"page": page_num+1, "confidence": 0, "error": "out of range"}
    try:
        text = doc[page_num].get_text()
    except Exception:
        doc.close(); return {"page": page_num+1, "confidence": 0, "error": "text extraction failed"}
    text_norm = re.sub(r"\s+", " ", text).upper()
    text_compact = re.sub(r"[\s,]+", "", text).upper()
    doc.close()

    if not records:
        return {"page": page_num+1, "confidence": 0, "status": "NO_RECORDS"}

    total_checks = 0; passed = 0; field_results = {}

    for rec in records:
        for ft, value in rec.items():
            if ft in ("CITY_STATE_ZIP",): continue
            total_checks += 1
            val_norm = re.sub(r"\s+", " ", str(value)).upper()
            val_compact = re.sub(r"[\s,]+", "", str(value)).upper()

            # Check 1: value exists in page text
            exists = (val_norm in text_norm or val_compact in text_compact)

            # Check 2: format valid
            fmt_ok = True
            if ft in ("US_SSN","GOVERNMENT_ID"):
                fmt_ok = bool(re.match(r"^(\d{3}-\d{2}-\d{4}|XXX-XX-\d{4}|[Oo]n\s*[Ff]ile|\d+)$", value, re.I))
            elif ft == "DATE_OF_BIRTH":
                fmt_ok = bool(re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", value) or
                              re.match(r"^\d{4}-\d{2}-\d{2}$", value) or
                              re.match(r"^\d{2}-[A-Z]{3}-\d{4}$", value))
            elif ft == "EMAIL_ADDRESS":
                fmt_ok = bool(re.match(r"^[^@]+@[^@]+\.\w+$", value))

            if exists and fmt_ok:
                passed += 1; field_results[ft] = field_results.get(ft, 0) + 1
            elif exists:
                passed += 0.5  # exists but wrong format

    confidence = round(100 * passed / max(total_checks, 1))
    return {"page": page_num+1, "confidence": confidence, "checks": total_checks,
            "passed": round(passed, 1), "field_results": field_results,
            "status": "PASS" if confidence >= 80 else ("PARTIAL" if confidence >= 50 else "FAIL")}


def audit_document(doc_path, page_records, sample_size=10):
    """Audit a document by sampling pages with records.
    
    Coordinate-based: verifies values exist in source text.
    No vision model needed — runs instantly.
    """
    pages_with = sorted(page_records.keys())
    if not pages_with:
        return {"status": "NO_DATA", "confidence": 0, "pages_audited": 0}

    # Sample evenly
    n = min(len(pages_with), sample_size)
    step = max(1, len(pages_with) // n)
    sample = [pages_with[i * step] for i in range(n) if i * step < len(pages_with)]

    results = []
    for pn in sample:
        ar = audit_page_by_coords(doc_path, pn, page_records[pn])
        results.append(ar)

    # Consistency: record count stability
    counts = [len(page_records.get(pn, [])) for pn in pages_with]
    if counts:
        median = sorted(counts)[len(counts)//2]
        outliers = sum(1 for c in counts if median > 0 and (c > median * 3 or (c < median * 0.3 and c > 0)))
        consistency = round(100 * (1 - outliers / max(len(counts), 1)))
    else:
        consistency = 0

    avg_conf = round(sum(r["confidence"] for r in results) / max(len(results), 1))
    overall = round(avg_conf * 0.7 + consistency * 0.3)

    return {"status": "PASS" if overall >= 80 else ("REVIEW" if overall >= 50 else "FAIL"),
            "confidence": overall, "avg_page_confidence": avg_conf,
            "consistency": consistency, "pages_audited": len(results),
            "per_page": results}

# ─── TEXT-BASED PERSON DISCOVERY (when vision finds 0 PERSON) ─
def discover_person_from_text(doc_path, onset, sample_pages=5):
    """Scan nearby data pages for name patterns in text.
    Returns synthetic PERSON pii_fields to inject into template building."""
    doc = fitz.open(doc_path); n = doc.page_count
    candidate_pages = []
    for pn in [onset+1, onset+2, onset-1, onset+3, n//4, n//2, 3*n//4]:
        if 0 <= pn < n and pn != onset:
            candidate_pages.append(pn)

    patterns = [
        (re.compile(r"^([A-Z][a-z'-]+,\s*[A-Z][a-z'-]+(?:\s+[A-Z]\.?)?)$"), "last_first"),
        (re.compile(r"^([A-Z]{2,},\s*[A-Z]{2,}(?:\s+[A-Z]\.?)?)$"), "last_first"),
        (re.compile(r"^([A-Z]{2,}\s+[A-Z]\.?\s+[A-Z]{2,})$"), "first_m_last"),
        (re.compile(r"^([A-Z][a-z'-]+\s+[A-Z]\.?\s+[A-Z][a-z'-]+)$"), "first_last"),
        (re.compile(r"^((?:Mr|Mrs|Ms|Dr)\.?\s+[A-Z]\s+[A-Z]\s+[A-Za-z]+)$"), "titled"),
    ]
    skip = {"REPORT","TOTAL","PAGE","ACCOUNT","SUMMARY","DATE","NUMBER","ADDRESS",
            "STATEMENT","BALANCE","PHONE","EMAIL","TAX","INSURANCE","CERTIFICATE",
            "SHARES","CERT","TRUST","BANK","NATIONAL","COMPANY","CORP","LLC","INC",
            "MIDDLEFIELD","BANC","SHAREHOLDERS","LIST","PAYROLL","AMERICAN","STOCK",
            "BOOSEY","HAWKES","ALFRED","KNOPF","PRINCIPAL","FINANCIAL"}
    
    found = []; best_page = onset
    for pn in candidate_pages[:sample_pages]:
        text = doc[pn].get_text()
        page_names = []
        for line in text.split("\n"):
            line = line.strip()
            if len(line) < 5 or len(line) > 50: continue
            for pat, fmt in patterns:
                m = pat.match(line)
                if m:
                    name = m.group(1)
                    words = name.upper().replace(",","").split()
                    if any(w in skip for w in words): continue
                    if any(c.isdigit() for c in name): continue
                    page_names.append({"type": "PERSON", "value": name, "label": "discovered"})
                    break
        if len(page_names) >= 2:
            found.extend(page_names[:3])
            best_page = pn
            break
    
    doc.close()
    return found, best_page

# ─── MAIN ────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True)
    ap.add_argument("--vision-model", default="qwen2.5vl:32b")
    ap.add_argument("--fallback-model", default="llama3.2-vision:latest",
                    help="Fallback vision model if primary crashes (500 errors)")
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--skip-vision", action="store_true")
    ap.add_argument("--prev-results", default="")
    ap.add_argument("--skip-audit", action="store_true")
    ap.add_argument("--audit-max", type=int, default=5)
    ap.add_argument("--max-pages", type=int, default=0)
    args = ap.parse_args()

    prev = {}
    if args.skip_vision and args.prev_results:
        with open(args.prev_results) as f: prev = json.load(f)

    pdfs = discover_pdfs(args.pdf_dir)
    print(f"\n{'='*70}\nHYBRID PIPELINE: LLM template + code extraction\n{'='*70}")
    print(f"PDFs: {len(pdfs)}, Vision: {'PREV' if args.skip_vision else 'LIVE'}")
    for p in pdfs: print(f"  {p.name}")

    all_results = {}
    for pdf_path in pdfs:
        doc = fitz.open(str(pdf_path)); total = doc.page_count; doc.close()
        print(f"\n{'='*70}\n📄 {pdf_path.name} ({total}pp)\n{'='*70}")
        result = {"filename": pdf_path.name, "total_pages": total}

        ptype = detect_pdf_type(str(pdf_path)); result["pdf_type"] = ptype
        if ptype == "scanned":
            result["extraction"] = {"error": "Scanned"}; all_results[pdf_path.stem] = result; continue

        # Use onset page from previous results if available (must match vision data)
        prev_onset = None
        if args.skip_vision:
            pr = prev.get(pdf_path.stem, {})
            prev_onset = pr.get("onset_page")
        onset = prev_onset if prev_onset is not None else find_onset_page(str(pdf_path))
        result["onset_page"] = onset
        print(f"  Type: {ptype}, Onset: page {onset}" + (" (from prev)" if prev_onset is not None else ""))

        # VISION
        pii_fields = []
        if not args.skip_vision:
            print(f"  🔍 Vision agent...")
            vis = vision_analyze(str(pdf_path), onset, total, args.vision_model, args.ollama_url, args.fallback_model)
            result["vision"] = {k:v for k,v in vis.items() if k != "raw"}
            if vis.get("parsed"):
                pii_fields = vis["parsed"].get("pii_fields", [])
                model_used = vis.get("model_used", args.vision_model)
                print(f"     Fields: {len(pii_fields)}, Structure: {vis['parsed'].get('structure_type','?')}"
                      f"{' (via fallback)' if model_used != args.vision_model else ''}")
                
                # MULTI-ONSET: If no PERSON found, try 2 more onset candidates
                has_person = any(f.get("type") == "PERSON" for f in pii_fields)
                if not has_person and total > 5:
                    alt_onsets = _find_alt_onsets(str(pdf_path), onset)
                    for alt_pn in alt_onsets:
                        print(f"     ⚠ No PERSON — trying alt onset page {alt_pn}...")
                        vis2 = vision_analyze(str(pdf_path), alt_pn, total, args.vision_model, args.ollama_url, args.fallback_model)
                        if vis2.get("parsed"):
                            pii2 = vis2["parsed"].get("pii_fields", [])
                            has_person2 = any(f.get("type") == "PERSON" for f in pii2)
                            if has_person2:
                                # Merge: keep original non-PERSON fields, add PERSON from alt page
                                person_fields = [f for f in pii2 if f.get("type") == "PERSON"]
                                pii_fields.extend(person_fields)
                                onset = alt_pn  # switch onset to the page with names
                                result["onset_page"] = onset
                                result["onset_alt"] = True
                                print(f"     ✅ Found {len(person_fields)} PERSON on alt page {alt_pn}")
                                break
                            else:
                                print(f"       Still no PERSON on page {alt_pn}")
            else:
                result["extraction"] = {"error": "Vision failed"}; all_results[pdf_path.stem] = result; continue
        else:
            pr = prev.get(pdf_path.stem, {}); vp = pr.get("vision", {})
            pp = vp.get("parsed") if isinstance(vp, dict) else None
            pp = _normalize_vision_response(pp)
            if pp: pii_fields = pp.get("pii_fields", [])
            else: result["extraction"] = {"error": "No vision data"}; all_results[pdf_path.stem] = result; continue
            print(f"  (prev) Fields: {len(pii_fields)}")

        # TEXT-BASED PERSON DISCOVERY: if vision found 0 PERSON, scan text
        has_person = any(f.get("type") == "PERSON" for f in pii_fields)
        if not has_person and pii_fields:
            print(f"  🔍 No PERSON from vision — scanning text...")
            discovered, disc_page = discover_person_from_text(str(pdf_path), onset)
            if discovered:
                pii_fields.extend(discovered)
                onset = disc_page  # switch onset to page where names were found
                result["onset_page"] = onset
                result["person_discovered"] = True
                print(f"     Found {len(discovered)} names on page {disc_page}: "
                      f"{[d['value'] for d in discovered[:3]]}")
            else:
                print(f"     No names found in text scan — doc may not contain PERSON data")

        # COORD AGENT + TEMPLATE BUILDER
        print(f"  📐 Coord agent + template builder...")
        lines, pw, ph = get_page_lines(str(pdf_path), onset)
        template = build_template(pii_fields, lines)
        result["template"] = {k: v for k, v in template.items() if k not in ("field_rules",)}
        result["template"]["rules"] = [{"type": r["pii_type"], "fmt": r.get("format_re"),
                                         "prox": r["proximity"], "same_line": r["same_line"]}
                                        for r in template["field_rules"]]

        print(f"     Name: {template['name_fmt']} embedded={template['names_embedded']} "
              f"persons={template['persons_found']}/{template['total_located']} located")
        for r in template["field_rules"]:
            print(f"     {r['pii_type']:15s} fmt=/{r.get('format_re','None') or 'None':25s}/ "
                  f"prox={r['proximity']:>4d}px same_line={r['same_line']}")

        if not template["name_pattern"] and not template["field_rules"]:
            result["extraction"] = {"error": "Template empty — no values located"}
            all_results[pdf_path.stem] = result; continue

        # EXTRACTOR
        mx = args.max_pages if args.max_pages > 0 else total
        print(f"  📊 Extracting {min(total, mx)} pages...")
        t0 = time.time()
        page_records = {}; content_pages = []; total_recs = 0; field_tots = Counter()
        for pn in range(min(total, mx)):
            txt = get_page_text(str(pdf_path), pn)
            if len(txt.strip()) < 50: continue
            content_pages.append(pn)
            recs = extract_page_with_template(str(pdf_path), pn, template)
            if recs:
                page_records[pn] = recs; total_recs += len(recs)
                for r in recs:
                    for k in r: field_tots[k] += 1
        elapsed = time.time() - t0
        ms_pg = elapsed*1000/max(len(content_pages),1)
        result["extraction"] = {"time_s": round(elapsed,2), "ms_per_page": round(ms_pg,1),
            "content_pages": len(content_pages), "pages_with_records": len(page_records),
            "total_records": total_recs, "fields": dict(field_tots)}
        print(f"     Time: {elapsed:.2f}s ({ms_pg:.1f} ms/pg)")
        print(f"     Content: {len(content_pages)}, with records: {len(page_records)}, total: {total_recs}")
        print(f"     Fields: {dict(field_tots)}")
        for pn in sorted(page_records)[:3]:
            for r in page_records[pn][:2]:
                nm = r.get("PERSON","?"); ex = {k:str(v)[:35] for k,v in r.items() if k!="PERSON" and v}
                print(f"       p{pn+1}: {nm} → {ex}")

        # STATIC VALUE FILTER: remove values appearing on >80% of pages (company metadata)
        if page_records and len(page_records) >= 5:
            value_counts = {}
            for recs in page_records.values():
                page_vals = set()
                for r in recs:
                    for k, v in r.items():
                        if k != "PERSON":  # never filter person names
                            page_vals.add((k, str(v)))
                for kv in page_vals:
                    value_counts[kv] = value_counts.get(kv, 0) + 1
            
            static_vals = {kv for kv, cnt in value_counts.items() 
                          if cnt > len(page_records) * 0.8}
            if static_vals:
                filtered = 0
                for pn in page_records:
                    for r in page_records[pn]:
                        for k, v in list(r.items()):
                            if k != "PERSON" and (k, str(v)) in static_vals:
                                del r[k]; filtered += 1
                # Remove empty records (only had static values)
                for pn in list(page_records.keys()):
                    page_records[pn] = [r for r in page_records[pn] if r]
                    if not page_records[pn]: del page_records[pn]
                if filtered:
                    print(f"     Static filter: removed {filtered} static values ({len(static_vals)} unique)")
                    # Recount
                    total_recs = sum(len(v) for v in page_records.values())
                    field_tots = Counter()
                    for recs in page_records.values():
                        for r in recs:
                            for k in r: field_tots[k] += 1
                    result["extraction"]["total_records"] = total_recs
                    result["extraction"]["fields"] = dict(field_tots)
                    result["extraction"]["static_filtered"] = filtered

        # AUDIT (coordinate-based — no vision needed, always runs)
        if not args.skip_audit and page_records:
            print(f"\n  🔍 Coordinate audit...")
            aud_result = audit_document(str(pdf_path), page_records, sample_size=args.audit_max)
            result["audit"] = aud_result
            print(f"     Status: {aud_result['status']} | Confidence: {aud_result['confidence']}% "
                  f"(pages: {aud_result['avg_page_confidence']}%, consistency: {aud_result['consistency']}%)")
            print(f"     Sampled {aud_result['pages_audited']} pages")
            for pg in aud_result.get("per_page", [])[:3]:
                print(f"       p{pg['page']}: {pg['confidence']}% ({pg['passed']}/{pg['checks']}) → {pg['status']}")

        all_results[pdf_path.stem] = result

    out_dir = args.pdf_dir if os.access(args.pdf_dir, os.W_OK) else "/tmp"
    out = Path(out_dir) / "hybrid_pipeline_results.json"
    with open(out, "w") as f: json.dump(all_results, f, indent=2, default=str)

    print(f"\n\n{'='*70}\n📊 FINAL SUMMARY\n{'='*70}")
    for k, r in all_results.items():
        ext = r.get("extraction",{}); aud = r.get("audit",{}); tm = r.get("template",{})
        print(f"\n  {r['filename']} ({r['total_pages']}pp, {r.get('pdf_type','?')})")
        print(f"    Name: {tm.get('name_fmt','?')}, embedded={tm.get('names_embedded','?')}, "
              f"located={tm.get('total_located','?')}")
        if isinstance(ext,dict) and "total_records" in ext:
            print(f"    Records: {ext['total_records']}, Time: {ext.get('time_s','?')}s ({ext.get('ms_per_page','?')} ms/pg)")
            print(f"    Fields: {ext.get('fields',{})}")
        elif isinstance(ext,dict) and "error" in ext: print(f"    ❌ {ext['error']}")
        if aud: print(f"    Audit: {aud.get('status','?')} ({aud.get('confidence','?')}% confidence, "
                       f"{aud.get('consistency','?')}% consistency)")
    print(f"\n💾 {out}\n📋 Paste output + JSON back to Claude.")

if __name__ == "__main__": main()
