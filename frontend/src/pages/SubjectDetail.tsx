import { useParams, useSearchParams } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { useContext } from "react"
import { ArrowLeft } from "lucide-react"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { MaskedField } from "@/components/MaskedField"
import { PIIBadge } from "@/components/PIIBadge"
import { StatusBadge } from "@/components/StatusBadge"
import { AuditTimeline } from "@/components/AuditTimeline"
import { getJobResults, getAuditHistory } from "@/api/client"
import type { MaskedSubject } from "@/api/client"
import { JobIdContext } from "@/App"

export function SubjectDetail() {
  const { id } = useParams<{ id: string }>()
  const [searchParams] = useSearchParams()
  const contextJobId = useContext(JobIdContext)
  const jobId = searchParams.get("job") ?? contextJobId

  const subjectId = id ?? ""

  const {
    data: subject,
    isLoading: subjectLoading,
  } = useQuery({
    queryKey: ["subject", jobId, subjectId],
    queryFn: async (): Promise<MaskedSubject | null> => {
      if (!jobId) return null
      const results = await getJobResults(jobId)
      return results.find((s) => s.subject_id === subjectId) ?? null
    },
    enabled: !!jobId,
  })

  const { data: auditEvents } = useQuery({
    queryKey: ["audit-history", subjectId],
    queryFn: () => getAuditHistory(subjectId),
    retry: false,
  })

  const address = subject?.canonical_name
    ? formatAddress(subject)
    : null

  return (
    <div className="space-y-6">
      {/* Back button */}
      <button
        onClick={() => window.history.back()}
        className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to Queue
      </button>

      {subjectLoading ? (
        <LoadingSkeleton />
      ) : !subject && jobId ? (
        <p className="text-sm text-muted-foreground">Subject not found in job results.</p>
      ) : !jobId ? (
        <p className="text-sm text-muted-foreground">
          No job context — open this page from a job result to see subject details.
        </p>
      ) : null}

      {subject && (
        <>
          {/* Card 1 — Identity & Contact */}
          <Card>
            <CardHeader>
              <CardTitle className="text-xl">{subject.canonical_name}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <MaskedField label="Email" value={subject.canonical_email} />
                <MaskedField label="Phone" value={subject.canonical_phone} />
              </div>
              {address && (
                <MaskedField label="Address" value={address} />
              )}
              <div className="flex items-center gap-3">
                <span
                  className={`text-xs font-medium px-2 py-0.5 rounded ${
                    subject.notification_required
                      ? "bg-orange-100 text-orange-700"
                      : "bg-gray-100 text-gray-600"
                  }`}
                >
                  Notification: {subject.notification_required ? "Required" : "Not required"}
                </span>
                <StatusBadge status={subject.review_status} />
              </div>
            </CardContent>
          </Card>

          {/* Card 2 — PII Inventory */}
          <Card>
            <CardHeader>
              <CardTitle>Data Elements Found</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap gap-2 mb-3">
                {(subject.pii_types_found ?? []).map((t) => (
                  <PIIBadge key={t} type={t} />
                ))}
              </div>
              <p className="text-xs text-muted-foreground">
                {(subject.pii_types_found ?? []).length} data element type
                {(subject.pii_types_found ?? []).length !== 1 ? "s" : ""} identified
              </p>
            </CardContent>
          </Card>
        </>
      )}

      {/* Card 3 — Audit Trail (always shown if subjectId exists) */}
      <Card>
        <CardHeader>
          <CardTitle>Review History</CardTitle>
        </CardHeader>
        <CardContent>
          {auditEvents ? (
            <AuditTimeline events={auditEvents} />
          ) : (
            <p className="text-sm text-muted-foreground">No review activity yet</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function formatAddress(subject: MaskedSubject): string | null {
  // MaskedSubject doesn't carry address — return null
  // Address data would need to come from a separate field
  void subject
  return null
}

function LoadingSkeleton() {
  return (
    <div className="space-y-4">
      {[1, 2, 3].map((i) => (
        <div key={i} className="rounded-xl border p-6 animate-pulse">
          <div className="h-5 bg-muted rounded w-1/3 mb-4" />
          <div className="h-4 bg-muted rounded w-2/3" />
        </div>
      ))}
    </div>
  )
}
