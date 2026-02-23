const BASE_URL = import.meta.env.VITE_API_URL ?? "http://localhost:3848"

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ReviewTask {
  review_task_id: string
  queue_type: string
  subject_id: string
  assigned_to: string | null
  status: string
  required_role: string
  created_at: string
}

export interface CompleteTaskBody {
  reviewer_id: string
  role: string
  decision: string
  rationale: string
  regulatory_basis?: string | null
}

export interface SubmitJobBody {
  protocol_id: string
  source_directory: string
  job_id?: string
}

export interface JobResult {
  job_id: string
  status: string
  subjects_found: number
  notification_required: number
}

export interface JobStatus {
  job_id: string
  protocol_id: string
  status: string
  subject_count: number
  created_at: string | null
}

export interface MaskedSubject {
  subject_id: string
  canonical_name: string
  canonical_email: string
  canonical_phone: string
  pii_types_found: string[]
  notification_required: boolean
  review_status: string
}

export interface AuditEvent {
  event_type: string
  actor: string
  decision: string | null
  timestamp: string
  regulatory_basis: string | null
}

export interface Protocol {
  protocol_id: string
  name: string
  jurisdiction: string
  regulatory_framework: string
  notification_deadline_days: number
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`API ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// Queues
// ---------------------------------------------------------------------------

export function getQueues(): Promise<Record<string, number>> {
  return api("/review/queues")
}

export function getQueue(type: string): Promise<ReviewTask[]> {
  return api(`/review/queues/${type}`)
}

// ---------------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------------

export function assignTask(id: string, reviewer_id: string, role: string) {
  return api<ReviewTask>(`/review/tasks/${id}/assign`, {
    method: "POST",
    body: JSON.stringify({ reviewer_id, role }),
  })
}

export function completeTask(id: string, body: CompleteTaskBody) {
  return api<ReviewTask>(`/review/tasks/${id}/complete`, {
    method: "POST",
    body: JSON.stringify(body),
  })
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

export function submitJob(body: SubmitJobBody): Promise<JobResult> {
  return api("/jobs", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export function getJob(jobId: string): Promise<JobStatus> {
  return api(`/jobs/${jobId}`)
}

export function getJobResults(jobId: string): Promise<MaskedSubject[]> {
  return api(`/jobs/${jobId}/results`)
}

// ---------------------------------------------------------------------------
// Audit
// ---------------------------------------------------------------------------

export function getAuditHistory(subjectId: string): Promise<AuditEvent[]> {
  return api(`/audit/${subjectId}/history`)
}

export function getRecentAudit(): Promise<AuditEvent[]> {
  return api("/audit/recent")
}

// ---------------------------------------------------------------------------
// Protocols
// ---------------------------------------------------------------------------

export function getProtocols(): Promise<Protocol[]> {
  return api("/jobs/protocols")
}
