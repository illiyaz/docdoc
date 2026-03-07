import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  Settings as SettingsIcon,
  CheckCircle,
  XCircle,
  Loader2,
  Server,
  Shield,
  Database,
  Brain,
  Upload,
  FileText,
  AlertTriangle,
  Info,
} from "lucide-react"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { getAppSettings, getProtocols, runDiagnostic } from "@/api/client"
import type { AppSettings, DiagnosticReport, DiagnosticPage } from "@/api/client"

// ---------------------------------------------------------------------------
// Status indicator
// ---------------------------------------------------------------------------

function StatusDot({ ok }: { ok: boolean }) {
  return ok ? (
    <CheckCircle className="h-4 w-4 text-green-600" />
  ) : (
    <XCircle className="h-4 w-4 text-red-500" />
  )
}

// ---------------------------------------------------------------------------
// Diagnostic section (absorbed from old Diagnostic page)
// ---------------------------------------------------------------------------

function DiagnosticSection() {
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

  return (
    <div className="space-y-4">
      <div className="flex gap-3 items-end flex-wrap">
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">File</label>
          <label className="flex items-center gap-2 rounded-md border bg-background px-3 py-1.5 text-sm cursor-pointer hover:bg-accent/50">
            <Upload className="h-3.5 w-3.5" />
            {file ? file.name : "Choose file..."}
            <input
              type="file"
              className="hidden"
              accept=".pdf,.docx,.xlsx,.xls,.csv,.html,.htm,.xml,.eml,.msg,.parquet,.avro"
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) { setFile(f); setReport(null); setError(null) }
              }}
            />
          </label>
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Protocol</label>
          <select
            value={protocolId}
            onChange={(e) => setProtocolId(e.target.value)}
            className="rounded-md border bg-background px-3 py-1.5 text-sm"
          >
            <option value="">Select...</option>
            {(protocols ?? []).map((p) => (
              <option key={p.protocol_id} value={p.protocol_id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
        <button
          onClick={handleRun}
          disabled={loading || !file || !protocolId}
          className="rounded-md bg-primary text-primary-foreground px-4 py-1.5 text-sm font-medium disabled:opacity-50 flex items-center gap-2"
        >
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
          {loading ? "Analyzing..." : "Run Diagnostic"}
        </button>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 border border-red-200 px-4 py-2 text-sm text-red-800">
          {error}
        </div>
      )}

      {report && (
        <div className="rounded-md border bg-muted/30 p-4 space-y-3">
          <div className="flex items-center gap-3">
            <FileText className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm font-medium">{report.file_name}</span>
            <Badge variant="outline" className="text-xs">{report.total_pages} pages</Badge>
            <Badge
              variant={report.summary.total_pii_hits > 0 ? "destructive" : "secondary"}
              className="text-xs"
            >
              {report.summary.total_pii_hits} PII hits
            </Badge>
          </div>

          {report.summary.total_pii_hits > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(report.summary.by_entity_type).map(([type, count]) => (
                <Badge key={type} variant="secondary" className="text-xs">
                  {type}: {count}
                </Badge>
              ))}
            </div>
          )}

          <div className="grid grid-cols-3 gap-2 text-center text-xs">
            <div>
              <p className="font-medium">{report.summary.layer_distribution.layer_1}</p>
              <p className="text-muted-foreground">Pattern</p>
            </div>
            <div>
              <p className="font-medium">{report.summary.layer_distribution.layer_2}</p>
              <p className="text-muted-foreground">Context</p>
            </div>
            <div>
              <p className="font-medium">{report.summary.layer_distribution.layer_3}</p>
              <p className="text-muted-foreground">Positional</p>
            </div>
          </div>

          {report.summary.low_confidence_hits > 0 && (
            <div className="flex items-center gap-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-3 py-1.5">
              <AlertTriangle className="h-3.5 w-3.5" />
              {report.summary.low_confidence_hits} low-confidence detections need review
            </div>
          )}

          {/* Per-page summary */}
          <details className="text-sm">
            <summary className="cursor-pointer text-xs font-medium text-muted-foreground hover:text-foreground">
              Page-by-page ({report.pages.length} pages)
            </summary>
            <div className="mt-2 space-y-1">
              {report.pages.map((page: DiagnosticPage, i: number) => (
                <div key={i} className="flex items-center gap-2 text-xs px-2 py-1 rounded hover:bg-accent/30">
                  <span className="font-mono w-16">
                    {page.page_type === "sheet" ? "Sheet" : "Page"} {page.page_number}
                  </span>
                  <span className="text-muted-foreground">
                    {page.blocks_extracted} blocks
                  </span>
                  {page.ocr_used && <Badge variant="outline" className="text-[10px]">OCR</Badge>}
                  {page.skipped_by_onset && <Badge variant="secondary" className="text-[10px]">skipped</Badge>}
                  <Badge
                    variant={page.pii_hits.length > 0 ? "destructive" : "secondary"}
                    className="text-[10px] ml-auto"
                  >
                    {page.pii_hits.length} hits
                  </Badge>
                </div>
              ))}
            </div>
          </details>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Settings page
// ---------------------------------------------------------------------------

export function Settings() {
  const { data: settings, isLoading, isError } = useQuery({
    queryKey: ["app-settings"],
    queryFn: getAppSettings,
    staleTime: 30_000,
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        Loading settings...
      </div>
    )
  }

  if (isError || !settings) {
    return (
      <div className="max-w-3xl mx-auto mt-8">
        <div className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-800">
          Failed to load application settings. Is the backend running?
        </div>
      </div>
    )
  }

  const ollamaIsLocal =
    settings.ollama_url.includes("localhost") ||
    settings.ollama_url.includes("127.0.0.1")

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <SettingsIcon className="h-6 w-6 text-primary" />
        <h1 className="text-2xl font-bold">Settings</h1>
      </div>

      {/* Application */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Shield className="h-4 w-4" />
            Application
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-y-3 gap-x-8 text-sm">
            <div className="text-muted-foreground">App Name</div>
            <div className="font-medium">{settings.app_name}</div>

            <div className="text-muted-foreground">Version</div>
            <div className="font-mono">{settings.app_version}</div>

            <div className="text-muted-foreground">Environment</div>
            <div>
              <Badge variant="outline" className="text-xs">
                {settings.app_env}
              </Badge>
            </div>

            <div className="text-muted-foreground">Database</div>
            <div className="flex items-center gap-2">
              <StatusDot ok={settings.database_url_set} />
              <span>{settings.database_url_set ? "Connected" : "Not configured"}</span>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* LLM Configuration */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Brain className="h-4 w-4" />
            LLM Configuration
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-y-3 gap-x-8 text-sm">
            <div className="text-muted-foreground">LLM Assist</div>
            <div className="flex items-center gap-2">
              <StatusDot ok={settings.llm_assist_enabled} />
              <span>{settings.llm_assist_enabled ? "Enabled" : "Disabled"}</span>
            </div>

            <div className="text-muted-foreground">Model</div>
            <div className="font-mono">{settings.ollama_model}</div>

            <div className="text-muted-foreground">PII Masking</div>
            <div className="flex items-center gap-2">
              <StatusDot ok={settings.pii_masking_enabled} />
              <span>{settings.pii_masking_enabled ? "Enabled" : "Disabled"}</span>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Data Locality */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Server className="h-4 w-4" />
            Data Locality
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-y-3 gap-x-8 text-sm">
            <div className="text-muted-foreground">Inference Endpoint</div>
            <div className="font-mono text-xs">{settings.ollama_url}</div>

            <div className="text-muted-foreground">Endpoint Type</div>
            <div className="flex items-center gap-2">
              <StatusDot ok={ollamaIsLocal} />
              <span>{ollamaIsLocal ? "Local" : "Remote"}</span>
            </div>

            <div className="text-muted-foreground">Network Isolation</div>
            <div className="flex items-center gap-2">
              <StatusDot ok={ollamaIsLocal} />
              <span>{ollamaIsLocal ? "Air-gap safe" : "Requires network"}</span>
            </div>
          </div>

          {!ollamaIsLocal && (
            <div className="mt-3 flex items-center gap-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-3 py-2">
              <Info className="h-3.5 w-3.5 shrink-0" />
              Ollama endpoint is not local. For air-gap compliance, point to localhost.
            </div>
          )}
        </CardContent>
      </Card>

      {/* Diagnostics */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Database className="h-4 w-4" />
            Diagnostics
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground mb-4">
            Upload a single file to preview PII extraction. No data is persisted.
          </p>
          <DiagnosticSection />
        </CardContent>
      </Card>
    </div>
  )
}
