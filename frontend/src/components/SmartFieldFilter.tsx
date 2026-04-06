import { useState, useMemo } from "react"
import { ChevronUp, ChevronDown, AlertTriangle, Lightbulb, X, Ban, CheckCircle } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { PIIBadge } from "@/components/PIIBadge"
import type { FieldFrequency, PersonFieldContext } from "@/api/client"

// ---------------------------------------------------------------------------
// Category definitions (mirrors PIIBadge.tsx)
// ---------------------------------------------------------------------------

const PHI_TYPES = new Set([
  "medical_record", "mrn", "hicn", "npi", "dea", "health_plan", "icd10",
  "nhs_number",
])
const FINANCIAL_TYPES = new Set([
  "credit_card", "financial_account", "routing_number", "iban",
])
const GOV_ID_TYPES = new Set([
  "ssn", "us_ssn", "aadhaar", "passport", "drivers_license", "government_id",
])
const CONTACT_TYPES = new Set(["email", "phone", "address"])

interface Category {
  id: string
  label: string
  types: Set<string>
  colorClass: string
}

const CATEGORIES: Category[] = [
  { id: "gov_id", label: "Government IDs", types: GOV_ID_TYPES, colorClass: "border-yellow-200 bg-yellow-50" },
  { id: "phi", label: "Healthcare / PHI", types: PHI_TYPES, colorClass: "border-red-200 bg-red-50" },
  { id: "financial", label: "Financial", types: FINANCIAL_TYPES, colorClass: "border-orange-200 bg-orange-50" },
  { id: "contact", label: "Contact Info", types: CONTACT_TYPES, colorClass: "border-blue-200 bg-blue-50" },
  { id: "other", label: "Other", types: new Set<string>(), colorClass: "border-gray-200 bg-gray-50" },
]

function categorize(piiType: string): string {
  const t = piiType.toLowerCase()
  for (const cat of CATEGORIES) {
    if (cat.id !== "other" && cat.types.has(t)) return cat.id
  }
  return "other"
}

// ---------------------------------------------------------------------------
// Suggestion engine
// ---------------------------------------------------------------------------

interface Suggestion {
  id: string
  message: string
  actionLabel: string
  affectedTypes: string[]
  severity: "info" | "warning"
}

