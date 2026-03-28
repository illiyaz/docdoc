import { useState, useEffect } from "react"

const BASE_URL = import.meta.env.VITE_API_URL ?? "/api"

interface MergeSignal {
  anchor: string
  matched: boolean
  score: number
  detail: string
  field_a: string
  field_b: string
}

interface MergePair {
  record_a_label: string
  record_b_label: string
  overall_confidence: number
  signals: MergeSignal[]
}

interface MergeExplanationData {
  subject_id: string
  canonical_name: string | null
  merge_confidence: number | null
  merge_explanation: { pairs: MergePair[] }
}

export function MergeExplanation({ subjectId }: { subjectId: string }) {
  const [data, setData] = useState<MergeExplanationData | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch(`${BASE_URL}/review/subjects/${subjectId}/merge-explanation`)
      .then((r) => r.json())
      .then((d) => {
        setData(d)
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [subjectId])

  if (loading) return <div className="text-xs text-gray-400">Loading merge details...</div>
  if (!data || !data.merge_explanation?.pairs?.length) {
    return <div className="text-xs text-gray-400">No merge history (single record)</div>
  }

  return (
    <div className="space-y-3">
      <h4 className="text-sm font-medium text-gray-700">
        Merge History ({data.merge_explanation.pairs.length} pair{data.merge_explanation.pairs.length > 1 ? "s" : ""})
      </h4>
      {data.merge_explanation.pairs.map((pair, i) => (
        <div key={i} className="rounded border border-gray-200 bg-gray-50 p-3 text-xs">
          <div className="mb-2 flex items-center justify-between">
            <div className="space-y-0.5">
              <div className="font-medium text-gray-700">{pair.record_a_label}</div>
              <div className="text-gray-400">merged with</div>
              <div className="font-medium text-gray-700">{pair.record_b_label}</div>
            </div>
            <div
              className={`rounded px-2 py-0.5 text-xs font-medium ${
                pair.overall_confidence >= 0.8
                  ? "bg-green-100 text-green-700"
                  : pair.overall_confidence >= 0.6
                  ? "bg-amber-100 text-amber-700"
                  : "bg-red-100 text-red-700"
              }`}
            >
              {(pair.overall_confidence * 100).toFixed(0)}%
            </div>
          </div>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-gray-500">
                <th className="text-left py-0.5">Signal</th>
                <th className="text-left py-0.5">Record A</th>
                <th className="text-left py-0.5">Record B</th>
                <th className="text-right py-0.5">Score</th>
              </tr>
            </thead>
            <tbody>
              {pair.signals.map((sig, j) => (
                <tr key={j} className={sig.matched ? "text-gray-700" : "text-gray-400"}>
                  <td className="py-0.5">
                    <span className={`mr-1 ${sig.matched ? "text-green-600" : "text-red-400"}`}>
                      {sig.matched ? "\u2713" : "\u2717"}
                    </span>
                    {sig.detail}
                  </td>
                  <td className="py-0.5 font-mono">{sig.field_a || "-"}</td>
                  <td className="py-0.5 font-mono">{sig.field_b || "-"}</td>
                  <td className="py-0.5 text-right">{sig.score > 0 ? `+${sig.score.toFixed(2)}` : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  )
}
