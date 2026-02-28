import { useState, useContext, useRef, useCallback } from "react"
import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import {
  Loader2, CheckCircle, Upload, FolderOpen, Server,
  X, FileText, Circle, AlertCircle,
} from "lucide-react"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { getProtocols, uploadFiles, submitJobStreaming } from "@/api/client"
import type { JobResult, UploadResult, PipelineProgress } from "@/api/client"
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
  { id: "discovery", label: "Document Discovery" },
  { id: "detection", label: "PII Detection" },
  { id: "resolution", label: "Entity Resolution" },
  { id: "deduplication", label: "Deduplication" },
  { id: "notification", label: "Notification List" },
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
// Main component
// ---------------------------------------------------------------------------

type Tab = "upload" | "server"
type Phase = "idle" | "files_selected" | "uploading" | "uploaded" | "running" | "complete"

export function JobSubmit() {
  const navigate = useNavigate()
  const setJobId = useContext(JobIdSetterContext)

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
    setError(null)
    setStageStates({})
    setPhase("running")

    try {
      const body = tab === "upload" && uploadResult
        ? { protocol_id: protocolId, upload_id: uploadResult.upload_id }
        : { protocol_id: protocolId, source_directory: sourceDir }

      const res = await submitJobStreaming(body, handlePipelineProgress)
      setResult(res)
      setJobId(res.job_id)
      setPhase("complete")
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
  const canRunUploadTab = tab === "upload" && phase === "uploaded" && protocolId !== ""
  const canRunServerTab = tab === "server" && sourceDir.trim() !== "" && protocolId !== "" && !isRunning

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
            <div className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <p className="text-muted-foreground">Subjects found</p>
                <p className="text-lg font-bold">{result.subjects_found}</p>
              </div>
              <div>
                <p className="text-muted-foreground">Notification required</p>
                <p className="text-lg font-bold">{result.notification_required}</p>
              </div>
            </div>
            {selectedProtocol && (
              <p className="text-sm text-muted-foreground">
                Protocol applied: {selectedProtocol.name}
              </p>
            )}
            <div className="flex gap-3 pt-2">
              <button
                onClick={() => navigate("/queues/low_confidence")}
                className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium"
              >
                View Review Queue &rarr;
              </button>
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

  return (
    <div className="max-w-2xl mx-auto mt-8">
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

              {/* Protocol select — shared */}
              {(tab === "server" || phase === "uploaded") && (
                <div className="space-y-1.5">
                  <label className="text-sm font-medium">Protocol</label>
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
