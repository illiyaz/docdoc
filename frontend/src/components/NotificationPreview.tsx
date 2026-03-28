import { useState, useEffect } from "react"

const BASE_URL = import.meta.env.VITE_API_URL ?? "/api"

interface PreviewData {
  subject_id: string
  subject_name: string
  protocol_id: string
  format: "email" | "letter"
  html: string
}

export function NotificationPreview({
  subjectId,
  protocolId = "default",
}: {
  subjectId: string
  protocolId?: string
}) {
  const [format, setFormat] = useState<"email" | "letter">("email")
  const [preview, setPreview] = useState<PreviewData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetch(
      `${BASE_URL}/notifications/preview/${format}?subject_id=${subjectId}&protocol_id=${protocolId}`
    )
      .then((r) => {
        if (!r.ok) throw new Error(`Preview failed: ${r.status}`)
        return r.json()
      })
      .then((d) => {
        setPreview(d)
        setLoading(false)
      })
      .catch((e) => {
        setError(e.message)
        setLoading(false)
      })
  }, [subjectId, protocolId, format])

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">Notification Preview</h4>
        <div className="flex gap-1 rounded border p-0.5 text-xs">
          <button
            onClick={() => setFormat("email")}
            className={`rounded px-2 py-0.5 ${
              format === "email" ? "bg-blue-100 text-blue-700" : "text-gray-500"
            }`}
          >
            Email
          </button>
          <button
            onClick={() => setFormat("letter")}
            className={`rounded px-2 py-0.5 ${
              format === "letter" ? "bg-blue-100 text-blue-700" : "text-gray-500"
            }`}
          >
            Letter
          </button>
        </div>
      </div>

      {loading && (
        <div className="flex h-32 items-center justify-center text-sm text-gray-400">
          Rendering preview...
        </div>
      )}
      {error && <div className="rounded bg-red-50 p-2 text-xs text-red-600">{error}</div>}
      {preview && !loading && (
        <div className="rounded border border-gray-200">
          <div className="border-b bg-gray-50 px-3 py-1.5 text-xs text-gray-500">
            {preview.format === "email" ? "Email Preview" : "Letter Preview"} for{" "}
            <span className="font-medium">{preview.subject_name}</span> ({preview.protocol_id})
          </div>
          <iframe
            srcDoc={preview.html}
            sandbox=""
            className="w-full border-0"
            style={{ minHeight: 300, maxHeight: 600 }}
            title="Notification preview"
          />
        </div>
      )}

      <p className="text-xs text-gray-400">
        All PII values are masked in this preview. The actual notification will
        contain the real values for the subject.
      </p>
    </div>
  )
}
