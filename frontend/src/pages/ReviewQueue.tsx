import { useState } from "react"
import { Link } from "react-router-dom"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Inbox, ClipboardCheck } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"
import {
  getQueues,
  getQueue,
  assignTask,
  completeTask,
} from "@/api/client"
import type { ReviewTask, CompleteTaskBody } from "@/api/client"

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const QUEUE_TYPES = ["low_confidence", "escalation", "qc_sampling", "rra_review"] as const

const QUEUE_LABELS: Record<string, string> = {
  low_confidence: "Low Confidence",
  escalation: "Escalation",
  qc_sampling: "QC Sampling",
  rra_review: "RRA Review",
}

const QUEUE_ROLES: Record<string, string> = {
  low_confidence: "REVIEWER",
  escalation: "LEGAL_REVIEWER",
  qc_sampling: "QC_SAMPLER",
  rra_review: "REVIEWER",
}

const QUEUE_BADGE_STYLES: Record<string, string> = {
  low_confidence: "bg-yellow-100 text-yellow-800 border-yellow-300",
  escalation: "bg-red-100 text-red-800 border-red-300",
  qc_sampling: "bg-blue-100 text-blue-800 border-blue-300",
  rra_review: "bg-purple-100 text-purple-800 border-purple-300",
}

const ROLE_OPTIONS = ["REVIEWER", "LEGAL_REVIEWER", "QC_SAMPLER", "APPROVER"]

// ---------------------------------------------------------------------------
// ReviewQueue page — merged view of all 4 queues
// ---------------------------------------------------------------------------

