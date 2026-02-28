import { useState, useCallback, useRef } from "react"
import { useQuery } from "@tanstack/react-query"
import { Upload, FileText, Loader2, AlertTriangle, CheckCircle, Info } from "lucide-react"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { getProtocols, runDiagnostic } from "@/api/client"
import type { DiagnosticReport, DiagnosticPage } from "@/api/client"

// ---------------------------------------------------------------------------
// Layer color helpers
// ---------------------------------------------------------------------------

const LAYER_COLORS: Record<string, string> = {
  layer_1: "bg-emerald-500",
  layer_2: "bg-amber-500",
  layer_3: "bg-rose-500",
}

const LAYER_LABELS: Record<string, string> = {
  layer_1: "Pattern Match",
  layer_2: "Context Window",
  layer_3: "Positional",
}

function confidenceColor(score: number): string {
  if (score >= 0.9) return "text-emerald-600"
  if (score >= 0.75) return "text-amber-600"
  return "text-rose-600"
}

// ---------------------------------------------------------------------------
// Upload zone
// ---------------------------------------------------------------------------

function UploadZone({
  onFile,
  disabled,
}: {
  onFile: (f: File) => void
  disabled: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      setDragOver(false)
      const file = e.dataTransfer.files[0]
      if (file) onFile(file)
    },
    [onFile],
  )

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
      onClick={() => inputRef.current?.click()}
      className={`border-2 border-dashed rounded-lg p-10 text-center cursor-pointer transition-colors ${
        dragOver
          ? "border-primary bg-primary/5"
          : "border-muted-foreground/25 hover:border-primary/50"
      } ${disabled ? "opacity-50 pointer-events-none" : ""}`}
    >
      <Upload className="h-10 w-10 mx-auto text-muted-foreground mb-3" />
      <p className="text-sm font-medium">Drop a file here or click to browse</p>
      <p className="text-xs text-muted-foreground mt-1">
        PDF, DOCX, XLSX, CSV, HTML, EML, Parquet, Avro
      </p>
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        disabled={disabled}
        accept=".pdf,.docx,.xlsx,.xls,.csv,.html,.htm,.xml,.eml,.msg,.parquet,.avro"
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) onFile(file)
        }}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Layer distribution bar
// ---------------------------------------------------------------------------

