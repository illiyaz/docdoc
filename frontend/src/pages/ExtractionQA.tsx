/**
 * Extraction QA page (Step 30e-7).
 *
 * Three-panel layout:
 * 1. Summary dashboard (top) — completeness stats, per-document breakdown
 * 2. Smart sample panel (middle) — curated record samples
 * 3. Unresolved gaps panel (bottom) — gap resolution actions
 *
 * URL: /projects/:id/qa?job_id=xxx
 */
import { useState } from "react"
import { useParams, useSearchParams, useNavigate } from "react-router-dom"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import {
  CheckCircle,
  XCircle,
  AlertTriangle,
  FileText,
  Shield,
  BarChart3,
  Eye,
  ArrowLeft,
  Loader2,
  ChevronDown,
  ChevronUp,
  Edit3,
  Ban,
  HelpCircle,
  Lock,
} from "lucide-react"
import {
  getQASummary,
  getQASamples,
  getQAGaps,
  resolveQAGap,
  approveQA,
} from "@/api/client"
import type { QASample, ExtractionGap } from "@/api/client"

// ---------------------------------------------------------------------------
// Severity badge
// ---------------------------------------------------------------------------

function SeverityBadge({ severity }: { severity: string }) {
  if (severity === "high") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700">
        <AlertTriangle className="h-3 w-3" /> High
      </span>
    )
  }
  if (severity === "medium") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700">
        Medium
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">
      Low
    </span>
  )
}

// ---------------------------------------------------------------------------
// Category badge for samples
// ---------------------------------------------------------------------------

const CATEGORY_COLORS: Record<string, string> = {
  largest_group: "bg-blue-100 text-blue-700",
  gap_filled: "bg-purple-100 text-purple-700",
  merged: "bg-indigo-100 text-indigo-700",
  cross_type: "bg-teal-100 text-teal-700",
  edge_case: "bg-orange-100 text-orange-700",
}

const CATEGORY_LABELS: Record<string, string> = {
  largest_group: "Largest Group",
  gap_filled: "Gap Filled",
  merged: "Merged",
  cross_type: "Cross-Type",
  edge_case: "Edge Case",
}

