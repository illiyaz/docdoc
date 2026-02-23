import { useState, useContext } from "react"
import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { Loader2, CheckCircle } from "lucide-react"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { getProtocols, submitJob } from "@/api/client"
import type { JobResult } from "@/api/client"
import { JobIdSetterContext } from "@/App"

export function JobSubmit() {
  const navigate = useNavigate()
  const setJobId = useContext(JobIdSetterContext)

  const [protocolId, setProtocolId] = useState("")
  const [sourceDir, setSourceDir] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<JobResult | null>(null)

  const { data: protocols } = useQuery({
    queryKey: ["protocols"],
    queryFn: getProtocols,
  })

  const selectedProtocol = protocols?.find((p) => p.protocol_id === protocolId)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!protocolId || !sourceDir.trim()) return
    setLoading(true)
    setError(null)
    try {
      const res = await submitJob({ protocol_id: protocolId, source_directory: sourceDir })
      setResult(res)
      setJobId(res.job_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Job submission failed")
    } finally {
      setLoading(false)
    }
  }

  function handleReset() {
    setResult(null)
    setProtocolId("")
    setSourceDir("")
    setError(null)
  }

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

  return (
    <div className="max-w-lg mx-auto mt-8">
      <Card>
        <CardHeader>
          <CardTitle>Submit Breach Dataset for Analysis</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-5">
            {error && (
              <div className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-800">
                {error}
              </div>
            )}

            {/* Protocol select */}
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Protocol</label>
              <select
                value={protocolId}
                onChange={(e) => setProtocolId(e.target.value)}
                disabled={loading}
                required
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

            {/* Source directory */}
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Source Directory</label>
              <input
                type="text"
                placeholder="/data/breach_documents"
                value={sourceDir}
                onChange={(e) => setSourceDir(e.target.value)}
                disabled={loading}
                required
                className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              />
              <p className="text-xs text-muted-foreground">
                Absolute path to document directory
              </p>
            </div>

            {/* Submit */}
            <button
              type="submit"
              disabled={loading || !protocolId || !sourceDir.trim()}
              className="w-full rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium disabled:opacity-50 flex items-center justify-center gap-2"
            >
              {loading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Processing...
                </>
              ) : (
                "Run Pipeline"
              )}
            </button>

            {loading && (
              <p className="text-xs text-muted-foreground text-center">
                Large datasets may take several minutes
              </p>
            )}
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
