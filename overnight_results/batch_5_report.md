# Batch 5 Report: MSG Email Files

## Summary
- **Files Processed:** 5 MSG email files
- **Success Rate:** 5/5 (100%)
- **Total Records Extracted:** 49
- **Total Execution Time:** 0.03 seconds

## Per-File Breakdown

| File | Records | Body Chars | Sender | Subject Snippet | Time |
|------|---------|-----------|--------|-----------------|------|
| Vamp0000066511.msg | 6 | 3,116 | Ann Maynard | Fw: Drug-screen registration Ronald A. Bailey | 0.01s |
| Vamp0000066652.msg | 21 | 2,069 | Ann Maynard | Re: Alexandrea Malley - 02960-001016000... | 0.0s |
| Vamp0000068289.msg | 10 | 7,474 | Ann Maynard | Re: [EXTERNAL] Re: New Tax ID, Setup Information | 0.01s |
| Vamp0000068368.msg | 6 | 3,475 | Ann Maynard | FW: Drug-screen registration Elanie Rodriguez | 0.0s |
| Vamp0000068375.msg | 6 | 3,030 | Ann Maynard | FW: Drug-screen registration Marcelo Figueroa | 0.0s |

## PII Field Types Found

| Field Type | Count | Prevalence |
|------------|-------|------------|
| PERSON | 45 | 91.8% |
| PHONE_NUMBER | 10 | 20.4% |
| EMAIL_ADDRESS | 14 | 28.6% |
| LOCATION | 3 | 6.1% |

## Extraction Patterns & Notes

### Common Sender
- All 5 emails from single sender: "Ann Maynard <amaynard@jswallacegroup.com>"
- Indicates internal organizational communications
- Potential for org-level dedup (sender address appears in extraction results)

### Subject Patterns
- Drug-screen registrations (3 emails: Bailey, Rodriguez, Figueroa)
- Employee/supplier setup (1 email: Alexandrea Malley, Johnstone Supply)
- Tax ID/setup information (1 email: external/supplier onboarding)

### Repeating Organization Names (False Positive Signals)
- **"Ann Maynard"**: extracted 5 times (once per email signature)
- **"Johnstone Supply"**: extracted 3 times (subject reference, email body mentions)
- **"Group Rochester"**: extracted 3 times with consistent phone "(585) 482-8000"
- **"Robert Half"**: extracted 2 times (credit/HR processing)

### Contact Information Patterns
- Email domain: "amaynard@jswallacegroup.com" appears multiple times
- Service provider emails: "support@etbservices.com" (drug test/benefits provider)
- Shared org contact: "RobertHalfCreditDepartment@roberthalf.com"

### Phone Numbers Extracted
- "(585) 482-8000" — Group Rochester main line (appears 2x)
- "(540)373-9673" — Care facility contact
- "(610)827-5127" — Regional office
- "(717)200-8030" — Local site
- "800 356-1994" — National support line

## Potential False Positives

### Organizational Metadata
1. **Sender signature repeating:** Ann Maynard appears 5x as extracted PERSON (once per email footer)
2. **Org names as PERSON:** "Group Rochester", "Johnstone Supply", "Robert Half" marked as PERSON
3. **Boilerplate phone numbers:** Shared org numbers (e.g., "(585) 482-8000") extracted 2x
4. **Email domains:** Both personal ("amaynard@jswallacegroup.com") and service ("support@etbservices.com") extracted

### Recommended Filters
- Filter out sender addresses from extracted PERSON records
- Org phone numbers (>1 occurrence) likely false positives
- Corporate names ("Group Rochester", "Robert Half") should be de-emphasized or tagged as org

## Actual Individual Records

Based on unique person names in subjects:
- Ronald A. Bailey
- Alexandrea Malley
- Elanie Rodriguez
- Marcelo Figueroa
- Jennifer Atseff
- Nancy Varady

These 6 unique individuals are the true breach subjects.

## Recommendations

1. **Dedup by sender:** Remove Ann Maynard from PERSON extraction (appears 5x in signatures)
2. **Org-address filtering:** Apply org address/phone deny-list from email domain
3. **Entity linkage:** Consolidate org metadata (Johnstone Supply, Group Rochester) into single org entity
4. **Production readiness:** Manual review recommended before notification to validate subject linkage
5. **Metadata extraction:** Consider extracting sender org vs. individual person separately

## Quality Assessment

- **Raw extraction:** 49 records
- **Estimated true subjects:** 6 unique individuals
- **Org/boilerplate overhead:** ~43 records (88%) are org/metadata
- **FP rate:** Very high for email extraction without org deny-listing
