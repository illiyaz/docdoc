const BASE_URL = import.meta.env.VITE_API_URL ?? ""

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
  source_directory?: string
  upload_id?: string
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

export interface UploadFileInfo {
  name: string
  size_bytes: number
  extension: string
}

export interface UploadResult {
  upload_id: string
  directory: string
  file_count: number
  total_size_bytes: number
  files: UploadFileInfo[]
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

/**
 * Upload files to the server. Uses XMLHttpRequest for upload progress support.
 */
export function uploadFiles(
  files: File[],
  onProgress?: (percent: number) => void,
): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    for (const f of files) {
      // Use the file's name explicitly to ensure correct filename on the server
      form.append("files", f, f.name)
    }

    const xhr = new XMLHttpRequest()
    xhr.open("POST", `${BASE_URL}/jobs/upload`)
    xhr.timeout = 600_000 // 10 min for large uploads

    if (onProgress) {
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
          onProgress(Math.round((e.loaded / e.total) * 100))
        }
      })
    }

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as UploadResult)
        } catch {
          reject(new Error("Upload failed: invalid server response"))
        }
      } else {
        let detail = xhr.responseText
        try {
          const json = JSON.parse(detail)
          detail = json.detail ?? detail
        } catch { /* use raw text */ }
        reject(new Error(`Upload failed (${xhr.status}): ${detail}`))
      }
    })

    xhr.addEventListener("error", () => {
      reject(new Error("Upload failed: could not connect to server"))
    })

    xhr.addEventListener("timeout", () => {
      reject(new Error("Upload failed: request timed out"))
    })

    xhr.addEventListener("abort", () => {
      reject(new Error("Upload cancelled"))
    })

    xhr.send(form)
  })
}

// ---------------------------------------------------------------------------
// Streaming job execution (SSE)
// ---------------------------------------------------------------------------

export interface PipelineProgress {
  stage: string
  status?: string
  message: string
  detail?: Record<string, unknown>
  result?: JobResult
}

/**
 * Submit a job with real-time SSE progress updates.
 * Uses POST /jobs/run which streams pipeline stage events.
 */
export async function submitJobStreaming(
  body: SubmitJobBody,
  onProgress: (event: PipelineProgress) => void,
): Promise<JobResult> {
  const res = await fetch(`${BASE_URL}/jobs/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })

  if (!res.ok) {
    const text = await res.text()
    throw new Error(`API ${res.status}: ${text}`)
  }

  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  let finalResult: JobResult | null = null

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })

    // SSE lines are separated by double newlines
    const parts = buffer.split("\n\n")
    buffer = parts.pop()! // keep the incomplete chunk

    for (const part of parts) {
      for (const line of part.split("\n")) {
        if (line.startsWith("data: ")) {
          try {
            const data = JSON.parse(line.slice(6)) as PipelineProgress
            onProgress(data)
            if (data.stage === "complete" && data.result) {
              finalResult = data.result
            }
            if (data.stage === "error") {
              throw new Error(data.message)
            }
          } catch (e) {
            if (e instanceof Error && e.message !== "Unexpected end of JSON input") {
              throw e
            }
          }
        }
      }
    }
  }

  if (!finalResult) {
    throw new Error("Pipeline ended without producing a result")
  }

  return finalResult
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

// ---------------------------------------------------------------------------
// Base Protocols (from YAML files)
// ---------------------------------------------------------------------------

export interface BaseProtocol {
  protocol_id: string
  name: string
  jurisdiction: string
  regulatory_framework: string
  notification_deadline_days: number
}

export function getBaseProtocols(): Promise<BaseProtocol[]> {
  return api("/protocols/base")
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

export interface ProjectSummary {
  id: string
  name: string
  description: string | null
  status: string
  created_by: string | null
  created_at: string | null
  updated_at: string | null
}

export interface ProjectDetail extends ProjectSummary {
  protocols: ProtocolConfigSummary[]
}

export interface CreateProjectBody {
  name: string
  description?: string | null
  created_by?: string | null
}

export interface UpdateProjectBody {
  name?: string | null
  description?: string | null
  status?: string | null
}

export interface ProtocolConfigSummary {
  id: string
  project_id: string
  base_protocol_id: string | null
  name: string
  config_json: Record<string, unknown>
  status: string
  created_at: string | null
  updated_at: string | null
}

export interface CreateProtocolConfigBody {
  name: string
  base_protocol_id?: string | null
  config_json: Record<string, unknown>
}

export interface UpdateProtocolConfigBody {
  name?: string | null
  config_json?: Record<string, unknown> | null
  status?: string | null
}

export interface CatalogSummary {
  project_id: string
  total_documents: number
  auto_processable: number
  manual_review: number
  by_file_type: Record<string, number>
  by_structure_class: Record<string, number>
}

export interface DensitySummaryItem {
  id: string
  document_id: string | null
  total_entities: number
  by_category: Record<string, number> | null
  by_type: Record<string, number> | null
  confidence: string | null
  confidence_notes: string | null
  created_at: string | null
}

export interface DensityResponse {
  project_id: string
  project_summary: DensitySummaryItem | null
  document_summaries: DensitySummaryItem[]
}

export interface ExportJobSummary {
  id: string
  project_id: string
  protocol_config_id: string | null
  export_type: string | null
  status: string
  file_path: string | null
  row_count: number | null
  filters_json: Record<string, unknown> | null
  created_at: string | null
  completed_at: string | null
}

export interface CreateExportBody {
  protocol_config_id?: string | null
  filters?: Record<string, unknown> | null
}

export function createProject(body: CreateProjectBody): Promise<ProjectSummary> {
  return api("/projects", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export function listProjects(): Promise<ProjectSummary[]> {
  return api("/projects")
}

export function getProject(id: string): Promise<ProjectDetail> {
  return api(`/projects/${id}`)
}

export function updateProject(id: string, body: UpdateProjectBody): Promise<ProjectSummary> {
  return api(`/projects/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  })
}