function generateSuggestions(
  piiTypes: string[],
  fieldFrequency?: FieldFrequency[],
): Suggestion[] {
  const suggestions: Suggestion[] = []

  // Suggestion 1: High-frequency org metadata
  const orgFields = (fieldFrequency ?? []).filter((f) => f.is_org_metadata)
  if (orgFields.length > 0) {
    const typeList = orgFields.map((f) => f.pii_type.toUpperCase()).join(", ")
    suggestions.push({
      id: "org-metadata",
      message: `${orgFields.length} field${orgFields.length > 1 ? "s" : ""} (${typeList}) appear on most pages — likely organizational metadata.`,
      actionLabel: "Suppress All",
      affectedTypes: orgFields.map((f) => f.pii_type),
      severity: "warning",
    })
  }

  // Suggestion 2: High PII density
  if (piiTypes.length > 5) {
    suggestions.push({
      id: "high-density",
      message: `${piiTypes.length} PII types detected on this subject — review carefully for accuracy.`,
      actionLabel: "Flag for Review",
      affectedTypes: [],
      severity: "info",
    })
  }

  return suggestions
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface SmartFieldFilterProps {
  piiTypes: string[]
  fieldFrequency?: FieldFrequency[]
  personContext?: PersonFieldContext[]
}

// ---------------------------------------------------------------------------
// Frequency badge sub-component
// ---------------------------------------------------------------------------

function FrequencyBadge({ freq }: { freq: FieldFrequency }) {
  const ratio = freq.page_count / freq.total_pages
  const isHigh = ratio >= 0.8

  return (
    <span
      className={`inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded ${
        isHigh
          ? "bg-amber-100 text-amber-700"
          : "bg-gray-100 text-gray-500"
      }`}
      title={`Appears on ${freq.page_count} of ${freq.total_pages} pages`}
    >
      {freq.page_count}/{freq.total_pages} pg
      {isHigh && <AlertTriangle className="h-3 w-3" />}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function SmartFieldFilter({
  piiTypes,
  fieldFrequency,
  personContext,
}: SmartFieldFilterProps) {
  // Track which categories are expanded
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  // Track dismissed suggestions
  const [dismissedSuggestions, setDismissedSuggestions] = useState<Set<string>>(new Set())
  // Track suppressed types
  const [suppressedTypes, setSuppressedTypes] = useState<Set<string>>(new Set())

  // Build frequency lookup
  const freqMap = useMemo(() => {
    const map = new Map<string, FieldFrequency>()
    for (const f of fieldFrequency ?? []) {
      map.set(f.pii_type.toLowerCase(), f)
    }
    return map
  }, [fieldFrequency])

  // Group PII types by category
  const grouped = useMemo(() => {
    const groups: Record<string, string[]> = {}
    for (const cat of CATEGORIES) {
      groups[cat.id] = []
    }
    for (const t of piiTypes) {
      const catId = categorize(t)
      groups[catId].push(t)
    }
    return groups
  }, [piiTypes])

  // Person context lookup: pii_type → person info
  const personMap = useMemo(() => {
    const map = new Map<string, { name: string; role: string }>()
    for (const pc of personContext ?? []) {
      for (const t of pc.pii_types) {
        map.set(t.toLowerCase(), { name: pc.person_name, role: pc.role })
      }
    }
    return map
  }, [personContext])

  // Generate suggestions
  const suggestions = useMemo(
    () => generateSuggestions(piiTypes, fieldFrequency),
    [piiTypes, fieldFrequency],
  )

  const activeSuggestions = suggestions.filter(
    (s) => !dismissedSuggestions.has(s.id),
  )

  // Active (non-suppressed) types
  const activeTypes = piiTypes.filter((t) => !suppressedTypes.has(t.toLowerCase()))

  function toggleCategory(catId: string) {
    setCollapsed((prev) => ({ ...prev, [catId]: !prev[catId] }))
  }

  function handleDismiss(id: string) {
    setDismissedSuggestions((prev) => new Set(prev).add(id))
  }

  function handleSuppressTypes(types: string[]) {
    setSuppressedTypes((prev) => {
      const next = new Set(prev)
      for (const t of types) next.add(t.toLowerCase())
      return next
    })
  }

  function handleUnsuppressAll() {
    setSuppressedTypes(new Set())
  }

  return (
    <div className="space-y-3">
      {/* Smart suggestions */}
      {activeSuggestions.map((s) => (
        <div
          key={s.id}
          className={`flex items-start gap-2 rounded-md border px-3 py-2 text-sm ${
            s.severity === "warning"
              ? "bg-amber-50 border-amber-200 text-amber-800"
              : "bg-blue-50 border-blue-200 text-blue-800"
          }`}
        >
          <Lightbulb className="h-4 w-4 mt-0.5 shrink-0" />
          <div className="flex-1">
            <p>{s.message}</p>
            <div className="flex gap-2 mt-1.5">
              {s.affectedTypes.length > 0 && (
                <button
                  onClick={() => handleSuppressTypes(s.affectedTypes)}
                  className="text-xs font-medium underline hover:no-underline"
                >
                  {s.actionLabel}
                </button>
              )}
              <button
                onClick={() => handleDismiss(s.id)}
                className="text-xs text-muted-foreground hover:text-foreground"
              >
                Dismiss
              </button>
            </div>
          </div>
          <button
            onClick={() => handleDismiss(s.id)}
            className="shrink-0 opacity-50 hover:opacity-100"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      ))}

      {/* Bulk actions */}
      <div className="flex flex-wrap gap-2">
        {(fieldFrequency ?? []).some((f) => f.is_org_metadata) && (
          <button
            onClick={() =>
              handleSuppressTypes(
                (fieldFrequency ?? [])
                  .filter((f) => f.is_org_metadata)
                  .map((f) => f.pii_type),
              )
            }
            className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs font-medium hover:bg-muted"
          >
            <Ban className="h-3 w-3" />
            Suppress Org Metadata
          </button>
        )}
        {suppressedTypes.size > 0 && (
          <button
            onClick={handleUnsuppressAll}
            className="inline-flex items-center gap-1 rounded-md border border-green-200 bg-green-50 px-2.5 py-1 text-xs font-medium text-green-700 hover:bg-green-100"
          >
            <CheckCircle className="h-3 w-3" />
            Restore All ({suppressedTypes.size})
          </button>
        )}
      </div>

      {/* Summary line */}
      <p className="text-xs text-muted-foreground">
        {activeTypes.length} active data element type{activeTypes.length !== 1 ? "s" : ""}
        {suppressedTypes.size > 0 && (
          <span className="text-amber-600 ml-1">
            ({suppressedTypes.size} suppressed)
          </span>
        )}
      </p>

      {/* Grouped categories */}
      {CATEGORIES.map((cat) => {
        const types = grouped[cat.id] ?? []
        if (types.length === 0) return null
        const isCollapsed = collapsed[cat.id] ?? false
        const activeCount = types.filter(
          (t) => !suppressedTypes.has(t.toLowerCase()),
        ).length

        return (
          <div key={cat.id} className={`rounded-md border ${cat.colorClass}`}>
            <button
              onClick={() => toggleCategory(cat.id)}
              className="w-full flex items-center justify-between px-3 py-2 text-sm font-medium"
            >
              <span>
                {cat.label}{" "}
                <span className="text-muted-foreground font-normal">
                  ({activeCount}{activeCount !== types.length ? `/${types.length}` : ""})
                </span>
              </span>
              {isCollapsed ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronUp className="h-4 w-4" />
              )}
            </button>

            {!isCollapsed && (
              <div className="px-3 pb-3 space-y-1.5">
                {types.map((t) => {
                  const tLower = t.toLowerCase()
                  const freq = freqMap.get(tLower)
                  const person = personMap.get(tLower)
                  const isSuppressed = suppressedTypes.has(tLower)

                  return (
                    <div
                      key={t}
                      className={`flex items-center gap-2 flex-wrap ${
                        isSuppressed ? "opacity-40 line-through" : ""
                      }`}
                    >
                      <PIIBadge type={t} />
                      {freq && <FrequencyBadge freq={freq} />}
                      {person && (
                        <span className="text-xs text-muted-foreground">
                          {person.name}
                          <span className="ml-1 text-[10px] opacity-60">
                            ({person.role.replace(/_/g, " ")})
                          </span>
                        </span>
                      )}
                      {freq?.is_org_metadata && !isSuppressed && (
                        <Badge
                          variant="outline"
                          className="text-[10px] bg-amber-50 text-amber-600 border-amber-200"
                        >
                          likely org
                        </Badge>
                      )}
                      {isSuppressed ? (
                        <button
                          onClick={() => {
                            setSuppressedTypes((prev) => {
                              const next = new Set(prev)
                              next.delete(tLower)
                              return next
                            })
                          }}
                          className="text-[10px] text-green-600 hover:underline ml-auto"
                        >
                          restore
                        </button>
                      ) : (
                        <button
                          onClick={() => handleSuppressTypes([t])}
                          className="text-[10px] text-muted-foreground hover:text-red-600 ml-auto"
                        >
                          suppress
                        </button>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