export function ReviewQueue() {
  const [activeTab, setActiveTab] = useState<string>("all")
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const queryClient = useQueryClient()

  // Fetch queue counts
  const { data: queueCounts } = useQuery({
    queryKey: ["queues"],
    queryFn: getQueues,
    refetchInterval: 15_000,
  })

  // Fetch tasks for active queue (or all)
  const queuesToFetch = activeTab === "all" ? QUEUE_TYPES : [activeTab]
  const queries = queuesToFetch.map((qt) =>
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useQuery({
      queryKey: ["queue", qt],
      queryFn: () => getQueue(qt),
      refetchInterval: 15_000,
    }),
  )

  const allTasks: Array<ReviewTask & { queue_type_label: string; queue_type_key: string }> = []
  queries.forEach((q, i) => {
    const qt = queuesToFetch[i]
    for (const task of q.data ?? []) {
      allTasks.push({
        ...task,
        queue_type_label: QUEUE_LABELS[qt] ?? qt,
        queue_type_key: qt,
      })
    }
  })

  // Sort by created_at ascending (oldest first)
  allTasks.sort((a, b) => {
    const da = a.created_at ? new Date(a.created_at).getTime() : 0
    const db = b.created_at ? new Date(b.created_at).getTime() : 0
    return da - db
  })

  const isLoading = queries.some((q) => q.isLoading)
  const totalPending = queueCounts
    ? Object.values(queueCounts).reduce((s, v) => s + v, 0)
    : 0

  function handleTaskDone() {
    setExpandedId(null)
    queryClient.invalidateQueries({ queryKey: ["queue"] })
    queryClient.invalidateQueries({ queryKey: ["queues"] })
  }

  return (
    <div className="space-y-6">
      {/* Breadcrumb */}
      <div className="flex items-center gap-1 text-sm text-muted-foreground">
        <Link to="/" className="hover:text-foreground">Dashboard</Link>
        <span>&gt;</span>
        <span className="text-foreground font-medium">Review Queue</span>
      </div>

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <ClipboardCheck className="h-6 w-6 text-primary" />
          <h1 className="text-2xl font-bold">Review Queue</h1>
          {totalPending > 0 && (
            <Badge variant="outline" className="text-xs">
              {totalPending} pending
            </Badge>
          )}
        </div>
      </div>

      {/* Tab bar */}
      <div className="flex items-center gap-2 border-b pb-2">
        <button
          onClick={() => setActiveTab("all")}
          className={`rounded-t-md px-3 py-1.5 text-sm font-medium transition-colors ${
            activeTab === "all"
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:bg-accent"
          }`}
        >
          All ({totalPending})
        </button>
        {QUEUE_TYPES.map((qt) => {
          const count = queueCounts?.[qt] ?? 0
          return (
            <button
              key={qt}
              onClick={() => setActiveTab(qt)}
              className={`rounded-t-md px-3 py-1.5 text-sm font-medium transition-colors ${
                activeTab === qt
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-accent"
              }`}
            >
              {QUEUE_LABELS[qt]} ({count})
            </button>
          )
        })}
      </div>

      {/* Task list */}
      {isLoading ? (
        <p className="text-sm text-muted-foreground py-8 text-center">Loading tasks...</p>
      ) : allTasks.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
          <Inbox className="h-12 w-12 mb-3 opacity-40" />
          <p className="text-sm">
            {activeTab === "all"
              ? "All clear -- no pending review tasks"
              : `No pending tasks in ${QUEUE_LABELS[activeTab] ?? activeTab}`}
          </p>
        </div>
      ) : (
        <Card>
          <CardContent className="p-0">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/50">
                  <th className="px-4 py-2.5 text-left font-medium">Task ID</th>
                  <th className="px-4 py-2.5 text-left font-medium">Subject ID</th>
                  {activeTab === "all" && (
                    <th className="px-4 py-2.5 text-left font-medium">Queue</th>
                  )}
                  <th className="px-4 py-2.5 text-left font-medium">Status</th>
                  <th className="px-4 py-2.5 text-left font-medium">Created</th>
                  <th className="px-4 py-2.5 text-right font-medium">Action</th>
                </tr>
              </thead>
              <tbody>
                {allTasks.map((task) => {
                  const isExpanded = expandedId === task.review_task_id
                  const created = task.created_at
                    ? new Date(task.created_at).toLocaleDateString()
                    : "--"

                  return (
                    <TaskRow
                      key={task.review_task_id}
                      task={task}
                      queueType={task.queue_type_key}
                      queueLabel={task.queue_type_label}
                      showQueue={activeTab === "all"}
                      isExpanded={isExpanded}
                      created={created}
                      onToggle={() =>
                        setExpandedId(isExpanded ? null : task.review_task_id)
                      }
                      onDone={handleTaskDone}
                    />
                  )
                })}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Task row
// ---------------------------------------------------------------------------

function TaskRow({
  task,
  queueType,
  queueLabel,
  showQueue,
  isExpanded,
  created,
  onToggle,
  onDone,
}: {
  task: ReviewTask
  queueType: string
  queueLabel: string
  showQueue: boolean
  isExpanded: boolean
  created: string
  onToggle: () => void
  onDone: () => void
}) {
  const colSpan = showQueue ? 6 : 5

  return (
    <>
      <tr className="border-b last:border-0 hover:bg-accent/30">
        <td className="px-4 py-2.5 font-mono text-xs">
          {task.review_task_id.slice(0, 8)}...
        </td>
        <td className="px-4 py-2.5 font-mono text-xs">
          {task.subject_id ? `${task.subject_id.slice(0, 8)}...` : "--"}
        </td>
        {showQueue && (
          <td className="px-4 py-2.5">
            <Badge
              variant="outline"
              className={`text-xs ${QUEUE_BADGE_STYLES[queueType] ?? ""}`}
            >
              {queueLabel}
            </Badge>
          </td>
        )}
        <td className="px-4 py-2.5">
          <Badge variant="outline" className="text-xs">{task.status}</Badge>
        </td>
        <td className="px-4 py-2.5 text-xs text-muted-foreground">{created}</td>
        <td className="px-4 py-2.5 text-right">
          <button
            onClick={onToggle}
            className="text-xs font-medium text-primary hover:underline"
          >
            {isExpanded ? "Cancel" : "Take Task"}
          </button>
        </td>
      </tr>
      {isExpanded && (
        <tr>
          <td colSpan={colSpan} className="px-4 py-3">
            <TaskExpansion task={task} queueType={queueType} onDone={onDone} />
          </td>
        </tr>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Task expansion (assign + complete)
// ---------------------------------------------------------------------------

function TaskExpansion({
  task,
  queueType,
  onDone,
}: {
  task: ReviewTask
  queueType: string
  onDone: () => void
}) {
  const [step, setStep] = useState<"assign" | "complete">("assign")
  const [role, setRole] = useState(QUEUE_ROLES[queueType] ?? "REVIEWER")
  const [reviewerId, setReviewerId] = useState("")
  const [decision, setDecision] = useState("approved")
  const [rationale, setRationale] = useState("")
  const [regulatoryBasis, setRegulatoryBasis] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [toast, setToast] = useState<string | null>(null)

  async function handleAssign() {
    if (!reviewerId.trim()) return
    setLoading(true)
    setError(null)
    try {
      await assignTask(task.review_task_id, reviewerId, role)
      setStep("complete")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error"
      if (msg.includes("400")) setError("Your role cannot action this queue")
      else if (msg.includes("404")) setError("Task no longer available")
      else setError(msg)
    } finally {
      setLoading(false)
    }
  }

  async function handleComplete() {
    if (rationale.trim().length < 10) return
    setLoading(true)
    setError(null)
    try {
      const body: CompleteTaskBody = {
        reviewer_id: reviewerId,
        role,
        decision,
        rationale,
      }
      if (role === "LEGAL_REVIEWER" && regulatoryBasis.trim()) {
        body.regulatory_basis = regulatoryBasis
      }
      await completeTask(task.review_task_id, body)
      setToast("Decision recorded")
      setTimeout(onDone, 800)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error"
      if (msg.includes("400")) setError("Your role cannot action this queue")
      else if (msg.includes("404")) setError("Task no longer available")
      else setError(msg)
    } finally {
      setLoading(false)
    }
  }

  if (toast) {
    return (
      <div className="rounded-md bg-green-50 border border-green-200 px-4 py-3 text-sm text-green-800">
        {toast}
      </div>
    )
  }

  return (
    <div className="rounded-md border bg-muted/30 p-4 space-y-4">
      {error && (
        <div
          className={`rounded-md px-4 py-2 text-sm ${
            error.includes("no longer")
              ? "bg-yellow-50 border border-yellow-200 text-yellow-800"
              : "bg-red-50 border border-red-200 text-red-800"
          }`}
        >
          {error}
        </div>
      )}

      {step === "assign" && (
        <div className="space-y-3">
          <h4 className="text-sm font-semibold">Step 1 -- Assign</h4>
          <div className="flex gap-3 flex-wrap">
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="rounded-md border bg-background px-3 py-1.5 text-sm"
            >
              {ROLE_OPTIONS.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
            <input
              type="text"
              placeholder="your-id"
              value={reviewerId}
              onChange={(e) => setReviewerId(e.target.value)}
              className="rounded-md border bg-background px-3 py-1.5 text-sm w-48"
            />
            <button
              onClick={handleAssign}
              disabled={loading || !reviewerId.trim()}
              className="rounded-md bg-primary text-primary-foreground px-4 py-1.5 text-sm font-medium disabled:opacity-50"
            >
              {loading ? "Assigning..." : "Assign to Me"}
            </button>
          </div>
        </div>
      )}

      {step === "complete" && (
        <div className="space-y-3">
          <h4 className="text-sm font-semibold">Step 2 -- Complete</h4>

          <div className="flex gap-4">
            {["approved", "rejected", ...(queueType !== "escalation" ? ["escalated"] : [])].map(
              (d) => (
                <label key={d} className="flex items-center gap-1.5 text-sm">
                  <input
                    type="radio"
                    name="decision"
                    value={d}
                    checked={decision === d}
                    onChange={() => setDecision(d)}
                    className="accent-primary"
                  />
                  {d.charAt(0).toUpperCase() + d.slice(1)}
                </label>
              ),
            )}
          </div>

          <textarea
            placeholder="Describe your decision... (min 10 chars)"
            value={rationale}
            onChange={(e) => setRationale(e.target.value)}
            className="w-full rounded-md border bg-background px-3 py-2 text-sm min-h-[80px]"
          />
          {rationale.length > 0 && rationale.length < 10 && (
            <p className="text-xs text-destructive">
              Rationale must be at least 10 characters.
            </p>
          )}

          {role === "LEGAL_REVIEWER" && (
            <input
              type="text"
              placeholder="e.g. GDPR Art. 33"
              value={regulatoryBasis}
              onChange={(e) => setRegulatoryBasis(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-1.5 text-sm"
            />
          )}

          <button
            onClick={handleComplete}
            disabled={loading || rationale.trim().length < 10}
            className="rounded-md bg-primary text-primary-foreground px-4 py-1.5 text-sm font-medium disabled:opacity-50"
          >
            {loading ? "Submitting..." : "Submit Decision"}
          </button>
        </div>
      )}
    </div>
  )
}
