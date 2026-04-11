import { useState, useContext, useRef, useCallback } from "react"
import { useQuery } from "@tanstack/react-query"
import { useNavigate, Link } from "react-router-dom"
import {
  Loader2, CheckCircle, Upload, FolderOpen, Server,
  X, FileText, Circle, AlertCircle, Clock, Play, XCircle,
  ChevronDown, ChevronUp,
} from "lucide-react"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { getProtocols, uploadFiles, submitJobStreaming, listProjects, getRecentJobs } from "@/api/client"
import type { JobResult, UploadResult, PipelineProgress, JobSummary } from "@/api/client"
import { JobIdSetterContext } from "@/App"

const SUPPORTED_EXTENSIONS = new Set([
  ".pdf", ".xlsx", ".xls", ".docx", ".csv",
  ".html", ".htm", ".xml", ".eml", ".msg",
  ".parquet", ".avro",
])

function isSupported(name: string): boolean {
  const dot = name.lastIndexOf(".")
  if (dot === -1) return false
  return SUPPORTED_EXTENSIONS.has(name.slice(dot).toLowerCase())
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// ---------------------------------------------------------------------------
// Pipeline progress stepper
// ---------------------------------------------------------------------------

const PIPELINE_STAGES = [
  { id: "discovery", label: "Discovery" },
  { id: "cataloging", label: "Cataloging" },
  { id: "detection", label: "PII Detection" },
  { id: "extraction", label: "PII Extraction" },
  { id: "normalization", label: "Normalization" },
  { id: "resolution", label: "Entity Resolution" },
  { id: "qa", label: "Quality Assurance" },
  { id: "notification", label: "Notification" },
] as const

type StageStatus = "pending" | "running" | "complete" | "error"

interface StageState {
  status: StageStatus
  message: string
}

function PipelineStepper({ stages }: { stages: Record<string, StageState> }) {
  return (
    <div className="space-y-1">
      {PIPELINE_STAGES.map((stage) => {
        const state = stages[stage.id] ?? { status: "pending", message: "" }
        return (
          <div key={stage.id} className="flex items-start gap-3 py-1.5">
            <div className="mt-0.5 shrink-0">
              {state.status === "complete" && (
                <CheckCircle className="h-4 w-4 text-green-600" />
              )}
              {state.status === "running" && (
                <Loader2 className="h-4 w-4 text-primary animate-spin" />
              )}
              {state.status === "error" && (
                <AlertCircle className="h-4 w-4 text-red-500" />
              )}
              {state.status === "pending" && (
                <Circle className="h-4 w-4 text-muted-foreground/40" />
              )}
            </div>
            <div className="min-w-0">
              <p className={`text-sm font-medium leading-tight ${
                state.status === "pending" ? "text-muted-foreground/50" : ""
              }`}>
                {stage.label}
              </p>
              {state.message && state.status !== "pending" && (
                <p className="text-xs text-muted-foreground mt-0.5 truncate">
                  {state.message}
                </p>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Job List component (C1)
// ---------------------------------------------------------------------------

type StatusFilter = "all" | "running" | "completed" | "failed" | "analyze_complete"

function statusBadge(status: string) {
  const s = status.toLowerCase()
  if (s === "running" || s === "extracting")
    return <span className="inline-flex items-center gap-1 text-xs font-medium text-blue-700 bg-blue-50 rounded-full px-2 py-0.5"><Loader2 className="h-3 w-3 animate-spin" />Running</span>
  if (s === "completed" || s === "complete")
    return <span className="inline-flex items-center gap-1 text-xs font-medium text-green-700 bg-green-50 rounded-full px-2 py-0.5"><CheckCircle className="h-3 w-3" />Complete</span>
  if (s === "failed" || s === "error")
    return <span className="inline-flex items-center gap-1 text-xs font-medium text-red-700 bg-red-50 rounded-full px-2 py-0.5"><XCircle className="h-3 w-3" />Failed</span>
  if (s === "analyze_complete" || s === "awaiting_review" || s === "analyzed")
    return <span className="inline-flex items-center gap-1 text-xs font-medium text-amber-700 bg-amber-50 rounded-full px-2 py-0.5"><Clock className="h-3 w-3" />Ready to Extract</span>
  if (s === "analyzing")
    return <span className="inline-flex items-center gap-1 text-xs font-medium text-purple-700 bg-purple-50 rounded-full px-2 py-0.5"><Loader2 className="h-3 w-3 animate-spin" />Analyzing</span>
  return <span className="text-xs text-muted-foreground">{status}</span>
}

function timeAgo(dateStr: string | null): string {
  if (!dateStr) return ""
  const d = new Date(dateStr)
  const now = Date.now()
  const diffMs = now - d.getTime()
  if (diffMs < 0) return "just now"
  const mins = Math.floor(diffMs / 60000)
  if (mins < 1) return "just now"
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}

function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return ""
  if (seconds < 60) return `${Math.round(seconds)}s`
  const mins = Math.floor(seconds / 60)
  const secs = Math.round(seconds % 60)
  if (mins < 60) return `${mins}m ${secs}s`
  const hrs = Math.floor(mins / 60)
  return `${hrs}h ${mins % 60}m`
}

function JobList({ onJobSelect, onGoToProject }: { onJobSelect: (jobId: string) => void; onGoToProject?: (projectId: string) => void }) {
  const [filter, setFilter] = useState<StatusFilter>("all")
  const [expanded, setExpanded] = useState(true)

  const { data: jobs, isLoading } = useQuery({
    queryKey: ["recentJobs"],
    queryFn: () => getRecentJobs(false, 50),
    refetchInterval: 5000, // Poll every 5s for live updates
  })

  const filtered = (jobs ?? []).filter((j) => {
    if (filter === "all") return true
    const s = j.status.toLowerCase()
    if (filter === "running") return s === "running" || s === "extracting" || s === "analyzing"
    if (filter === "completed") return s === "completed" || s === "complete"
    if (filter === "failed") return s === "failed" || s === "error"
    if (filter === "analyze_complete") return s === "analyze_complete" || s === "awaiting_review" || s === "analyzed"
    return true
  })

  const counts = {
    all: (jobs ?? []).length,
    running: (jobs ?? []).filter((j) => ["running", "extracting", "analyzing"].includes(j.status.toLowerCase())).length,
    completed: (jobs ?? []).filter((j) => ["completed", "complete"].includes(j.status.toLowerCase())).length,
    failed: (jobs ?? []).filter((j) => ["failed", "error"].includes(j.status.toLowerCase())).length,
    analyze_complete: (jobs ?? []).filter((j) => ["analyze_complete", "awaiting_review", "analyzed"].includes(j.status.toLowerCase())).length,
  }

  return (
    <Card className="mb-6">
      <CardHeader className="pb-3 cursor-pointer" onClick={() => setExpanded(!expanded)}>
        <div className="flex items-center justify-between">
          <CardTitle className="text-base flex items-center gap-2">
            Recent Jobs
            {counts.running > 0 && (
              <span className="inline-flex items-center gap-1 text-xs font-medium text-blue-700 bg-blue-50 rounded-full px-2 py-0.5">
                <Loader2 className="h-3 w-3 animate-spin" />{counts.running} running
              </span>
            )}
          </CardTitle>
          {expanded ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
        </div>
      </CardHeader>
      {expanded && (
        <CardContent className="pt-0">
          {/* Filter tabs */}
          <div className="flex gap-1 mb-3 flex-wrap">
            {(["all", "running", "analyze_complete", "completed", "failed"] as StatusFilter[]).map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`text-xs px-2.5 py-1 rounded-full font-medium transition-colors ${
                  filter === f
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted text-muted-foreground hover:bg-muted/80"
                }`}
              >
                {f === "all" ? "All" : f === "analyze_complete" ? "Review" : f.charAt(0).toUpperCase() + f.slice(1)}
                {counts[f] > 0 && <span className="ml-1 opacity-75">({counts[f]})</span>}
              </button>
            ))}
          </div>

          {isLoading ? (
            <div className="flex items-center justify-center py-6 text-muted-foreground text-sm">
              <Loader2 className="h-4 w-4 animate-spin mr-2" /> Loading jobs...
            </div>
          ) : filtered.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-4">
              {filter === "all" ? "No jobs yet. Submit a dataset below to get started." : `No ${filter} jobs.`}
            </p>
          ) : (
            <div className="max-h-64 overflow-y-auto rounded-md border divide-y">
              {filtered.map((job) => {
                const isAnalyzed = job.status.toLowerCase() === "analyzed"
                const hasProject = !!job.project_id
                return (
                  <div
                    key={job.id}
                    className="w-full flex items-center justify-between px-3 py-2.5 text-left hover:bg-accent/50 transition-colors cursor-pointer"
                    onClick={() => {
                      if (isAnalyzed && hasProject && onGoToProject) {
                        onGoToProject(job.project_id!)
                      } else {
                        onJobSelect(job.id)
                      }
                    }}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium truncate">
                          {job.first_file_name ?? job.source_path ?? job.id.slice(0, 8)}
                        </span>
                        {statusBadge(job.status)}
                      </div>
                      <div className="flex items-center gap-3 mt-0.5 text-xs text-muted-foreground">
                        <span>{job.document_count} doc{job.document_count !== 1 ? "s" : ""}</span>
                        {job.duration_seconds != null && <span>{formatDuration(job.duration_seconds)}</span>}
                        <span>{timeAgo(job.started_at ?? job.created_at)}</span>
                      </div>
                    </div>
                    {isAnalyzed && hasProject ? (
                      <span className="text-xs font-medium text-amber-700 bg-amber-50 rounded px-2 py-1 shrink-0 ml-2">
                        Extract
                      </span>
                    ) : (
                      <Play className="h-3.5 w-3.5 text-muted-foreground shrink-0 ml-2" />
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </CardContent>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

type Tab = "upload" | "server"
type Phase = "idle" | "files_selected" | "uploading" | "uploaded" | "running" | "complete"

export function JobSubmit() {
  const navigate = useNavigate()
  const setJobId = useContext(JobIdSetterContext)

  // Project selection
  const [selectedProjectId, setSelectedProjectId] = useState("")
  const { data: projects } = useQuery({
    queryKey: ["projects"],
    queryFn: listProjects,
  })

  // Shared state
  const [protocolId, setProtocolId] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<JobResult | null>(null)

  // Tab state
  const [tab, setTab] = useState<Tab>("upload")

  // Upload tab state
  const [selectedFiles, setSelectedFiles] = useState<File[]>([])
  const [uploadProgress, setUploadProgress] = useState(0)
  const [uploadResult, setUploadResult] = useState<UploadResult | null>(null)
  const [phase, setPhase] = useState<Phase>("idle")

  // Pipeline progress state
  const [stageStates, setStageStates] = useState<Record<string, StageState>>({})

  // Server path tab state
  const [sourceDir, setSourceDir] = useState("")

  // Refs for file inputs
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)

  const { data: protocols } = useQuery({
    queryKey: ["protocols"],
    queryFn: getProtocols,
  })

  const selectedProtocol = protocols?.find((p) => p.protocol_id === protocolId)

  // ---- File selection ----

  const addFiles = useCallback((newFiles: FileList | File[]) => {
    const arr = Array.from(newFiles)
    setSelectedFiles((prev) => [...prev, ...arr])
    setPhase("files_selected")
    setError(null)
  }, [])

  function removeFile(index: number) {
    setSelectedFiles((prev) => {
      const next = prev.filter((_, i) => i !== index)
      if (next.length === 0) setPhase("idle")
      return next
    })
  }

  function clearFiles() {
    setSelectedFiles([])
    setUploadResult(null)
    setPhase("idle")
    setError(null)
  }

  // ---- Drag & drop ----

  function handleDragOver(e: React.DragEvent) {
    e.preventDefault()
    e.stopPropagation()
  }

  async function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    e.stopPropagation()
    const items = e.dataTransfer.items
    if (!items || items.length === 0) {
      addFiles(e.dataTransfer.files)
      return
    }

    const allFiles: File[] = []
    const entries: FileSystemEntry[] = []
    for (let i = 0; i < items.length; i++) {
      const entry = items[i].webkitGetAsEntry?.()
      if (entry) entries.push(entry)
    }

    if (entries.length === 0) {
      addFiles(e.dataTransfer.files)
      return
    }

    async function readAllEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
      const all: FileSystemEntry[] = []
      let batch: FileSystemEntry[]
      do {
        batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
          reader.readEntries(resolve, reject),
        )
        all.push(...batch)
      } while (batch.length > 0)
      return all
    }

    async function readEntry(entry: FileSystemEntry): Promise<void> {
      if (entry.isFile) {
        try {
          const file = await new Promise<File>((resolve, reject) =>
            (entry as FileSystemFileEntry).file(resolve, reject),
          )
          allFiles.push(file)
        } catch {
          // Skip files that can't be read
        }
      } else if (entry.isDirectory) {
        const dirReader = (entry as FileSystemDirectoryEntry).createReader()
        const children = await readAllEntries(dirReader)
        for (const child of children) {
          await readEntry(child)
        }
      }
    }

    for (const entry of entries) {
      await readEntry(entry)
    }

    if (allFiles.length > 0) {
      addFiles(allFiles)
    }
  }

  // ---- Upload ----

  async function handleUpload() {
    if (selectedFiles.length === 0) return
    setPhase("uploading")
    setUploadProgress(0)
    setError(null)

    try {
      const result = await uploadFiles(selectedFiles, setUploadProgress)
      setUploadResult(result)
      setPhase("uploaded")
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed")
      setPhase("files_selected")
    }
  }

  // ---- Pipeline progress handler ----

  function handlePipelineProgress(event: PipelineProgress) {
    if (event.stage === "complete" || event.stage === "error") return
    setStageStates((prev) => ({
      ...prev,
      [event.stage]: {
        status: (event.status ?? "running") as StageStatus,
        message: event.message,
      },
    }))
  }

  // ---- Run pipeline ----

  async function handleRunPipeline() {
    if (!protocolId) return
    if (!selectedProjectId) {
      setError("Please select a project before running the pipeline")
      return
    }
    setError(null)
    setStageStates({})
    setPhase("running")

    try {
      const baseBody = tab === "upload" && uploadResult
        ? { protocol_id: protocolId, upload_id: uploadResult.upload_id }
        : { protocol_id: protocolId, source_directory: sourceDir }

      const body = { ...baseBody, project_id: selectedProjectId, pipeline_mode: "two_phase" }

      const res = await submitJobStreaming(body, handlePipelineProgress)
      setResult(res)
      setJobId(res.job_id)
      setPhase("complete")

      // C4: Auto-navigate to project detail after 2s so user sees results
      if (selectedProjectId) {
        setTimeout(() => navigate(`/projects/${selectedProjectId}`), 2000)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Job submission failed")
      // Mark current running stage as error
      setStageStates((prev) => {
        const next = { ...prev }
        for (const key of Object.keys(next)) {
          if (next[key].status === "running") {
            next[key] = { ...next[key], status: "error" }
          }
        }
        return next
      })
      if (tab === "upload") {
        setPhase("uploaded")
      } else {
        setPhase("idle")
      }
    }
  }

  // ---- Reset ----

  function handleReset() {
    setResult(null)
    setProtocolId("")
    setSelectedProjectId("")
    setSourceDir("")
    setSelectedFiles([])
    setUploadResult(null)
    setPhase("idle")
    setError(null)
    setStageStates({})
  }

  // ---- Derived ----

  const supportedCount = selectedFiles.filter((f) => isSupported(f.name)).length
  const totalSize = selectedFiles.reduce((sum, f) => sum + f.size, 0)
  const isUploading = phase === "uploading"
  const isRunning = phase === "running"
  const canRunUploadTab = tab === "upload" && phase === "uploaded" && protocolId !== "" && selectedProjectId !== ""
  const canRunServerTab = tab === "server" && sourceDir.trim() !== "" && protocolId !== "" && selectedProjectId !== "" && !isRunning

  // ---- Result view ----

  if (result) {
    return (
      <div className="max-w-lg mx-auto mt-8">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <CheckCircle className="h-5 w-5 text-green-600" />
              Pipeline Complete
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
              <div>
                <p className="text-muted-foreground">Records extracted</p>
                <p className="text-lg font-bold">{(result.total_records ?? 0).toLocaleString()}</p>
              </div>
              <div>
                <p className="text-muted-foreground">Unique subjects</p>
                <p className="text-lg font-bold">{result.subjects_found}</p>
              </div>
              <div>
                <p className="text-muted-foreground">Duplicates removed</p>
                <p className="text-lg font-bold">{(result.duplicates_removed ?? 0).toLocaleString()}</p>
              </div>
              <div>
                <p className="text-muted-foreground">Notification required</p>
                <p className="text-lg font-bold">{result.notification_required}</p>
              </div>
            </div>
            {(result.flagged_for_review ?? 0) > 0 && (
              <div className="rounded-md bg-amber-50 border border-amber-200 px-3 py-2 text-sm text-amber-800">
                {result.flagged_for_review} subject(s) flagged for human review (low merge confidence)
              </div>
            )}
            {selectedProtocol && (
              <p className="text-sm text-muted-foreground">
                Protocol applied: {selectedProtocol.name}
              </p>
            )}
            {selectedProjectId && (
              <div className="rounded-md bg-blue-50 border border-blue-200 px-4 py-2 text-sm text-blue-800">
                Job linked to project.{" "}
                <Link
                  to={`/projects/${selectedProjectId}`}
                  className="font-medium underline hover:text-blue-600"
                >
                  View Project Jobs tab &rarr;
                </Link>
              </div>
            )}
            <div className="flex gap-3 pt-2">
              <button
                onClick={() => navigate("/review")}
                className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium"
              >
                View Review Queue &rarr;
              </button>
              {selectedProjectId && (
                <button
                  onClick={() => navigate(`/projects/${selectedProjectId}`)}
                  className="rounded-md border px-4 py-2 text-sm font-medium hover:bg-accent"
                >
                  Go to Project
                </button>
              )}
              <button
                onClick={handleReset}
                className="rounded-md border px-4 py-2 text-sm font-medium hover:bg-accent"
              >
                Submit Another
              </button>
            </div>
          </CardContent>
        </Card>
      </div>
    )
  }

  // ---- Main form ----

  // Job list navigation
  const handleJobSelect = useCallback((jobId: string) => {
    setJobId?.(jobId)
    navigate(`/review`)
  }, [navigate, setJobId])

  const handleGoToProject = useCallback((projectId: string) => {
    navigate(`/projects/${projectId}?tab=jobs`)
  }, [navigate])

  return (
    <div className="max-w-2xl mx-auto mt-8">
      <JobList onJobSelect={handleJobSelect} onGoToProject={handleGoToProject} />
      <Card>
        <CardHeader>
          <CardTitle>Submit Breach Dataset for Analysis</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          {error && (
            <div className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-800">
              {error}
            </div>
          )}

          {/* Pipeline progress — shown when running */}
          {isRunning && (
            <div className="rounded-md border bg-muted/30 px-4 py-4">
              <p className="text-sm font-medium mb-3">Pipeline Progress</p>
              <PipelineStepper stages={stageStates} />
            </div>
          )}

          {/* Hide form controls while pipeline is running */}
          {!isRunning && (
            <>
              {/* Tab switcher */}
              <div className="flex border-b">
                <button
                  onClick={() => setTab("upload")}
                  className={`flex items-center gap-2 px-4 py-2 text-sm font-medium border-b-2 -mb-px ${
                    tab === "upload"
                      ? "border-primary text-primary"
                      : "border-transparent text-muted-foreground hover:text-foreground"
                  }`}
                >
                  <Upload className="h-4 w-4" />
                  Upload Files
                </button>
                <button
                  onClick={() => setTab("server")}
                  className={`flex items-center gap-2 px-4 py-2 text-sm font-medium border-b-2 -mb-px ${
                    tab === "server"
                      ? "border-primary text-primary"
                      : "border-transparent text-muted-foreground hover:text-foreground"
                  }`}
                >
                  <Server className="h-4 w-4" />
                  Server Path
                </button>
              </div>

              {/* Upload tab */}
              {tab === "upload" && (
                <div className="space-y-4">
                  {/* Drop zone */}
                  {phase !== "uploaded" && (
                    <div
                      onDragOver={handleDragOver}
                      onDrop={handleDrop}
                      className="border-2 border-dashed rounded-lg p-8 text-center hover:border-primary/50 transition-colors"
                    >
                      <FolderOpen className="h-10 w-10 mx-auto text-muted-foreground mb-3" />
                      <p className="text-sm text-muted-foreground mb-3">
                        Drag & drop files or a folder here
                      </p>
                      <div className="flex gap-3 justify-center">
                        <button
                          type="button"
                          onClick={() => fileInputRef.current?.click()}
                          disabled={isUploading}
                          className="rounded-md border px-3 py-1.5 text-sm font-medium hover:bg-accent"
                        >
                          Select Files
                        </button>
                        <button
                          type="button"
                          onClick={() => folderInputRef.current?.click()}
                          disabled={isUploading}
                          className="rounded-md border px-3 py-1.5 text-sm font-medium hover:bg-accent"
                        >
                          Select Folder
                        </button>
                      </div>
                      <input
                        ref={fileInputRef}
                        type="file"
                        multiple
                        className="hidden"
                        onChange={(e) => e.target.files && addFiles(e.target.files)}
                      />
                      <input
                        ref={folderInputRef}
                        type="file"
                        // @ts-expect-error webkitdirectory is not in React types
                        webkitdirectory=""
                        className="hidden"
                        onChange={(e) => e.target.files && addFiles(e.target.files)}
                      />
                    </div>
                  )}

                  {/* File list */}
                  {selectedFiles.length > 0 && phase !== "uploaded" && (
                    <div className="space-y-2">
                      <div className="flex items-center justify-between">
                        <p className="text-sm font-medium">
                          {selectedFiles.length} file{selectedFiles.length !== 1 ? "s" : ""} selected
                          ({supportedCount} supported) — {formatSize(totalSize)}
                        </p>
                        <button
                          onClick={clearFiles}
                          disabled={isUploading}
                          className="text-xs text-muted-foreground hover:text-foreground"
                        >
                          Clear all
                        </button>
                      </div>
                      <div className="max-h-48 overflow-y-auto rounded-md border divide-y">
                        {selectedFiles.map((f, i) => {
                          const supported = isSupported(f.name)
                          return (
                            <div
                              key={`${f.name}-${i}`}
                              className={`flex items-center justify-between px-3 py-1.5 text-sm ${
                                supported ? "" : "opacity-40"
                              }`}
                            >
                              <div className="flex items-center gap-2 min-w-0">
                                <FileText className="h-3.5 w-3.5 shrink-0" />
                                <span className="truncate">{f.name}</span>
                                <span className="text-xs text-muted-foreground shrink-0">
                                  {formatSize(f.size)}
                                </span>
                                {!supported && (
                                  <span className="text-xs text-muted-foreground shrink-0">
                                    (skipped)
                                  </span>
                                )}
                              </div>
                              <button
                                onClick={() => removeFile(i)}
                                disabled={isUploading}
                                className="text-muted-foreground hover:text-foreground shrink-0 ml-2"
                              >
                                <X className="h-3.5 w-3.5" />
                              </button>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  )}

                  {/* Upload progress */}
                  {isUploading && (
                    <div className="space-y-2">
                      <div className="flex justify-between text-sm">
                        <span>Uploading...</span>
                        <span>{uploadProgress}%</span>
                      </div>
                      <div className="w-full bg-secondary rounded-full h-2">
                        <div
                          className="bg-primary h-2 rounded-full transition-all"
                          style={{ width: `${uploadProgress}%` }}
                        />
                      </div>
                    </div>
                  )}

                  {/* Upload complete summary */}
                  {uploadResult && phase === "uploaded" && (
                    <div className="rounded-md bg-green-50 border border-green-200 px-4 py-3 text-sm text-green-800">
                      <div className="flex items-center gap-2 font-medium mb-1">
                        <CheckCircle className="h-4 w-4" />
                        Upload complete
                      </div>
                      <p>
                        {uploadResult.file_count} file{uploadResult.file_count !== 1 ? "s" : ""} ready
                        ({formatSize(uploadResult.total_size_bytes)})
                      </p>
                    </div>
                  )}

                  {/* Upload button */}
                  {phase === "files_selected" && (
                    <button
                      type="button"
                      onClick={handleUpload}
                      disabled={supportedCount === 0}
                      className="w-full rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium disabled:opacity-50 flex items-center justify-center gap-2"
                    >
                      <Upload className="h-4 w-4" />
                      Upload {supportedCount} File{supportedCount !== 1 ? "s" : ""}
                    </button>
                  )}
                </div>
              )}

              {/* Server path tab */}
              {tab === "server" && (
                <div className="space-y-1.5">
                  <label className="text-sm font-medium">Source Directory</label>
                  <input
                    type="text"
                    placeholder="/data/breach_documents"
                    value={sourceDir}
                    onChange={(e) => setSourceDir(e.target.value)}
                    className="w-full rounded-md border bg-background px-3 py-2 text-sm"
                  />
                  <p className="text-xs text-muted-foreground">
                    Absolute path to document directory on the server
                  </p>
                </div>
              )}

              {/* Project select — required */}
              {(tab === "server" || phase === "uploaded") && (
                <div className="space-y-1.5">
                  <label className="text-sm font-medium">Project *</label>
                  <select
                    value={selectedProjectId}
                    onChange={(e) => setSelectedProjectId(e.target.value)}
                    className="w-full rounded-md border bg-background px-3 py-2 text-sm"
                  >
                    <option value="">Select a project...</option>
                    {(projects ?? []).map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name} ({p.status})
                      </option>
                    ))}
                  </select>
                  <p className="text-xs text-muted-foreground">
                    Jobs must be linked to a project for tracking and analysis.
                  </p>
                </div>
              )}

              {/* Protocol select — shared */}
              {(tab === "server" || phase === "uploaded") && (
                <div className="space-y-1.5">
                  <label className="text-sm font-medium">Protocol *</label>
                  <select
                    value={protocolId}
                    onChange={(e) => setProtocolId(e.target.value)}
                    className="w-full rounded-md border bg-background px-3 py-2 text-sm"
                  >
                    <option value="">Select a protocol...</option>
                    {(protocols ?? []).map((p) => (
                      <option key={p.protocol_id} value={p.protocol_id}>
                        {p.name} — {p.jurisdiction} ({p.notification_deadline_days} day deadline)
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {/* Run pipeline button — shared */}
              {(canRunUploadTab || canRunServerTab) && (
                <button
                  type="button"
                  onClick={handleRunPipeline}
                  className="w-full rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium flex items-center justify-center gap-2"
                >
                  Run Pipeline
                </button>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
