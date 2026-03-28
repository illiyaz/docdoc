import { useState, useEffect, useCallback } from "react"
import {
  getDocumentInfo,
  getDocumentPage,
  type DocumentInfo,
  type DocumentPageResponse,
  type PageHighlight,
} from "../api/client"
import { PIIBadge } from "./PIIBadge"

interface DocumentViewerProps {
  documentId: string
  initialPage?: number
  highlightExtractions?: boolean
  compact?: boolean
  onClose?: () => void
}

const PII_COLORS: Record<string, string> = {
  PERSON: "bg-blue-100 border-blue-400",
  US_SSN: "bg-red-100 border-red-400",
  DOB: "bg-orange-100 border-orange-400",
  LOCATION: "bg-green-100 border-green-400",
  EMAIL_ADDRESS: "bg-purple-100 border-purple-400",
  PHONE_NUMBER: "bg-teal-100 border-teal-400",
  NI_NUMBER: "bg-red-100 border-red-400",
  GOVERNMENT_ID: "bg-red-100 border-red-400",
}

export function DocumentViewer({
  documentId,
  initialPage,
  highlightExtractions = true,
  compact = false,
  onClose,
}: DocumentViewerProps) {
  const [info, setInfo] = useState<DocumentInfo | null>(null)
  const [pageData, setPageData] = useState<DocumentPageResponse | null>(null)
  const [currentPage, setCurrentPage] = useState(initialPage ?? 0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Load document info on mount
  useEffect(() => {
    getDocumentInfo(documentId)
      .then((data) => {
        setInfo(data)
        if (!data.is_pdf) {
          setError("Document viewer is only available for PDF files")
          setLoading(false)
          return
        }
        const startPage = initialPage ?? data.onset_page ?? 0
        setCurrentPage(startPage)
      })
      .catch((e) => {
        setError(e.message || "Failed to load document info")
        setLoading(false)
      })
  }, [documentId, initialPage])

  // Load page image when currentPage changes
  const loadPage = useCallback(
    (pageNum: number) => {
      if (!info?.is_pdf) return
      setLoading(true)
      setError(null)
      getDocumentPage(documentId, pageNum, { highlight: highlightExtractions })
        .then((data) => {
          setPageData(data)
          setLoading(false)
        })
        .catch((e) => {
          setError(e.message || "Failed to render page")
          setLoading(false)
        })
    },
    [documentId, info, highlightExtractions]
  )

  useEffect(() => {
    if (info?.is_pdf) loadPage(currentPage)
  }, [currentPage, info, loadPage])

  const goToPrev = () => setCurrentPage((p) => Math.max(0, p - 1))
  const goToNext = () =>
    setCurrentPage((p) => Math.min((pageData?.page_count ?? 1) - 1, p + 1))

  if (error && !info?.is_pdf) {
    return (
      <div className="rounded border border-gray-200 bg-gray-50 p-4 text-sm text-gray-500">
        {error}
        {onClose && (
          <button onClick={onClose} className="ml-2 text-blue-600 underline">
            Close
          </button>
        )}
      </div>
    )
  }

  return (
    <div className={`rounded border border-gray-200 bg-white ${compact ? "p-2" : "p-4"}`}>
      {/* Header */}
      <div className="mb-2 flex items-center justify-between">
        <h4 className="text-sm font-medium text-gray-700">
          {info?.file_name ?? "Loading..."}
        </h4>
        {onClose && (
          <button
            onClick={onClose}
            className="text-xs text-gray-400 hover:text-gray-600"
          >
            Close
          </button>
        )}
      </div>

      {/* Page navigation */}
      <div className="mb-2 flex items-center gap-2 text-xs text-gray-500">
        <button
          onClick={goToPrev}
          disabled={currentPage <= 0 || loading}
          className="rounded border px-2 py-0.5 disabled:opacity-30"
        >
          Prev
        </button>
        <span>
          Page {currentPage + 1} of {pageData?.page_count ?? info?.page_count ?? "?"}
        </span>
        <button
          onClick={goToNext}
          disabled={
            currentPage >= (pageData?.page_count ?? info?.page_count ?? 1) - 1 ||
            loading
          }
          className="rounded border px-2 py-0.5 disabled:opacity-30"
        >
          Next
        </button>
        <input
          type="number"
          min={1}
          max={pageData?.page_count ?? info?.page_count ?? 1}
          value={currentPage + 1}
          onChange={(e) => {
            const v = parseInt(e.target.value, 10)
            if (!isNaN(v) && v >= 1) setCurrentPage(v - 1)
          }}
          className="w-14 rounded border px-1 py-0.5 text-center text-xs"
        />
      </div>

      {/* Page image */}
      {loading && (
        <div className="flex h-48 items-center justify-center bg-gray-50 text-sm text-gray-400">
          Rendering page...
        </div>
      )}
      {error && !loading && (
        <div className="rounded bg-red-50 p-3 text-sm text-red-600">{error}</div>
      )}
      {pageData && !loading && (
        <div className="overflow-auto rounded border" style={{ maxHeight: compact ? 400 : 700 }}>
          <img
            src={`data:image/png;base64,${pageData.image_base64}`}
            alt={`Page ${currentPage + 1}`}
            className="w-full"
          />
        </div>
      )}

      {/* Extraction highlights legend */}
      {pageData && pageData.highlighted_extractions.length > 0 && !loading && (
        <div className="mt-2">
          <p className="mb-1 text-xs font-medium text-gray-500">
            Extractions on this page:
          </p>
          <div className="flex flex-wrap gap-1">
            {pageData.highlighted_extractions.map((h: PageHighlight) => (
              <span
                key={h.extraction_id}
                className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs ${
                  PII_COLORS[h.pii_type] ?? "bg-gray-100 border-gray-300"
                }`}
              >
                <PIIBadge type={h.pii_type} />
                {h.masked_value && (
                  <span className="text-gray-600">{h.masked_value}</span>
                )}
                {h.bbox && <span className="text-gray-400">(highlighted)</span>}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