export function getCatalogSummary(projectId: string): Promise<CatalogSummary> {
  return api(`/projects/${projectId}/catalog-summary`)
}

export function getDensity(projectId: string): Promise<DensityResponse> {
  return api(`/projects/${projectId}/density`)
}

// ---------------------------------------------------------------------------
// Protocol Configs
// ---------------------------------------------------------------------------

export function createProtocolConfig(
  projectId: string,
  body: CreateProtocolConfigBody,
): Promise<ProtocolConfigSummary> {
  return api(`/projects/${projectId}/protocols`, {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export function listProtocolConfigs(projectId: string): Promise<ProtocolConfigSummary[]> {
  return api(`/projects/${projectId}/protocols`)
}

export function getProtocolConfig(
  projectId: string,
  configId: string,
): Promise<ProtocolConfigSummary> {
  return api(`/projects/${projectId}/protocols/${configId}`)
}

export function updateProtocolConfig(
  projectId: string,
  configId: string,
  body: UpdateProtocolConfigBody,
): Promise<ProtocolConfigSummary> {
  return api(`/projects/${projectId}/protocols/${configId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  })
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

export function createExport(
  projectId: string,
  body: CreateExportBody,
): Promise<ExportJobSummary> {
  return api(`/projects/${projectId}/exports`, {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export function listExports(projectId: string): Promise<ExportJobSummary[]> {
  return api(`/projects/${projectId}/exports`)
}

export function getExport(projectId: string, exportId: string): Promise<ExportJobSummary> {
  return api(`/projects/${projectId}/exports/${exportId}`)
}

export function getExportDownloadUrl(projectId: string, exportId: string): string {
  return `${BASE_URL}/projects/${projectId}/exports/${exportId}/download`
}

// ---------------------------------------------------------------------------
// Diagnostic
// ---------------------------------------------------------------------------

export interface DiagnosticPIIHit {
  entity_type: string
  masked_value: string
  confidence: number
  extraction_layer: string
  pattern_used: string
  context_snippet: string
}

export interface DiagnosticPage {
  page_number: number
  page_type: string
  blocks_extracted: number
  skipped_by_onset: boolean
  ocr_used: boolean
  pii_hits: DiagnosticPIIHit[]
}

export interface DiagnosticSummary {
  total_pii_hits: number
  by_entity_type: Record<string, number>
  layer_distribution: { layer_1: number; layer_2: number; layer_3: number }
  low_confidence_hits: number
  pages_skipped_by_onset: number
  ocr_pages: number
}

export interface DiagnosticReport {
  file_name: string
  file_type: string
  total_pages: number
  onset_page: number | null
  pages: DiagnosticPage[]
  summary: DiagnosticSummary
}

export async function runDiagnostic(
  file: File,
  protocolId: string,
): Promise<DiagnosticReport> {
  const form = new FormData()
  form.append("file", file)
  form.append("protocol_id", protocolId)
  const res = await fetch(`${BASE_URL}/diagnostic/file`, {
    method: "POST",
    body: form,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`API ${res.status}: ${text}`)
  }
  return res.json() as Promise<DiagnosticReport>
}
