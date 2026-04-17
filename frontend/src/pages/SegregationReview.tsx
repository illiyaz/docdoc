/**
 * Segregation Review page (Step 30e-3).
 *
 * Displays document groups produced by the segregation engine.
 * Auditor can approve, reject, or reclassify each group before
 * extraction begins.
 *
 * URL: /projects/:id/segregation?job_id=xxx
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
  ShieldOff,
  ChevronDown,
  ChevronUp,
  Loader2,
  ArrowLeft,
  CheckCheck,
  RefreshCw,
  Eye,
} from "lucide-react"
import {
  getSegregationGroups,
  approveSegregationGroup,
  rejectSegregationGroup,
  reclassifySegregationGroup,
  approveAllSegregationGroups,
  runSegregation,
} from "@/api/client"
import type { SegregationGroup } from "@/api/client"

// ---------------------------------------------------------------------------
// Status badge component
// ---------------------------------------------------------------------------

function StatusBadge({ status }: { status: string }) {
  if (status === "approved") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800">
        <CheckCircle className="h-3 w-3" /> Approved
      </span>
    )
  }
  if (status === "rejected") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-red-100 px-2.5 py-0.5 text-xs font-medium text-red-800">
        <XCircle className="h-3 w-3" /> Rejected
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800">
      <AlertTriangle className="h-3 w-3" /> Pending Review
    </span>
  )
}

// ---------------------------------------------------------------------------
// Field chip component
// ---------------------------------------------------------------------------

function FieldChip({ field, role }: { field: string; role?: string }) {
  const isPrimary = role === "primary_subject"
  return (
    <span
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ${
        isPrimary
          ? "bg-blue-100 text-blue-800"
          : "bg-gray-100 text-gray-700"
      }`}
    >
      {field}
      {role && (
        <span className="ml-1 text-[10px] opacity-60">
          ({isPrimary ? "primary" : "secondary"})
        </span>
      )}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Group card component
// ---------------------------------------------------------------------------

function GroupCard({
  group,
  projectId,
  jobId,
  onRefresh,
}: {
  group: SegregationGroup
  projectId: string
  jobId: string
  onRefresh: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [showReclassify, setShowReclassify] = useState(false)
  const [newDocType, setNewDocType] = useState(group.document_type)
  const [newIsPii, setNewIsPii] = useState(group.is_pii)
  const queryClient = useQueryClient()

  const approveMut = useMutation({
    mutationFn: () =>
      approveSegregationGroup(projectId, group.group_id, { reviewer_id: "auditor" }, jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["segregation", projectId] })
      onRefresh()
    },
  })

  const rejectMut = useMutation({
    mutationFn: () =>
      rejectSegregationGroup(projectId, group.group_id, { reviewer_id: "auditor" }, jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["segregation", projectId] })
      onRefresh()
    },
  })

  const reclassifyMut = useMutation({
    mutationFn: () =>
      reclassifySegregationGroup(
        projectId,
        group.group_id,
        {
          reviewer_id: "auditor",
          new_document_type: newDocType !== group.document_type ? newDocType : undefined,
          new_is_pii: newIsPii !== group.is_pii ? newIsPii : undefined,
        },
        jobId,
      ),
    onSuccess: () => {
      setShowReclassify(false)
      queryClient.invalidateQueries({ queryKey: ["segregation", projectId] })
      onRefresh()
    },
  })

  const isPending = group.status === "pending_review"
  const borderColor = group.is_pii
    ? "border-l-4 border-l-red-400"
    : "border-l-4 border-l-gray-300"

  return (
    <div className={`rounded-lg border bg-card shadow-sm ${borderColor}`}>
      {/* Card header */}
      <div className="flex items-start justify-between p-4">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            {group.is_pii ? (
              <Shield className="h-4 w-4 text-red-500" />
            ) : (
              <ShieldOff className="h-4 w-4 text-gray-400" />
            )}
            <h3 className="font-semibold text-sm">{group.group_name}</h3>
            <StatusBadge status={group.status} />
          </div>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span className="flex items-center gap-1">
              <FileText className="h-3 w-3" />
              {group.file_count} file{group.file_count !== 1 ? "s" : ""}
            </span>
            <span>Type: {group.document_type.replace(/_/g, " ")}</span>
            {group.primary_subject_type && (
              <span>Subject: {group.primary_subject_type}</span>
            )}
            <span>
              Confidence: {(group.confidence_avg * 100).toFixed(0)}%
              {group.confidence_min < group.confidence_avg && (
                <span className="text-amber-600 ml-1">
                  (min {(group.confidence_min * 100).toFixed(0)}%)
                </span>
              )}
            </span>
          </div>
        </div>

        <button
          onClick={() => setExpanded(!expanded)}
          className="p-1 rounded hover:bg-muted"
        >
          {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </button>
      </div>

      {/* Field inventory chips */}
      {group.field_inventory.length > 0 && (
        <div className="px-4 pb-3 flex flex-wrap gap-1">
          {group.field_inventory.map((field) => (
            <FieldChip
              key={field}
              field={field}
              role={group.role_summary[field]}
            />
          ))}
        </div>
      )}

      {/* Issuing entities */}
      {group.issuing_entities.length > 0 && (
        <div className="px-4 pb-3 text-xs text-muted-foreground">
          Entities: {group.issuing_entities.join(", ")}
        </div>
      )}

      {/* Expanded details */}
      {expanded && (
        <div className="border-t px-4 py-3 space-y-3 text-sm">
          {/* Sample files */}
          <div>
            <h4 className="text-xs font-medium text-muted-foreground mb-1">
              Sample Files ({group.sample_file_paths.length} of {group.file_count})
            </h4>
            <ul className="space-y-0.5">
              {group.sample_file_paths.map((fp) => (
                <li key={fp} className="flex items-center gap-1 text-xs">
                  <Eye className="h-3 w-3 text-muted-foreground" />
                  <span className="truncate">{fp.split("/").pop()}</span>
                </li>
              ))}
            </ul>
          </div>

          {/* Role summary */}
          {Object.keys(group.role_summary).length > 0 && (
            <div>
              <h4 className="text-xs font-medium text-muted-foreground mb-1">Role Attribution</h4>
              <div className="grid grid-cols-2 gap-1 text-xs">
                {Object.entries(group.role_summary).map(([field, role]) => (
                  <div key={field} className="flex justify-between">
                    <span>{field}</span>
                    <span className="text-muted-foreground">{role.replace(/_/g, " ")}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Review info */}
          {group.reviewed_by && (
            <div className="text-xs text-muted-foreground">
              Reviewed by {group.reviewed_by}
              {group.reviewed_at && (
                <span> at {new Date(group.reviewed_at).toLocaleString()}</span>
              )}
              {group.review_rationale && (
                <span className="block mt-0.5 italic">"{group.review_rationale}"</span>
              )}
            </div>
          )}

          {/* Reclassify form */}
          {showReclassify && (
            <div className="border rounded p-3 space-y-2 bg-muted/30">
              <h4 className="text-xs font-medium">Reclassify Group</h4>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={newDocType}
                  onChange={(e) => setNewDocType(e.target.value)}
                  className="flex-1 rounded border px-2 py-1 text-xs"
                  placeholder="Document type"
                />
                <label className="flex items-center gap-1 text-xs">
                  <input
                    type="checkbox"
                    checked={newIsPii}
                    onChange={(e) => setNewIsPii(e.target.checked)}
                  />
                  Contains PII
                </label>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => reclassifyMut.mutate()}
                  disabled={reclassifyMut.isPending}
                  className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  Save
                </button>
                <button
                  onClick={() => setShowReclassify(false)}
                  className="rounded border px-3 py-1 text-xs hover:bg-muted"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Action buttons */}
      {isPending && (
        <div className="border-t px-4 py-2 flex gap-2">
          <button
            onClick={() => approveMut.mutate()}
            disabled={approveMut.isPending}
            className="flex items-center gap-1 rounded bg-green-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
          >
            {approveMut.isPending ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <CheckCircle className="h-3 w-3" />
            )}
            Approve
          </button>
          <button
            onClick={() => rejectMut.mutate()}
            disabled={rejectMut.isPending}
            className="flex items-center gap-1 rounded bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50"
          >
            {rejectMut.isPending ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <XCircle className="h-3 w-3" />
            )}
            Reject
          </button>
          <button
            onClick={() => setShowReclassify(!showReclassify)}
            className="flex items-center gap-1 rounded border px-3 py-1.5 text-xs font-medium hover:bg-muted"
          >
            <RefreshCw className="h-3 w-3" />
            Reclassify
          </button>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main page component
// ---------------------------------------------------------------------------

export function SegregationReview() {
  const { id: projectId } = useParams<{ id: string }>()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const jobId = searchParams.get("job_id") ?? undefined

  const {
    data,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ["segregation", projectId, jobId],
    queryFn: () => getSegregationGroups(projectId!, jobId),
    enabled: !!projectId,
  })

  const runMut = useMutation({
    mutationFn: () => runSegregation(projectId!, { job_id: jobId!, sample_size: 5 }),
    onSuccess: () => refetch(),
  })

  const approveAllMut = useMutation({
    mutationFn: () =>
      approveAllSegregationGroups(projectId!, { reviewer_id: "auditor" }, jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["segregation", projectId] })
      refetch()
    },
  })

  if (!projectId) return <p className="text-red-500">Missing project ID</p>

  const groups = data?.groups ?? []
  const summary = data?.summary
  const piiGroups = groups.filter((g) => g.is_pii)
  const nonPiiGroups = groups.filter((g) => !g.is_pii)
  const hasPending = (summary?.pending_review ?? 0) > 0
  const allApproved = groups.length > 0 && (summary?.pending_review ?? 0) === 0 && (summary?.rejected ?? 0) === 0

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate(`/projects/${projectId}`)}
            className="p-1 rounded hover:bg-muted"
          >
            <ArrowLeft className="h-5 w-5" />
          </button>
          <div>
            <h1 className="text-xl font-bold">Segregation Review</h1>
            <p className="text-sm text-muted-foreground">
              Review document groups before extraction begins
            </p>
          </div>
        </div>

        <div className="flex gap-2">
          {groups.length === 0 && jobId && (
            <button
              onClick={() => runMut.mutate()}
              disabled={runMut.isPending}
              className="flex items-center gap-1 rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {runMut.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Shield className="h-4 w-4" />
              )}
              Run Segregation
            </button>
          )}
          {hasPending && (
            <button
              onClick={() => approveAllMut.mutate()}
              disabled={approveAllMut.isPending}
              className="flex items-center gap-1 rounded bg-green-600 px-4 py-2 text-sm font-medium text-white hover:bg-green-700 disabled:opacity-50"
            >
              {approveAllMut.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <CheckCheck className="h-4 w-4" />
              )}
              Approve All
            </button>
          )}
          {allApproved && (
            <button
              onClick={() => navigate(`/projects/${projectId}?tab=jobs&expand=${jobId}`)}
              className="flex items-center gap-1 rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
            >
              Proceed to Extraction
            </button>
          )}
        </div>
      </div>

      {/* Summary bar */}
      {summary && groups.length > 0 && (
        <div className="flex gap-4 rounded-lg border bg-card p-4">
          <div className="text-center">
            <div className="text-2xl font-bold">{summary.total_files}</div>
            <div className="text-xs text-muted-foreground">Total Files</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-bold">{summary.total_groups}</div>
            <div className="text-xs text-muted-foreground">Groups</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-bold text-amber-600">{summary.pending_review}</div>
            <div className="text-xs text-muted-foreground">Pending</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-bold text-green-600">{summary.approved}</div>
            <div className="text-xs text-muted-foreground">Approved</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-bold text-red-600">{summary.rejected}</div>
            <div className="text-xs text-muted-foreground">Rejected</div>
          </div>
        </div>
      )}

      {/* Loading / Error */}
      {isLoading && (
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading segregation groups...
        </div>
      )}
      {error && (
        <div className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          Failed to load groups: {(error as Error).message}
        </div>
      )}

      {/* Run segregation message */}
      {runMut.isSuccess && (
        <div className="rounded border border-green-200 bg-green-50 p-4 text-sm text-green-700">
          Segregation complete. {runMut.data?.total_groups} group(s) created from{" "}
          {runMut.data?.total_files} files.
        </div>
      )}
      {runMut.isError && (
        <div className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          Segregation failed: {(runMut.error as Error).message}
        </div>
      )}

      {/* PII Groups */}
      {piiGroups.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold flex items-center gap-2">
            <Shield className="h-4 w-4 text-red-500" />
            PII Document Groups ({piiGroups.length})
          </h2>
          <div className="space-y-3">
            {piiGroups.map((group) => (
              <GroupCard
                key={group.group_id}
                group={group}
                projectId={projectId}
                jobId={data?.job_id ?? ""}
                onRefresh={refetch}
              />
            ))}
          </div>
        </div>
      )}

      {/* Non-PII Groups */}
      {nonPiiGroups.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold flex items-center gap-2">
            <ShieldOff className="h-4 w-4 text-gray-400" />
            Non-PII Documents ({nonPiiGroups.length})
          </h2>
          <div className="space-y-3">
            {nonPiiGroups.map((group) => (
              <GroupCard
                key={group.group_id}
                group={group}
                projectId={projectId}
                jobId={data?.job_id ?? ""}
                onRefresh={refetch}
              />
            ))}
          </div>
        </div>
      )}

      {/* Empty state */}
      {!isLoading && groups.length === 0 && !runMut.isSuccess && (
        <div className="rounded-lg border border-dashed p-8 text-center text-muted-foreground">
          <Shield className="mx-auto h-10 w-10 opacity-30 mb-3" />
          <p className="font-medium">No segregation groups yet</p>
          <p className="text-sm mt-1">
            {jobId
              ? 'Click "Run Segregation" to classify documents in this job.'
              : "Select a job to review segregation groups."}
          </p>
        </div>
      )}
    </div>
  )
}