function CategoryBadge({ category }: { category: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
        CATEGORY_COLORS[category] || "bg-gray-100 text-gray-600"
      }`}
    >
      {CATEGORY_LABELS[category] || category}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Sample card
// ---------------------------------------------------------------------------

function SampleCard({ sample }: { sample: QASample }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <CategoryBadge category={sample.category} />
            <span className="text-xs text-gray-500">
              p.{sample.page_num} · {sample.extraction_method}
            </span>
          </div>
          <p className="mt-1 text-sm text-gray-600">{sample.category_reason}</p>
        </div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-gray-400 hover:text-gray-600"
        >
          {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </button>
      </div>

      {/* Field chips */}
      <div className="mt-3 flex flex-wrap gap-2">
        {Object.entries(sample.fields).map(([type, value]) => (
          <span
            key={type}
            className="inline-flex items-center gap-1 rounded bg-gray-50 px-2 py-1 text-xs font-mono"
          >
            <span className="font-semibold text-gray-700">{type}:</span>
            <span className="text-gray-500">{value}</span>
          </span>
        ))}
      </div>

      {expanded && (
        <div className="mt-3 space-y-2 border-t pt-3 text-xs text-gray-500">
          <p>Document: {sample.document_name || sample.document_id}</p>
          <p>Record ID: {sample.record_id}</p>
          {sample.merge_explanation && (
            <p className="text-indigo-600">Merge: {sample.merge_explanation}</p>
          )}
          {sample.gap_fill_method && (
            <p className="text-purple-600">
              Gap fill: {sample.gap_type} → {sample.gap_fill_method}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Gap resolution row
// ---------------------------------------------------------------------------

function GapRow({
  gap,
  index,
  projectId,
  jobId,
  onResolved,
}: {
  gap: ExtractionGap
  index: number
  projectId: string
  jobId: string
  onResolved: () => void
}) {
  const [showResolve, setShowResolve] = useState(false)
  const [value, setValue] = useState("")
  const [notes, setNotes] = useState("")

  const resolveMutation = useMutation({
    mutationFn: (body: { value?: string; action: string; notes?: string }) =>
      resolveQAGap(projectId, index, body, jobId),
    onSuccess: () => {
      setShowResolve(false)
      setValue("")
      setNotes("")
      onResolved()
    },
  })

  const isResolved = gap.fill_result === "filled" || gap.fill_result === "not_applicable"
  const isManual = gap.filled_by === "manual"

  return (
    <div
      className={`rounded-lg border p-4 ${
        isResolved
          ? "border-green-200 bg-green-50"
          : gap.severity === "high"
            ? "border-red-200 bg-red-50"
            : "border-gray-200 bg-white"
      }`}
    >
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <SeverityBadge severity={gap.severity} />
            <span className="text-sm font-medium text-gray-700">{gap.gap_type}</span>
            {gap.expected_field && (
              <span className="rounded bg-gray-100 px-1.5 py-0.5 text-xs font-mono text-gray-600">
                {gap.expected_field}
              </span>
            )}
            {isResolved && (
              <span className="inline-flex items-center gap-1 rounded-full bg-green-100 px-2 py-0.5 text-xs text-green-700">
                <CheckCircle className="h-3 w-3" />
                {isManual ? "Manually resolved" : "Auto-filled"}
              </span>
            )}
          </div>
          <p className="mt-1 text-xs text-gray-500">
            Page {gap.page_num} · {gap.document_name}
          </p>
          {gap.context && (
            <p className="mt-1 text-xs text-gray-400">{gap.context}</p>
          )}
          {gap.filled_value_masked && (
            <p className="mt-1 text-xs font-mono text-gray-600">
              Value: {gap.filled_value_masked}
            </p>
          )}
        </div>

        {!isResolved && gap.fill_result !== "not_applicable" && (
          <button
            onClick={() => setShowResolve(!showResolve)}
            className="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700"
          >
            Resolve
          </button>
        )}
      </div>

      {showResolve && (
        <div className="mt-3 space-y-3 border-t pt-3">
          <div>
            <label className="block text-xs font-medium text-gray-700">
              Enter value (visible on source page)
            </label>
            <div className="mt-1 flex gap-2">
              <input
                type="text"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                className="flex-1 rounded border border-gray-300 px-3 py-1.5 text-sm"
                placeholder={`Enter ${gap.expected_field || "value"}...`}
              />
              <button
                onClick={() =>
                  resolveMutation.mutate({ value, action: "resolve" })
                }
                disabled={!value || resolveMutation.isPending}
                className="rounded bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
              >
                <Edit3 className="mr-1 inline h-3 w-3" />
                Save
              </button>
            </div>
          </div>

          <div className="flex gap-2">
            <button
              onClick={() =>
                resolveMutation.mutate({ action: "mark_na", notes })
              }
              disabled={resolveMutation.isPending}
              className="flex items-center gap-1 rounded border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50"
            >
              <Ban className="h-3 w-3" /> Not Applicable
            </button>
            <button
              onClick={() =>
                resolveMutation.mutate({ action: "mark_unrecoverable", notes })
              }
              disabled={resolveMutation.isPending}
              className="flex items-center gap-1 rounded border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50"
            >
              <HelpCircle className="h-3 w-3" /> Unrecoverable
            </button>
          </div>

          <div>
            <input
              type="text"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className="w-full rounded border border-gray-300 px-3 py-1.5 text-xs"
              placeholder="Optional notes..."
            />
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function ExtractionQA() {
  const { id: projectId } = useParams<{ id: string }>()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const jobId = searchParams.get("job_id") || ""

  const [gapFilter, setGapFilter] = useState<string>("all")

  // Queries
  const summaryQuery = useQuery({
    queryKey: ["qa-summary", projectId, jobId],
    queryFn: () => getQASummary(projectId!, jobId),
    enabled: !!projectId && !!jobId,
  })

  const samplesQuery = useQuery({
    queryKey: ["qa-samples", projectId, jobId],
    queryFn: () => getQASamples(projectId!, jobId),
    enabled: !!projectId && !!jobId,
  })

  const gapsQuery = useQuery({
    queryKey: ["qa-gaps", projectId, jobId, gapFilter],
    queryFn: () =>
      getQAGaps(
        projectId!,
        jobId,
        gapFilter !== "all" ? { status: gapFilter } : undefined,
      ),
    enabled: !!projectId && !!jobId,
  })

  const approveMutation = useMutation({
    mutationFn: () => approveQA(projectId!, { reviewer_id: "auditor" }, jobId),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["qa-summary", projectId, jobId] })
      // Navigate to project detail (notification tab) after approval
      if (data?.status === "approved") {
        setTimeout(() => navigate(`/projects/${projectId}?tab=notification`), 1500)
      }
    },
  })

  const invalidateGaps = () => {
    queryClient.invalidateQueries({ queryKey: ["qa-gaps", projectId, jobId] })
    queryClient.invalidateQueries({ queryKey: ["qa-summary", projectId, jobId] })
  }

  if (!projectId || !jobId) {
    return (
      <div className="p-8 text-center text-gray-500">
        Missing project ID or job ID.
      </div>
    )
  }

  const summary = summaryQuery.data
  const isApproved = summary?.status === "approved"

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate(`/projects/${projectId}`)}
            className="text-gray-400 hover:text-gray-600"
          >
            <ArrowLeft className="h-5 w-5" />
          </button>
          <div>
            <h1 className="text-xl font-semibold text-gray-900">
              Extraction QA Review
            </h1>
            <p className="text-sm text-gray-500">
              Review extraction completeness before notification
            </p>
          </div>
        </div>
        {isApproved ? (
          <span className="inline-flex items-center gap-1 rounded-full bg-green-100 px-4 py-2 text-sm font-medium text-green-700">
            <CheckCircle className="h-4 w-4" /> Approved by{" "}
            {summary?.approved_by}
          </span>
        ) : (
          <button
            onClick={() => approveMutation.mutate()}
            disabled={approveMutation.isPending}
            className="inline-flex items-center gap-2 rounded-lg bg-green-600 px-4 py-2 text-sm font-medium text-white hover:bg-green-700 disabled:opacity-50"
          >
            {approveMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Shield className="h-4 w-4" />
            )}
            Approve for Notification
          </button>
        )}
      </div>

      {/* Approval blocked message */}
      {approveMutation.data?.status === "blocked" && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-4">
          <div className="flex items-center gap-2 text-red-700">
            <Lock className="h-4 w-4" />
            <span className="text-sm font-medium">
              {approveMutation.data.reason}
            </span>
          </div>
        </div>
      )}

      {/* ============================================================== */}
      {/* Section 1: Summary Dashboard */}
      {/* ============================================================== */}
      <div className="rounded-lg border border-gray-200 bg-white p-6">
        <h2 className="flex items-center gap-2 text-lg font-medium text-gray-800">
          <BarChart3 className="h-5 w-5 text-blue-500" /> Summary
        </h2>

        {summaryQuery.isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin text-gray-400" />
          </div>
        ) : summary ? (
          <div className="mt-4 space-y-4">
            {/* Stat cards */}
            <div className="grid grid-cols-4 gap-4">
              <div className="rounded-lg bg-blue-50 p-4 text-center">
                <p className="text-2xl font-bold text-blue-700">
                  {summary.stats.total_notification_subjects}
                </p>
                <p className="text-xs text-blue-600">Notification Subjects</p>
              </div>
              <div className="rounded-lg bg-indigo-50 p-4 text-center">
                <p className="text-2xl font-bold text-indigo-700">
                  {summary.stats.total_documents}
                </p>
                <p className="text-xs text-indigo-600">Documents</p>
              </div>
              <div className="rounded-lg bg-teal-50 p-4 text-center">
                <p className="text-2xl font-bold text-teal-700">
                  {summary.stats.total_pages}
                </p>
                <p className="text-xs text-teal-600">Pages</p>
              </div>
              <div
                className={`rounded-lg p-4 text-center ${
                  summary.stats.completeness_pct >= 95
                    ? "bg-green-50"
                    : summary.stats.completeness_pct >= 80
                      ? "bg-amber-50"
                      : "bg-red-50"
                }`}
              >
                <p
                  className={`text-2xl font-bold ${
                    summary.stats.completeness_pct >= 95
                      ? "text-green-700"
                      : summary.stats.completeness_pct >= 80
                        ? "text-amber-700"
                        : "text-red-700"
                  }`}
                >
                  {summary.stats.completeness_pct}%
                </p>
                <p className="text-xs text-gray-600">Completeness</p>
              </div>
            </div>

            {/* Gap summary */}
            {summary.gaps.total > 0 && (
              <div className="rounded bg-gray-50 p-3 text-sm text-gray-600">
                {summary.gaps.filled} gaps auto-filled,{" "}
                {summary.gaps.manually_resolved} manually resolved,{" "}
                {summary.gaps.unfilled} unfilled
                {summary.gaps.high_severity_unfilled > 0 && (
                  <span className="ml-2 font-medium text-red-600">
                    ({summary.gaps.high_severity_unfilled} high-severity
                    unresolved)
                  </span>
                )}
              </div>
            )}

            {/* Per-document breakdown */}
            {summary.per_document.length > 0 && (
              <div>
                <h3 className="text-sm font-medium text-gray-700">
                  Per-Document Breakdown
                </h3>
                <div className="mt-2 divide-y divide-gray-100 rounded border border-gray-200">
                  {summary.per_document.slice(0, 10).map((doc, i) => (
                    <div
                      key={i}
                      className="flex items-center justify-between px-3 py-2 text-sm"
                    >
                      <span className="truncate text-gray-700">
                        <FileText className="mr-1 inline h-3.5 w-3.5 text-gray-400" />
                        {doc.document_name}
                      </span>
                      <span className="text-xs text-gray-500">
                        {doc.record_count} records · {doc.page_count} pages
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <p className="mt-4 text-sm text-gray-500">No data available.</p>
        )}
      </div>

      {/* ============================================================== */}
      {/* Section 2: Smart Sample Panel */}
      {/* ============================================================== */}
      <div className="rounded-lg border border-gray-200 bg-white p-6">
        <h2 className="flex items-center gap-2 text-lg font-medium text-gray-800">
          <Eye className="h-5 w-5 text-purple-500" /> Sample Records
        </h2>
        <p className="mt-1 text-xs text-gray-500">
          Curated selection — not random. Covers largest groups, gap-filled
          records, merges, cross-type, and edge cases.
        </p>

        {samplesQuery.isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin text-gray-400" />
          </div>
        ) : samplesQuery.data?.samples.length ? (
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {samplesQuery.data.samples.map((sample, i) => (
              <SampleCard key={i} sample={sample} />
            ))}
          </div>
        ) : (
          <p className="mt-4 text-sm text-gray-500">
            No samples available. Run extraction first.
          </p>
        )}
      </div>

      {/* ============================================================== */}
      {/* Section 3: Unresolved Gaps */}
      {/* ============================================================== */}
      <div className="rounded-lg border border-gray-200 bg-white p-6">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-lg font-medium text-gray-800">
            <AlertTriangle className="h-5 w-5 text-amber-500" /> Extraction
            Gaps
          </h2>
          <div className="flex gap-1">
            {["all", "unfilled", "filled", "pending"].map((f) => (
              <button
                key={f}
                onClick={() => setGapFilter(f)}
                className={`rounded px-3 py-1 text-xs font-medium ${
                  gapFilter === f
                    ? "bg-gray-800 text-white"
                    : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                }`}
              >
                {f.charAt(0).toUpperCase() + f.slice(1)}
              </button>
            ))}
          </div>
        </div>

        {gapsQuery.isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin text-gray-400" />
          </div>
        ) : gapsQuery.data?.gaps.length ? (
          <div className="mt-4 space-y-3">
            {gapsQuery.data.gaps.map((gap, i) => (
              <GapRow
                key={i}
                gap={gap}
                index={i}
                projectId={projectId}
                jobId={jobId}
                onResolved={invalidateGaps}
              />
            ))}
          </div>
        ) : (
          <p className="mt-4 text-sm text-gray-500">
            {gapFilter === "all"
              ? "No gaps detected — extraction is complete."
              : `No ${gapFilter} gaps.`}
          </p>
        )}
      </div>
    </div>
  )
}
