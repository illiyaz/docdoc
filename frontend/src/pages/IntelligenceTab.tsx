/**
 * Intelligence Tab — Document Understanding & Diagnostic View
 *
 * Clean, read-only view of what the LLM understood about each document
 * after analysis.  Two-panel layout:
 *   Left:  Document list with type, routing, confidence indicators
 *   Right: Selected document detail — understanding, field map, sample records
 *
 * Also supports:
 *   - Test Extract: extract N pages from onset to validate quality
 *   - Correction Memory: user can correct LLM understanding for future runs
 */
import { useState, useMemo } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import {
  FileText,
  MapPin,
  Brain,
  Route,
  AlertTriangle,
  CheckCircle,
  ChevronRight,
  Loader2,
  Beaker,
  MessageSquare,
  X,
  Users,
  Table,
  Layers,
  ArrowRight,
  Clock,
  Briefcase,
} from "lucide-react"
import { formatDistanceToNow, parseISO, format } from "date-fns"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import {
  getProjectIntelligence,
  testExtract,
  submitCorrection,
} from "@/api/client"
import type {
  DocIntelligence,
  ProjectIntelligence,
  TestExtractResult,
  IntelligenceFieldMap,
} from "@/api/client"


// ---------------------------------------------------------------------------
// Routing path display helpers
// ---------------------------------------------------------------------------

const PATH_LABELS: Record<string, { label: string; color: string; description: string }> = {
  coordinate: {
    label: "Coordinate",
    color: "bg-emerald-100 text-emerald-800 border-emerald-300",
    description: "Fast fixed-layout extraction using field maps (30-45ms/page)",
  },
  llm_table: {
    label: "LLM Table",
    color: "bg-blue-100 text-blue-800 border-blue-300",
    description: "LLM extracts tabular data with multiple records per page",
  },
  llm_template: {
    label: "LLM Template",
    color: "bg-indigo-100 text-indigo-800 border-indigo-300",
    description: "LLM extracts repeating template patterns",
  },
  vision_direct: {
    label: "Vision",
    color: "bg-purple-100 text-purple-800 border-purple-300",
    description: "Vision model processes each page individually",
  },
  presidio: {
    label: "Presidio",
    color: "bg-amber-100 text-amber-800 border-amber-300",
    description: "Pattern-matching PII detection (fallback)",
  },
  unknown: {
    label: "Not Routed",
    color: "bg-gray-100 text-gray-600 border-gray-300",
    description: "Routing not yet determined",
  },
}

const LAYOUT_LABELS: Record<string, { label: string; color: string }> = {
  fixed: { label: "Fixed Layout", color: "bg-emerald-50 text-emerald-700" },
  template_with_drift: { label: "Template", color: "bg-blue-50 text-blue-700" },
  variable: { label: "Variable", color: "bg-gray-50 text-gray-600" },
}

const FIELD_TYPE_COLORS: Record<string, string> = {
  PERSON: "bg-blue-100 text-blue-800",
  LOCATION: "bg-green-100 text-green-800",
  DATE_OF_BIRTH: "bg-yellow-100 text-yellow-800",
  US_SSN: "bg-red-100 text-red-800",
  EMAIL_ADDRESS: "bg-cyan-100 text-cyan-800",
  PHONE_NUMBER: "bg-orange-100 text-orange-800",
  US_DRIVER_LICENSE: "bg-pink-100 text-pink-800",
  GOVERNMENT_ID: "bg-red-100 text-red-800",
}


// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function ConfidenceMeter({ value, label }: { value: number; label?: string }) {
  const pct = Math.round(value * 100)
  const color = pct >= 80 ? "bg-emerald-500" : pct >= 60 ? "bg-amber-500" : "bg-red-500"
  return (
    <div className="flex items-center gap-2 text-xs">
      {label && <span className="text-muted-foreground">{label}</span>}
      <div className="flex-1 h-1.5 bg-gray-200 rounded-full overflow-hidden min-w-[60px]">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-muted-foreground tabular-nums">{pct}%</span>
    </div>
  )
}


