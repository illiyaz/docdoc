import { useState } from "react"
import { useParams, Link } from "react-router-dom"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Inbox } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import {
  getQueue,
  assignTask,
  completeTask,
} from "@/api/client"
import type { ReviewTask, CompleteTaskBody } from "@/api/client"

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

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

const ROLE_OPTIONS = ["REVIEWER", "LEGAL_REVIEWER", "QC_SAMPLER", "APPROVER"]

// ---------------------------------------------------------------------------
// Inline task expansion
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
          <h4 className="text-sm font-semibold">Step 1 — Assign</h4>
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
          <h4 className="text-sm font-semibold">Step 2 — Complete</h4>

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
              placeholder="e.g. GDPR Art. 33 — personal data breach"
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

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function QueueView() {
  const { type } = useParams<{ type: string }>()
  const queueType = type ?? ""
  const label = QUEUE_LABELS[queueType] ?? queueType
  const requiredRole = QUEUE_ROLES[queueType] ?? "REVIEWER"
  const queryClient = useQueryClient()

  const [expandedId, setExpandedId] = useState<string | null>(null)

  const { data: tasks, isLoading } = useQuery({
    queryKey: ["queue", queueType],
    queryFn: () => getQueue(queueType),
    refetchInterval: 15_000,
  })

  function handleTaskDone() {
    setExpandedId(null)
    queryClient.invalidateQueries({ queryKey: ["queue", queueType] })
    queryClient.invalidateQueries({ queryKey: ["queues"] })
  }

  return (
    <div className="space-y-6">
      {/* Breadcrumb */}
      <div className="flex items-center gap-1 text-sm text-muted-foreground">
        <Link to="/" className="hover:text-foreground">Dashboard</Link>
        <span>&gt;</span>
        <span className="text-foreground font-medium">{label}</span>
      </div>

      {/* Header */}
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold">{label} Queue</h1>
        <Badge variant="outline">Requires: {requiredRole}</Badge>
      </div>
      <p className="text-sm text-muted-foreground">
        {tasks?.length ?? 0} pending task{(tasks?.length ?? 0) !== 1 ? "s" : ""}
      </p>

      {/* Task list */}
      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading tasks...</p>
      ) : !tasks || tasks.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
          <Inbox className="h-12 w-12 mb-3 opacity-40" />
          <p className="text-sm">No pending tasks in this queue</p>
        </div>
      ) : (
        <div className="rounded-lg border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/50">
                <th className="px-4 py-2 text-left font-medium">Task ID</th>
                <th className="px-4 py-2 text-left font-medium">Subject ID</th>
                <th className="px-4 py-2 text-left font-medium">Created</th>
                <th className="px-4 py-2 text-left font-medium">Status</th>
                <th className="px-4 py-2 text-right font-medium">Action</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <TaskRow
                  key={task.review_task_id}
                  task={task}
                  queueType={queueType}
                  isExpanded={expandedId === task.review_task_id}
                  onToggle={() =>
                    setExpandedId(
                      expandedId === task.review_task_id ? null : task.review_task_id,
                    )
                  }
                  onDone={handleTaskDone}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Task row + expansion
// ---------------------------------------------------------------------------

function TaskRow({
  task,
  queueType,
  isExpanded,
  onToggle,
  onDone,
}: {
  task: ReviewTask
  queueType: string
  isExpanded: boolean
  onToggle: () => void
  onDone: () => void
}) {
  const created = task.created_at
    ? new Date(task.created_at).toLocaleDateString()
    : "—"

  return (
    <>
      <tr className="border-b last:border-0">
        <td className="px-4 py-2 font-mono text-xs">
          {task.review_task_id.slice(0, 8)}&hellip;
        </td>
        <td className="px-4 py-2 font-mono text-xs">
          {task.subject_id ? `${task.subject_id.slice(0, 8)}\u2026` : "—"}
        </td>
        <td className="px-4 py-2">{created}</td>
        <td className="px-4 py-2">
          <Badge variant="outline" className="text-xs">{task.status}</Badge>
        </td>
        <td className="px-4 py-2 text-right">
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
          <td colSpan={5} className="px-4 py-3">
            <TaskExpansion task={task} queueType={queueType} onDone={onDone} />
          </td>
        </tr>
      )}
    </>
  )
}