function LayerBar({ distribution }: { distribution: DiagnosticReport["summary"]["layer_distribution"] }) {
  const total = distribution.layer_1 + distribution.layer_2 + distribution.layer_3
  if (total === 0) return <p className="text-xs text-muted-foreground">No detections</p>

  return (
    <div className="space-y-2">
      <div className="flex h-3 rounded-full overflow-hidden">
        {(["layer_1", "layer_2", "layer_3"] as const).map((layer) => {
          const val = distribution[layer]
          if (val === 0) return null
          const pct = (val / total) * 100
          return (
            <div
              key={layer}
              className={`${LAYER_COLORS[layer]} transition-all`}
              style={{ width: `${pct}%` }}
              title={`${LAYER_LABELS[layer]}: ${val}`}
            />
          )
        })}
      </div>
      <div className="flex gap-4 text-xs text-muted-foreground">
        {(["layer_1", "layer_2", "layer_3"] as const).map((layer) => (
          <span key={layer} className="flex items-center gap-1">
            <span className={`inline-block h-2 w-2 rounded-full ${LAYER_COLORS[layer]}`} />
            {LAYER_LABELS[layer]}: {distribution[layer]}
          </span>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Recommendation box
// ---------------------------------------------------------------------------

function Recommendation({ report }: { report: DiagnosticReport }) {
  const { summary } = report
  const total = summary.total_pii_hits
  const lowPct = total > 0 ? (summary.low_confidence_hits / total) * 100 : 0

  if (total === 0) {
    return (
      <div className="rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 flex gap-3">
        <CheckCircle className="h-5 w-5 text-emerald-600 shrink-0 mt-0.5" />
        <div>
          <p className="text-sm font-medium text-emerald-800">No PII Detected</p>
          <p className="text-xs text-emerald-700 mt-0.5">
            No personally identifiable information was found in this file.
          </p>
        </div>
      </div>
    )
  }

  if (lowPct > 30) {
    return (
      <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 flex gap-3">
        <AlertTriangle className="h-5 w-5 text-amber-600 shrink-0 mt-0.5" />
        <div>
          <p className="text-sm font-medium text-amber-800">High Review Load Expected</p>
          <p className="text-xs text-amber-700 mt-0.5">
            {summary.low_confidence_hits} of {total} detections ({lowPct.toFixed(0)}%) are
            below the 0.75 confidence threshold. Plan for significant human review time.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="rounded-md border border-blue-200 bg-blue-50 px-4 py-3 flex gap-3">
      <Info className="h-5 w-5 text-blue-600 shrink-0 mt-0.5" />
      <div>
        <p className="text-sm font-medium text-blue-800">
          {total} PII Detection{total !== 1 ? "s" : ""} Found
        </p>
        <p className="text-xs text-blue-700 mt-0.5">
          {summary.low_confidence_hits} low-confidence hit{summary.low_confidence_hits !== 1 ? "s" : ""} will
          need human review. Estimated review load is manageable.
        </p>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page result card
// ---------------------------------------------------------------------------

function PageCard({ page }: { page: DiagnosticPage }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="border rounded-md">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-4 py-2.5 text-sm hover:bg-accent/50 transition-colors"
      >
        <span className="flex items-center gap-2">
          <FileText className="h-4 w-4 text-muted-foreground" />
          <span className="font-medium">
            {page.page_type === "sheet" ? "Sheet" : "Page"} {page.page_number}
          </span>
          <span className="text-muted-foreground">
            {page.blocks_extracted} block{page.blocks_extracted !== 1 ? "s" : ""}
          </span>
          {page.ocr_used && (
            <Badge variant="outline" className="text-[10px]">
              OCR
            </Badge>
          )}
          {page.skipped_by_onset && (
            <Badge variant="secondary" className="text-[10px]">
              skipped
            </Badge>
          )}
        </span>
        <Badge variant={page.pii_hits.length > 0 ? "destructive" : "secondary"}>
          {page.pii_hits.length} hit{page.pii_hits.length !== 1 ? "s" : ""}
        </Badge>
      </button>

      {expanded && page.pii_hits.length > 0 && (
        <div className="border-t px-4 py-3 space-y-2">
          {page.pii_hits.map((hit, i) => (
            <div key={i} className="rounded-md bg-muted/50 px-3 py-2 text-sm space-y-1">
              <div className="flex items-center gap-2">
                <Badge variant="outline">{hit.entity_type}</Badge>
                <span className={`text-xs font-mono font-medium ${confidenceColor(hit.confidence)}`}>
                  {(hit.confidence * 100).toFixed(1)}%
                </span>
                <span className="text-xs text-muted-foreground">{hit.extraction_layer}</span>
              </div>
              <p className="font-mono text-xs text-muted-foreground break-all">
                {hit.masked_value}
              </p>
              {hit.context_snippet && (
                <p className="text-xs text-muted-foreground italic break-all">
                  ...{hit.context_snippet}...
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {expanded && page.pii_hits.length === 0 && (
        <div className="border-t px-4 py-3 text-sm text-muted-foreground">
          No PII detected on this page.
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main Diagnostic page
// ---------------------------------------------------------------------------

export function Diagnostic() {
  const [file, setFile] = useState<File | null>(null)
  const [protocolId, setProtocolId] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<DiagnosticReport | null>(null)

  const { data: protocols } = useQuery({
    queryKey: ["protocols"],
    queryFn: getProtocols,
  })

  async function handleRun() {
    if (!file || !protocolId) return
    setLoading(true)
    setError(null)
    setReport(null)
    try {
      const res = await runDiagnostic(file, protocolId)
      setReport(res)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Diagnostic failed")
    } finally {
      setLoading(false)
    }
  }

  function handleReset() {
    setFile(null)
    setProtocolId("")
    setReport(null)
    setError(null)
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {/* Left panel — upload */}
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">Diagnostic Scan</h1>
        <p className="text-sm text-muted-foreground">
          Upload a single file to preview PII extraction before running a full pipeline job.
          No data is persisted.
        </p>

        <Card>
          <CardContent className="pt-6 space-y-4">
            <UploadZone
              onFile={(f) => {
                setFile(f)
                setReport(null)
                setError(null)
              }}
              disabled={loading}
            />

            {file && (
              <div className="flex items-center gap-2 text-sm">
                <FileText className="h-4 w-4 text-muted-foreground" />
                <span className="font-medium">{file.name}</span>
                <span className="text-muted-foreground">
                  ({(file.size / 1024).toFixed(1)} KB)
                </span>
              </div>
            )}

            <div className="space-y-1.5">
              <label className="text-sm font-medium">Protocol</label>
              <select
                value={protocolId}
                onChange={(e) => setProtocolId(e.target.value)}
                disabled={loading}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              >
                <option value="">Select a protocol...</option>
                {(protocols ?? []).map((p) => (
                  <option key={p.protocol_id} value={p.protocol_id}>
                    {p.name} — {p.jurisdiction}
                  </option>
                ))}
              </select>
            </div>

            {error && (
              <div className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-800">
                {error}
              </div>
            )}

            <div className="flex gap-3">
              <button
                onClick={handleRun}
                disabled={loading || !file || !protocolId}
                className="flex-1 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {loading ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Analyzing...
                  </>
                ) : (
                  "Run Diagnostic"
                )}
              </button>
              {report && (
                <button
                  onClick={handleReset}
                  className="rounded-md border px-4 py-2 text-sm font-medium hover:bg-accent"
                >
                  Reset
                </button>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Right panel — results */}
      <div className="space-y-4">
        {!report && !loading && (
          <div className="flex items-center justify-center h-64 text-muted-foreground text-sm">
            Upload a file and run diagnostic to see results
          </div>
        )}

        {loading && (
          <div className="flex flex-col items-center justify-center h-64 gap-3 text-muted-foreground">
            <Loader2 className="h-8 w-8 animate-spin" />
            <p className="text-sm">Extracting and analyzing PII...</p>
          </div>
        )}

        {report && (
          <>
            {/* Summary card */}
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-lg">
                  Summary — {report.file_name}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid grid-cols-3 gap-3">
                  <div className="text-center">
                    <p className="text-2xl font-bold">{report.summary.total_pii_hits}</p>
                    <p className="text-xs text-muted-foreground">PII Hits</p>
                  </div>
                  <div className="text-center">
                    <p className="text-2xl font-bold">{report.total_pages}</p>
                    <p className="text-xs text-muted-foreground">Pages</p>
                  </div>
                  <div className="text-center">
                    <p className="text-2xl font-bold">{report.summary.low_confidence_hits}</p>
                    <p className="text-xs text-muted-foreground">Low Confidence</p>
                  </div>
                </div>

                {/* Entity type breakdown */}
                {Object.keys(report.summary.by_entity_type).length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {Object.entries(report.summary.by_entity_type).map(([type, count]) => (
                      <Badge key={type} variant="secondary">
                        {type}: {count}
                      </Badge>
                    ))}
                  </div>
                )}

                {/* Layer distribution */}
                <div>
                  <p className="text-xs font-medium text-muted-foreground mb-1.5">
                    Extraction Layer Distribution
                  </p>
                  <LayerBar distribution={report.summary.layer_distribution} />
                </div>

                <Recommendation report={report} />
              </CardContent>
            </Card>

            {/* Page-by-page results */}
            <div className="space-y-2">
              <h3 className="text-sm font-medium text-muted-foreground">
                Page-by-Page Results
              </h3>
              {report.pages.map((page, i) => (
                <PageCard key={i} page={page} />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