function FieldMapDisplay({ fields }: { fields: IntelligenceFieldMap[] }) {
  if (!fields.length) return <span className="text-xs text-muted-foreground">No field map</span>
  return (
    <div className="space-y-1.5">
      {fields.map((f, i) => (
        <div key={i} className="flex items-center gap-2 text-xs">
          <Badge variant="outline" className={`text-[10px] px-1.5 py-0 ${FIELD_TYPE_COLORS[f.field_type] || "bg-gray-100 text-gray-700"}`}>
            {f.field_type}
          </Badge>
          <span className="text-muted-foreground">←</span>
          <code className="bg-gray-100 px-1.5 py-0.5 rounded text-[11px]">{f.anchor_text}</code>
          <span className="text-muted-foreground">({f.spatial_relationship})</span>
        </div>
      ))}
    </div>
  )
}


function DocListItem({
  doc,
  isSelected,
  onClick,
}: {
  doc: DocIntelligence
  isSelected: boolean
  onClick: () => void
}) {
  const path = PATH_LABELS[doc.routing.recommended_path] || PATH_LABELS.unknown
  const layout = LAYOUT_LABELS[doc.understanding.layout_type] || LAYOUT_LABELS.variable

  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-3 py-2.5 border-b transition-colors ${
        isSelected
          ? "bg-primary/5 border-l-2 border-l-primary"
          : "hover:bg-muted/50 border-l-2 border-l-transparent"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="font-medium text-sm truncate">{doc.file_name}</div>
          <div className="flex items-center gap-1.5 mt-1">
            <Badge variant="outline" className={`text-[10px] px-1.5 py-0 ${path.color}`}>
              {path.label}
            </Badge>
            <Badge variant="outline" className={`text-[10px] px-1.5 py-0 ${layout.color}`}>
              {layout.label}
            </Badge>
          </div>
          <div className="flex items-center gap-3 mt-1 text-[11px] text-muted-foreground">
            <span>{doc.page_count ?? "?"} pages</span>
            <span>{doc.understanding.document_type || doc.structure.document_type || "unknown"}</span>
          </div>
        </div>
        <ChevronRight className={`h-4 w-4 mt-1 flex-shrink-0 ${isSelected ? "text-primary" : "text-muted-foreground"}`} />
      </div>
    </button>
  )
}


/** Group header for a batch of documents from the same job. */
function JobGroupHeader({ jobId, docs }: { jobId: string; docs: DocIntelligence[] }) {
  const first = docs[0]
  const analyzedAt = first?.job_started_at || first?.analyzed_at
  const totalPages = docs.reduce((s, d) => s + (d.page_count || 0), 0)

  return (
    <div className="px-3 py-2 bg-muted/60 border-b flex items-center gap-2 sticky top-0 z-10">
      <Briefcase className="h-3.5 w-3.5 text-muted-foreground flex-shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="text-[11px] font-medium text-foreground">
          {docs.length} document{docs.length !== 1 ? "s" : ""} · {totalPages.toLocaleString()} pages
        </div>
        <div className="text-[10px] text-muted-foreground flex items-center gap-1">
          <Clock className="h-2.5 w-2.5" />
          {analyzedAt
            ? formatDistanceToNow(parseISO(analyzedAt), { addSuffix: true })
            : "unknown"}
          <span className="ml-1">· Job {jobId.slice(0, 8)}</span>
        </div>
      </div>
      <Badge variant="outline" className={`text-[10px] px-1.5 py-0 ${
        first?.job_status === "completed" || first?.job_status === "analyzed"
          ? "bg-green-50 text-green-700"
          : "bg-gray-50 text-gray-600"
      }`}>
        {first?.job_status || "unknown"}
      </Badge>
    </div>
  )
}


function TestExtractPanel({
  doc,
  projectId,
}: {
  doc: DocIntelligence
  projectId: string
}) {
  const [pages, setPages] = useState(3)
  const mutation = useMutation({
    mutationFn: () =>
      testExtract({
        document_id: doc.document_id,
        job_id: doc.job_id,
        pages,
      }),
  })

  const result = mutation.data as TestExtractResult | undefined

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm flex items-center gap-2">
          <Beaker className="h-4 w-4" />
          Test Extract
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex items-center gap-3 mb-3">
          <label className="text-xs text-muted-foreground">Pages from onset:</label>
          <input
            type="number"
            min={1}
            max={20}
            value={pages}
            onChange={(e) => setPages(parseInt(e.target.value) || 3)}
            className="w-16 px-2 py-1 text-xs border rounded"
          />
          <button
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
            className="px-3 py-1.5 text-xs font-medium text-white bg-primary rounded hover:bg-primary/90 disabled:opacity-50 flex items-center gap-1.5"
          >
            {mutation.isPending ? (
              <><Loader2 className="h-3 w-3 animate-spin" /> Running...</>
            ) : (
              <><Beaker className="h-3 w-3" /> Extract</>
            )}
          </button>
        </div>

        {mutation.isError && (
          <div className="text-xs text-red-600 bg-red-50 rounded px-3 py-2 mb-3">
            {(mutation.error as Error).message}
          </div>
        )}

        {result && (
          <div className="space-y-2">
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              <span>Path: <strong>{result.extraction_path}</strong></span>
              <span>Pages: {result.pages_tested}</span>
              <span>Records: <strong>{result.total_records}</strong></span>
            </div>

            {result.error && (
              <div className="text-xs text-red-600 bg-red-50 rounded px-3 py-2">
                {result.error}
              </div>
            )}

            {result.records.length > 0 && (
              <div className="border rounded overflow-hidden">
                <table className="w-full text-xs">
                  <thead className="bg-muted/50">
                    <tr>
                      <th className="px-2 py-1.5 text-left font-medium">Page</th>
                      <th className="px-2 py-1.5 text-left font-medium">Name</th>
                      <th className="px-2 py-1.5 text-left font-medium">Gov ID</th>
                      <th className="px-2 py-1.5 text-left font-medium">Email</th>
                      <th className="px-2 py-1.5 text-left font-medium">Phone</th>
                      <th className="px-2 py-1.5 text-left font-medium">Address</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.records.map((rec, i) => (
                      <tr key={i} className="border-t">
                        <td className="px-2 py-1.5 tabular-nums">{rec.page as number ?? "-"}</td>
                        <td className="px-2 py-1.5">{(rec.name as string) || "-"}</td>
                        <td className="px-2 py-1.5 font-mono">{(rec.gov_id as string) || "-"}</td>
                        <td className="px-2 py-1.5">{(rec.email as string) || "-"}</td>
                        <td className="px-2 py-1.5 font-mono">{(rec.phone as string) || "-"}</td>
                        <td className="px-2 py-1.5">{(rec.address as string) || "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {result.records.length === 0 && !result.error && (
              <div className="text-xs text-amber-600 bg-amber-50 rounded px-3 py-2">
                No records extracted. The field map or extraction path may need adjustment.
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}


function CorrectionPanel({
  doc,
  projectId,
}: {
  doc: DocIntelligence
  projectId: string
}) {
  const [isOpen, setIsOpen] = useState(false)
  const [field, setField] = useState("document_type")
  const [correctedValue, setCorrectedValue] = useState("")
  const [reason, setReason] = useState("")
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: () =>
      submitCorrection(projectId, {
        document_id: doc.document_id,
        field,
        original_value: field === "document_type"
          ? doc.understanding.document_type
          : field === "layout_type"
          ? doc.understanding.layout_type
          : null,
        corrected_value: correctedValue,
        reason: reason || undefined,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["intelligence", projectId] })
      setIsOpen(false)
      setCorrectedValue("")
      setReason("")
    },
  })

  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <MessageSquare className="h-3 w-3" />
        Correct this understanding
      </button>
    )
  }

  return (
    <Card className="border-amber-200 bg-amber-50/30">
      <CardContent className="pt-4">
        <div className="flex items-center justify-between mb-3">
          <span className="text-xs font-medium">Submit Correction</span>
          <button onClick={() => setIsOpen(false)} className="text-muted-foreground hover:text-foreground">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="space-y-2">
          <div>
            <label className="text-[11px] text-muted-foreground">What was wrong?</label>
            <select
              value={field}
              onChange={(e) => setField(e.target.value)}
              className="w-full px-2 py-1 text-xs border rounded mt-0.5"
            >
              <option value="document_type">Document Type</option>
              <option value="layout_type">Layout Type</option>
              <option value="routing_path">Routing Path</option>
              <option value="field_map">Field Map</option>
              <option value="records_per_page">Records Per Page</option>
              <option value="pages_per_instance">Pages Per Instance (Template)</option>
              <option value="other">Other</option>
            </select>
          </div>
          <div>
            <label className="text-[11px] text-muted-foreground">Correct value</label>
            <input
              value={correctedValue}
              onChange={(e) => setCorrectedValue(e.target.value)}
              placeholder="What should it be?"
              className="w-full px-2 py-1 text-xs border rounded mt-0.5"
            />
          </div>
          <div>
            <label className="text-[11px] text-muted-foreground">Reason (optional)</label>
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Why is this wrong?"
              className="w-full px-2 py-1 text-xs border rounded mt-0.5"
            />
          </div>
          <button
            onClick={() => mutation.mutate()}
            disabled={!correctedValue || mutation.isPending}
            className="px-3 py-1.5 text-xs font-medium text-white bg-amber-600 rounded hover:bg-amber-700 disabled:opacity-50"
          >
            {mutation.isPending ? "Saving..." : "Save Correction"}
          </button>
        </div>

        {doc.corrections.length > 0 && (
          <div className="mt-3 pt-3 border-t border-amber-200">
            <span className="text-[11px] font-medium text-muted-foreground">Previous corrections:</span>
            {doc.corrections.map((c, i) => (
              <div key={i} className="mt-1 text-[11px] text-muted-foreground">
                <strong>{c.field}</strong> → {String(c.corrected_value)}
                {c.reason && <span className="italic"> ({c.reason})</span>}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}


function DocumentDetail({
  doc,
  projectId,
}: {
  doc: DocIntelligence
  projectId: string
}) {
  const path = PATH_LABELS[doc.routing.recommended_path] || PATH_LABELS.unknown
  const layout = LAYOUT_LABELS[doc.understanding.layout_type] || LAYOUT_LABELS.variable

  return (
    <div className="space-y-4">
      {/* Header */}
      <div>
        <h3 className="text-lg font-semibold">{doc.file_name}</h3>
        <div className="flex items-center gap-2 mt-1 text-sm text-muted-foreground">
          <span>{doc.file_type}</span>
          <span>·</span>
          <span>{doc.page_count ?? "?"} pages</span>
          <span>·</span>
          <span>Onset page {doc.onset_page ?? 0}</span>
        </div>
        {doc.analyzed_at && (
          <div className="flex items-center gap-1.5 mt-1.5 text-xs text-muted-foreground">
            <Clock className="h-3 w-3" />
            Analyzed {format(parseISO(doc.analyzed_at), "MMM d, yyyy 'at' h:mm a")}
            {doc.job_started_at && (
              <span className="ml-1">
                · Job started {format(parseISO(doc.job_started_at), "MMM d, yyyy 'at' h:mm a")}
              </span>
            )}
          </div>
        )}
      </div>

      {/* Understanding Card */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm flex items-center gap-2">
            <Brain className="h-4 w-4" />
            LLM Understanding
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
            <div>
              <span className="text-muted-foreground">Document Type</span>
              <div className="font-medium">{doc.understanding.document_type || "Not identified"}</div>
              {doc.understanding.document_subtype && (
                <div className="text-muted-foreground">{doc.understanding.document_subtype}</div>
              )}
            </div>
            <div>
              <span className="text-muted-foreground">Issuing Entity</span>
              <div className="font-medium">{doc.understanding.issuing_entity || "Unknown"}</div>
            </div>
            <div>
              <span className="text-muted-foreground">Layout</span>
              <div className="flex items-center gap-1.5">
                <Badge variant="outline" className={`text-[10px] px-1.5 py-0 ${layout.color}`}>
                  {layout.label}
                </Badge>
                {doc.understanding.layout_confidence > 0 && (
                  <span className="text-muted-foreground">
                    {Math.round(doc.understanding.layout_confidence * 100)}%
                  </span>
                )}
              </div>
            </div>
            <div>
              <span className="text-muted-foreground">Records/Page</span>
              <div className="font-medium">
                {doc.understanding.is_tabular ? (
                  <span className="flex items-center gap-1">
                    <Table className="h-3 w-3" />
                    ~{doc.understanding.records_per_page} (tabular)
                  </span>
                ) : (
                  doc.understanding.records_per_page
                )}
              </div>
            </div>
          </div>

          <ConfidenceMeter value={doc.understanding.schema_confidence} label="Schema confidence" />

          {doc.understanding.extraction_notes && (
            <div className="text-xs bg-muted/50 rounded px-3 py-2 text-muted-foreground">
              {doc.understanding.extraction_notes}
            </div>
          )}

          {/* Template info */}
          {doc.understanding.template && (
            <div className="border rounded px-3 py-2 text-xs space-y-1">
              <div className="font-medium flex items-center gap-1.5">
                <Layers className="h-3 w-3" />
                Repeating Template: {(doc.understanding.template as Record<string, unknown>).template_name as string}
              </div>
              <div className="text-muted-foreground">
                {(doc.understanding.template as Record<string, unknown>).pages_per_instance as number} pages per instance
                {(doc.understanding.template as Record<string, unknown>).total_instances_estimate && (
                  <> · ~{(doc.understanding.template as Record<string, unknown>).total_instances_estimate as number} individuals</>
                )}
              </div>
              {(doc.understanding.template as Record<string, unknown>).instance_marker && (
                <div className="text-muted-foreground">
                  Marker: <code className="bg-gray-100 px-1 rounded">{(doc.understanding.template as Record<string, unknown>).instance_marker as string}</code>
                </div>
              )}
            </div>
          )}

          {/* People identified */}
          {doc.understanding.people.length > 0 && (
            <div>
              <div className="text-[11px] text-muted-foreground mb-1 flex items-center gap-1">
                <Users className="h-3 w-3" /> People identified
              </div>
              <div className="space-y-1">
                {doc.understanding.people.map((p, i) => (
                  <div key={i} className="flex items-center gap-2 text-xs">
                    <span className="font-medium">{p.name}</span>
                    <Badge variant="outline" className="text-[10px] px-1 py-0">
                      {p.role}
                    </Badge>
                    {p.is_pii_subject && (
                      <Badge variant="outline" className="text-[10px] px-1 py-0 bg-red-50 text-red-700">
                        PII Subject
                      </Badge>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {doc.understanding.suppression_hints.length > 0 && (
            <div className="text-[11px] text-muted-foreground">
              <AlertTriangle className="h-3 w-3 inline mr-1" />
              Suppression: {doc.understanding.suppression_hints.join("; ")}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Routing Card */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm flex items-center gap-2">
            <Route className="h-4 w-4" />
            Extraction Routing
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-3 mb-3">
            <Badge variant="outline" className={`${path.color} text-xs`}>
              {path.label}
            </Badge>
            <span className="text-xs text-muted-foreground">{path.description}</span>
          </div>
          <div className="grid grid-cols-3 gap-4 text-xs">
            <div>
              <span className="text-muted-foreground">PII Fields</span>
              <div className="font-medium">{doc.routing.pii_field_count}</div>
            </div>
            <div>
              <span className="text-muted-foreground">Records/Page</span>
              <div className="font-medium">{doc.routing.records_per_page}</div>
            </div>
            <div>
              <span className="text-muted-foreground">Schema Skip</span>
              <div className="font-medium">
                {doc.routing.schema_skip ? (
                  <span className="text-emerald-600 flex items-center gap-1">
                    <CheckCircle className="h-3 w-3" /> Yes
                  </span>
                ) : (
                  "No"
                )}
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Field Map Card */}
      {doc.field_map.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <MapPin className="h-4 w-4" />
              Field Map ({doc.field_map.length} fields)
            </CardTitle>
          </CardHeader>
          <CardContent>
            <FieldMapDisplay fields={doc.field_map} />
          </CardContent>
        </Card>
      )}

      {/* Entity Analysis Card */}
      {doc.entities.summary && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <Users className="h-4 w-4" />
              Entity Analysis
            </CardTitle>
          </CardHeader>
          <CardContent className="text-xs space-y-2">
            <div>{doc.entities.summary}</div>
            {doc.entities.estimated_individuals != null && (
              <div className="font-medium">
                Estimated individuals: ~{doc.entities.estimated_individuals.toLocaleString()}
              </div>
            )}
            {doc.entities.guidance && (
              <div className="text-muted-foreground bg-muted/50 rounded px-3 py-2">
                {doc.entities.guidance}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Sample Extractions */}
      {doc.sample_extractions.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <FileText className="h-4 w-4" />
              Sample Extractions ({doc.sample_extractions.length})
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="border rounded overflow-hidden">
              <table className="w-full text-xs">
                <thead className="bg-muted/50">
                  <tr>
                    <th className="px-2 py-1.5 text-left font-medium">Type</th>
                    <th className="px-2 py-1.5 text-left font-medium">Value</th>
                    <th className="px-2 py-1.5 text-left font-medium">Confidence</th>
                    <th className="px-2 py-1.5 text-left font-medium">Page</th>
                  </tr>
                </thead>
                <tbody>
                  {doc.sample_extractions.map((s, i) => (
                    <tr key={i} className="border-t">
                      <td className="px-2 py-1.5">
                        <Badge variant="outline" className={`text-[10px] px-1 py-0 ${FIELD_TYPE_COLORS[s.pii_type] || ""}`}>
                          {s.pii_type}
                        </Badge>
                      </td>
                      <td className="px-2 py-1.5 font-mono">{s.masked_value}</td>
                      <td className="px-2 py-1.5 tabular-nums">
                        {Math.round(s.confidence * 100)}%
                      </td>
                      <td className="px-2 py-1.5 tabular-nums">{s.page ?? "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Test Extract */}
      <TestExtractPanel doc={doc} projectId={projectId} />

      {/* Correction */}
      <CorrectionPanel doc={doc} projectId={projectId} />
    </div>
  )
}


// ---------------------------------------------------------------------------
// Summary Bar
// ---------------------------------------------------------------------------

function SummaryBar({ data }: { data: ProjectIntelligence }) {
  const s = data.summary
  return (
    <div className="grid grid-cols-4 gap-4 mb-4">
      <div className="bg-muted/30 rounded-lg px-4 py-3">
        <div className="text-2xl font-bold">{s.total_documents}</div>
        <div className="text-xs text-muted-foreground">Documents</div>
      </div>
      <div className="bg-muted/30 rounded-lg px-4 py-3">
        <div className="text-2xl font-bold">{s.total_pages.toLocaleString()}</div>
        <div className="text-xs text-muted-foreground">Total Pages</div>
      </div>
      <div className="bg-muted/30 rounded-lg px-4 py-3">
        <div className="text-2xl font-bold">{s.routed_documents}</div>
        <div className="text-xs text-muted-foreground">Routed</div>
      </div>
      <div className="bg-muted/30 rounded-lg px-4 py-3">
        <div className="flex gap-1.5 flex-wrap">
          {Object.entries(s.path_distribution).map(([path, count]) => {
            const info = PATH_LABELS[path] || PATH_LABELS.unknown
            return (
              <Badge key={path} variant="outline" className={`text-[10px] ${info.color}`}>
                {info.label}: {count}
              </Badge>
            )
          })}
        </div>
        <div className="text-xs text-muted-foreground mt-1">Routing Distribution</div>
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Filter Bar
// ---------------------------------------------------------------------------

function FilterBar({
  docs,
  activeFilter,
  onFilter,
}: {
  docs: DocIntelligence[]
  activeFilter: string
  onFilter: (filter: string) => void
}) {
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: docs.length }
    for (const d of docs) {
      const p = d.routing.recommended_path || "unknown"
      c[p] = (c[p] || 0) + 1
    }
    return c
  }, [docs])

  const filters = [
    { key: "all", label: "All" },
    { key: "coordinate", label: "Coordinate" },
    { key: "llm_table", label: "LLM Table" },
    { key: "llm_template", label: "LLM Template" },
    { key: "vision_direct", label: "Vision" },
    { key: "presidio", label: "Presidio" },
    { key: "unknown", label: "Not Routed" },
  ].filter(f => f.key === "all" || (counts[f.key] || 0) > 0)

  return (
    <div className="flex gap-1 px-3 py-2 border-b overflow-x-auto">
      {filters.map(f => (
        <button
          key={f.key}
          onClick={() => onFilter(f.key)}
          className={`px-2 py-1 text-[11px] rounded-md whitespace-nowrap transition-colors ${
            activeFilter === f.key
              ? "bg-primary text-white"
              : "bg-muted/50 text-muted-foreground hover:bg-muted"
          }`}
        >
          {f.label} ({counts[f.key] || 0})
        </button>
      ))}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Main export
// ---------------------------------------------------------------------------

export function IntelligenceTab({ projectId }: { projectId: string }) {
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null)
  const [filter, setFilter] = useState("all")

  const { data, isLoading, isError } = useQuery({
    queryKey: ["intelligence", projectId],
    queryFn: () => getProjectIntelligence(projectId),
    refetchInterval: 30_000,
  })

  const filteredDocs = useMemo(() => {
    if (!data) return []
    if (filter === "all") return data.documents
    return data.documents.filter(d => (d.routing?.recommended_path || "unknown") === filter)
  }, [data, filter])

  /** Group filtered docs by job_id, maintaining desc date order. */
  const jobGroups = useMemo(() => {
    const groups: { jobId: string; docs: DocIntelligence[] }[] = []
    const seen = new Set<string>()
    for (const doc of filteredDocs) {
      const jid = doc.job_id || "unknown"
      if (!seen.has(jid)) {
        seen.add(jid)
        groups.push({ jobId: jid, docs: [] })
      }
      groups.find(g => g.jobId === jid)!.docs.push(doc)
    }
    return groups
  }, [filteredDocs])

  const selectedDoc = useMemo(
    () => filteredDocs.find(d => d.document_id === selectedDocId) || null,
    [filteredDocs, selectedDocId]
  )

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        <span className="text-sm">Loading document intelligence...</span>
      </div>
    )
  }

  if (isError) {
    return (
      <div className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-800">
        Failed to load intelligence data. Make sure at least one job has been analyzed.
      </div>
    )
  }

  if (!data || data.documents.length === 0) {
    return (
      <div className="text-center py-16">
        <Brain className="h-12 w-12 text-muted-foreground mx-auto mb-3" />
        <h3 className="text-lg font-medium">No analyzed documents yet</h3>
        <p className="text-sm text-muted-foreground mt-1">
          Run analysis on a job from the Jobs tab to see document intelligence here.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-0">
      <SummaryBar data={data} />

      <div className="border rounded-lg overflow-hidden flex" style={{ height: "calc(100vh - 320px)", minHeight: 500 }}>
        {/* Left: Document list */}
        <div className="w-80 border-r flex flex-col bg-white flex-shrink-0">
          <FilterBar docs={data.documents} activeFilter={filter} onFilter={setFilter} />
          <div className="overflow-y-auto flex-1">
            {jobGroups.map(group => (
              <div key={group.jobId}>
                <JobGroupHeader jobId={group.jobId} docs={group.docs} />
                {group.docs.map(doc => (
                  <DocListItem
                    key={doc.document_id}
                    doc={doc}
                    isSelected={doc.document_id === selectedDocId}
                    onClick={() => setSelectedDocId(doc.document_id)}
                  />
                ))}
              </div>
            ))}
          </div>
        </div>

        {/* Right: Detail */}
        <div className="flex-1 overflow-y-auto p-4 bg-muted/10">
          {selectedDoc ? (
            <DocumentDetail doc={selectedDoc} projectId={projectId} />
          ) : (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              <div className="text-center">
                <ArrowRight className="h-8 w-8 mx-auto mb-2 opacity-30" />
                <p className="text-sm">Select a document to view its intelligence</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
