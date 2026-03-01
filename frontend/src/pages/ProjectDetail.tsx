import { useState, useCallback, useMemo } from "react"
import { useParams, Link } from "react-router-dom"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ArrowLeft,
  Loader2,
  Save,
  FileText,
  Shield,
  BarChart3,
  Download,
  Plus,
  Lock,
  CheckCircle,
  ChevronUp,
  ChevronDown,
  Code,
  GripVertical,
  X,
} from "lucide-react"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { formatDistanceToNow, parseISO } from "date-fns"
import {
  getProject,
  updateProject,
  getCatalogSummary,
  getDensity,
  listExports,
  createExport,
  createProtocolConfig,
  getExportDownloadUrl,
} from "@/api/client"
import type {
  ProjectDetail as ProjectDetailType,
  ProtocolConfigSummary,
  CatalogSummary,
  DensityResponse,
  ExportJobSummary,
} from "@/api/client"

// ---------------------------------------------------------------------------
// Tab type
// ---------------------------------------------------------------------------

type TabId = "overview" | "protocols" | "catalog" | "density" | "exports"

const TABS: { id: TabId; label: string; icon: typeof FileText }[] = [
  { id: "overview", label: "Overview", icon: FileText },
  { id: "protocols", label: "Protocols", icon: Shield },
  { id: "catalog", label: "Catalog", icon: BarChart3 },
  { id: "density", label: "Density", icon: BarChart3 },
  { id: "exports", label: "Exports", icon: Download },
]

// ---------------------------------------------------------------------------
// Status helpers
// ---------------------------------------------------------------------------

const PROJECT_STATUS_STYLES: Record<string, string> = {
  active: "bg-green-100 text-green-800 border-green-300",
  archived: "bg-gray-100 text-gray-600 border-gray-300",
  completed: "bg-blue-100 text-blue-800 border-blue-300",
}

const PROTOCOL_STATUS_STYLES: Record<string, string> = {
  draft: "bg-yellow-100 text-yellow-800 border-yellow-300",
  active: "bg-green-100 text-green-800 border-green-300",
  locked: "bg-gray-100 text-gray-600 border-gray-300",
}

// ---------------------------------------------------------------------------
// Overview tab
// ---------------------------------------------------------------------------

function OverviewTab({
  project,
  onUpdated,
}: {
  project: ProjectDetailType
  onUpdated: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(project.name)
  const [description, setDescription] = useState(project.description ?? "")
  const [status, setStatus] = useState(project.status)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSave() {
    setSaving(true)
    setError(null)
    try {
      await updateProject(project.id, {
        name: name.trim() || null,
        description: description.trim() || null,
        status,
      })
      setEditing(false)
      onUpdated()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update project")
    } finally {
      setSaving(false)
    }
  }

  const createdAgo = project.created_at
    ? formatDistanceToNow(parseISO(project.created_at), { addSuffix: true })
    : null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between">
          <span>Project Information</span>
          {!editing && (
            <button
              onClick={() => setEditing(true)}
              className="text-xs font-medium text-primary hover:underline"
            >
              Edit
            </button>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && (
          <div className="rounded-md bg-red-50 border border-red-200 px-4 py-2 text-sm text-red-800">
            {error}
          </div>
        )}

        {editing ? (
          <div className="space-y-3">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Name</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Description</label>
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm min-h-[80px]"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Status</label>
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              >
                <option value="active">Active</option>
                <option value="archived">Archived</option>
                <option value="completed">Completed</option>
              </select>
            </div>
            <div className="flex gap-2">
              <button
                onClick={handleSave}
                disabled={saving}
                className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium disabled:opacity-50 flex items-center gap-2"
              >
                {saving ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Save className="h-4 w-4" />
                )}
                Save
              </button>
              <button
                onClick={() => {
                  setEditing(false)
                  setName(project.name)
                  setDescription(project.description ?? "")
                  setStatus(project.status)
                }}
                className="rounded-md border px-4 py-2 text-sm font-medium hover:bg-accent"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-xs font-medium text-muted-foreground">Name</p>
                <p className="text-sm font-medium">{project.name}</p>
              </div>
              <div>
                <p className="text-xs font-medium text-muted-foreground">Status</p>
                <Badge
                  variant="outline"
                  className={`${PROJECT_STATUS_STYLES[project.status] ?? ""} text-xs font-medium mt-0.5`}
                >
                  {project.status}
                </Badge>
              </div>
            </div>
            {project.description && (
              <div>
                <p className="text-xs font-medium text-muted-foreground">Description</p>
                <p className="text-sm">{project.description}</p>
              </div>
            )}
            <div className="grid grid-cols-2 gap-4">
              {project.created_by && (
                <div>
                  <p className="text-xs font-medium text-muted-foreground">Created By</p>
                  <p className="text-sm">{project.created_by}</p>
                </div>
              )}
              {createdAgo && (
                <div>
                  <p className="text-xs font-medium text-muted-foreground">Created</p>
                  <p className="text-sm">{createdAgo}</p>
                </div>
              )}
            </div>
            <div>
              <p className="text-xs font-medium text-muted-foreground">Protocol Configs</p>
              <p className="text-sm">{project.protocols.length} configured</p>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Protocol configs tab
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Protocol form constants & defaults
// ---------------------------------------------------------------------------

const BASE_PROTOCOLS = [
  { id: "hipaa", label: "HIPAA Breach Rule" },
  { id: "gdpr", label: "GDPR (Articles 33-34)" },
  { id: "ccpa", label: "CCPA" },
  { id: "pci_dss", label: "PCI-DSS" },
  { id: "state_breach", label: "State Breach (Generic)" },
  { id: "custom", label: "Custom" },
] as const

type BaseProtocolId = (typeof BASE_PROTOCOLS)[number]["id"]

/** Entity types grouped by category for the checkbox UI */
const ENTITY_TYPE_GROUPS = [
  {
    category: "Identity",
    types: [
      { id: "US_SSN", label: "SSN" },
      { id: "US_PASSPORT", label: "Passport" },
      { id: "US_DRIVER_LICENSE", label: "Driver's License" },
      { id: "PERSON", label: "Name" },
      { id: "DATE_OF_BIRTH_MDY", label: "Date of Birth" },
      { id: "EMAIL_ADDRESS", label: "Email" },
      { id: "PHONE_NUMBER", label: "Phone" },
    ],
  },
  {
    category: "Financial",
    types: [
      { id: "CREDIT_CARD", label: "Credit Card" },
      { id: "US_BANK_NUMBER", label: "Bank Account" },
      { id: "IBAN", label: "IBAN" },
    ],
  },
  {
    category: "Health",
    types: [
      { id: "MEDICAL_LICENSE", label: "Medical License" },
      { id: "NPI", label: "NPI Number" },
      { id: "MRN", label: "Medical Record No." },
      { id: "HICN", label: "Health Insurance No." },
    ],
  },
] as const

const DEDUP_ANCHORS = ["ssn", "email", "phone", "address", "name"] as const
type DedupAnchor = (typeof DEDUP_ANCHORS)[number]

const DEFAULT_EXPORT_FIELDS = [
  "subject_id",
  "canonical_name",
  "canonical_email",
  "canonical_phone",
  "pii_types_found",
  "notification_required",
  "review_status",
]

/** Per-protocol defaults — entity types, confidence, anchors, sampling, storage, export */
interface ProtocolDefaults {
  entityTypes: string[]
  confidence: number
  dedupAnchors: DedupAnchor[]
  samplingRate: number
  samplingMin: number
  samplingMax: number
  storagePolicy: "strict" | "investigation"
  exportFields: string[]
}

const PROTOCOL_DEFAULTS: Record<BaseProtocolId, ProtocolDefaults> = {
  hipaa: {
    entityTypes: ["US_SSN", "MRN", "NPI", "HICN", "MEDICAL_LICENSE"],
    confidence: 0.75,
    dedupAnchors: ["ssn", "name"],
    samplingRate: 10,
    samplingMin: 5,
    samplingMax: 100,
    storagePolicy: "strict",
    exportFields: [...DEFAULT_EXPORT_FIELDS],
  },
  gdpr: {
    entityTypes: ["EMAIL_ADDRESS", "PHONE_NUMBER", "PERSON", "IBAN"],
    confidence: 0.70,
    dedupAnchors: ["email", "name", "phone"],
    samplingRate: 15,
    samplingMin: 5,
    samplingMax: 200,
    storagePolicy: "strict",
    exportFields: [...DEFAULT_EXPORT_FIELDS],
  },
  ccpa: {
    entityTypes: ["US_SSN", "CREDIT_CARD", "US_BANK_NUMBER", "MRN"],
    confidence: 0.75,
    dedupAnchors: ["ssn", "email", "name"],
    samplingRate: 10,
    samplingMin: 5,
    samplingMax: 100,
    storagePolicy: "strict",
    exportFields: [...DEFAULT_EXPORT_FIELDS],
  },
  pci_dss: {
    entityTypes: ["CREDIT_CARD", "US_BANK_NUMBER", "IBAN"],
    confidence: 0.85,
    dedupAnchors: ["name", "email"],
    samplingRate: 20,
    samplingMin: 10,
    samplingMax: 50,
    storagePolicy: "strict",
    exportFields: [...DEFAULT_EXPORT_FIELDS],
  },
  state_breach: {
    entityTypes: ["US_SSN", "CREDIT_CARD", "US_BANK_NUMBER", "US_DRIVER_LICENSE"],
    confidence: 0.75,
    dedupAnchors: ["ssn", "name", "address"],
    samplingRate: 10,
    samplingMin: 5,
    samplingMax: 100,
    storagePolicy: "strict",
    exportFields: [...DEFAULT_EXPORT_FIELDS],
  },
  custom: {
    entityTypes: [],
    confidence: 0.75,
    dedupAnchors: ["ssn", "email"],
    samplingRate: 10,
    samplingMin: 5,
    samplingMax: 100,
    storagePolicy: "strict",
    exportFields: [...DEFAULT_EXPORT_FIELDS],
  },
}

// ---------------------------------------------------------------------------
// Guided Protocol Create Form
// ---------------------------------------------------------------------------

function ProtocolCreateForm({
  projectId,
  onCreated,
  onCancel,
}: {
  projectId: string
  onCreated: () => void
  onCancel: () => void
}) {
  const [pcName, setPcName] = useState("")
  const [baseProtocol, setBaseProtocol] = useState<BaseProtocolId | "">("")
  const [entityTypes, setEntityTypes] = useState<Set<string>>(new Set())
  const [confidence, setConfidence] = useState(0.75)
  const [dedupAnchors, setDedupAnchors] = useState<Set<DedupAnchor>>(new Set(["ssn", "email"]))
  const [samplingRate, setSamplingRate] = useState(10)
  const [samplingMin, setSamplingMin] = useState(5)
  const [samplingMax, setSamplingMax] = useState(100)
  const [storagePolicy, setStoragePolicy] = useState<"strict" | "investigation">("strict")
  const [exportFields, setExportFields] = useState<string[]>([...DEFAULT_EXPORT_FIELDS])
  const [newFieldInput, setNewFieldInput] = useState("")
  const [showRawJson, setShowRawJson] = useState(false)
  const [rawJsonOverride, setRawJsonOverride] = useState("")
  const [rawJsonMode, setRawJsonMode] = useState(false) // true = editing raw JSON directly
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /** Apply protocol defaults when base protocol changes */
  const handleBaseProtocolChange = useCallback((id: BaseProtocolId) => {
    setBaseProtocol(id)
    const defaults = PROTOCOL_DEFAULTS[id]
    setEntityTypes(new Set(defaults.entityTypes))
    setConfidence(defaults.confidence)
    setDedupAnchors(new Set(defaults.dedupAnchors))
    setSamplingRate(defaults.samplingRate)
    setSamplingMin(defaults.samplingMin)
    setSamplingMax(defaults.samplingMax)
    setStoragePolicy(defaults.storagePolicy)
    setExportFields([...defaults.exportFields])
    setRawJsonMode(false)
    setRawJsonOverride("")
  }, [])

  /** Toggle an entity type checkbox */
  const toggleEntityType = useCallback((typeId: string) => {
    setEntityTypes((prev) => {
      const next = new Set(prev)
      if (next.has(typeId)) next.delete(typeId)
      else next.add(typeId)
      return next
    })
  }, [])

  /** Toggle a dedup anchor checkbox */
  const toggleAnchor = useCallback((anchor: DedupAnchor) => {
    setDedupAnchors((prev) => {
      const next = new Set(prev)
      if (next.has(anchor)) next.delete(anchor)
      else next.add(anchor)
      return next
    })
  }, [])

  /** Move export field up/down */
  const moveField = useCallback((index: number, direction: "up" | "down") => {
    setExportFields((prev) => {
      const arr = [...prev]
      const target = direction === "up" ? index - 1 : index + 1
      if (target < 0 || target >= arr.length) return prev
      ;[arr[index], arr[target]] = [arr[target], arr[index]]
      return arr
    })
  }, [])

  /** Remove an export field */
  const removeField = useCallback((index: number) => {
    setExportFields((prev) => prev.filter((_, i) => i !== index))
  }, [])

  /** Add a custom export field */
  const addField = useCallback(() => {
    const val = newFieldInput.trim()
    if (!val) return
    setExportFields((prev) => (prev.includes(val) ? prev : [...prev, val]))
    setNewFieldInput("")
  }, [newFieldInput])

  /** Build config_json from form state */
  const configJson = useMemo<Record<string, unknown>>(() => {
    return {
      target_entity_types: Array.from(entityTypes),
      confidence_threshold: confidence,
      dedup_anchors: Array.from(dedupAnchors),
      sampling: {
        rate_percent: samplingRate,
        min: samplingMin,
        max: samplingMax,
      },
      storage_policy: storagePolicy,
      export_fields: exportFields,
    }
  }, [entityTypes, confidence, dedupAnchors, samplingRate, samplingMin, samplingMax, storagePolicy, exportFields])

  /** Raw JSON string for display */
  const configJsonStr = useMemo(() => JSON.stringify(configJson, null, 2), [configJson])

  /** Handle submission */
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!pcName.trim()) return
    setCreating(true)
    setError(null)
    try {
      let finalConfig: Record<string, unknown>
      if (rawJsonMode && rawJsonOverride.trim()) {
        try {
          finalConfig = JSON.parse(rawJsonOverride) as Record<string, unknown>
        } catch {
          setError("Invalid JSON in raw config editor")
          setCreating(false)
          return
        }
      } else {
        finalConfig = configJson
      }
      await createProtocolConfig(projectId, {
        name: pcName.trim(),
        base_protocol_id: baseProtocol === "custom" ? null : (baseProtocol || null),
        config_json: finalConfig,
      })
      onCreated()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create protocol config")
    } finally {
      setCreating(false)
    }
  }

  return (
    <Card>
      <CardContent className="pt-4">
        <form onSubmit={handleSubmit} className="space-y-5">
          {error && (
            <div className="rounded-md bg-red-50 border border-red-200 px-4 py-2 text-sm text-red-800">
              {error}
            </div>
          )}

          {/* --- Name --- */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Name *</label>
            <input
              type="text"
              placeholder="e.g. HIPAA Strict Config"
              value={pcName}
              onChange={(e) => setPcName(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              required
            />
          </div>

          {/* --- 1. Base Protocol Dropdown --- */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Base Protocol</label>
            <p className="text-xs text-muted-foreground">
              Selecting a base protocol pre-populates all fields below with sensible defaults.
            </p>
            <select
              value={baseProtocol}
              onChange={(e) => {
                const val = e.target.value as BaseProtocolId | ""
                if (val) handleBaseProtocolChange(val)
                else setBaseProtocol("")
              }}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm"
            >
              <option value="">-- Select a base protocol --</option>
              {BASE_PROTOCOLS.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
          </div>

          {/* --- 2. Target Entity Types --- */}
          <div className="space-y-3">
            <label className="text-sm font-medium">Target Entity Types</label>
            <p className="text-xs text-muted-foreground">
              Select the PII entity types this protocol will detect and process.
            </p>
            {ENTITY_TYPE_GROUPS.map((group) => (
              <div key={group.category} className="space-y-2">
                <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                  {group.category}
                </p>
                <div className="flex flex-wrap gap-x-4 gap-y-1.5">
                  {group.types.map((t) => (
                    <label key={t.id} className="flex items-center gap-1.5 text-sm cursor-pointer">
                      <input
                        type="checkbox"
                        checked={entityTypes.has(t.id)}
                        onChange={() => toggleEntityType(t.id)}
                        className="rounded border-gray-300 text-primary focus:ring-primary h-3.5 w-3.5"
                      />
                      {t.label}
                    </label>
                  ))}
                </div>
              </div>
            ))}
          </div>

          {/* --- 3. Confidence Threshold Slider --- */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">
              Confidence Threshold:{" "}
              <span className="font-mono text-primary">{confidence.toFixed(2)}</span>
            </label>
            <p className="text-xs text-muted-foreground">
              Minimum confidence score (0.50 - 1.00) for an entity to be accepted.
            </p>
            <div className="flex items-center gap-3">
              <span className="text-xs text-muted-foreground">0.50</span>
              <input
                type="range"
                min="0.50"
                max="1.00"
                step="0.01"
                value={confidence}
                onChange={(e) => setConfidence(parseFloat(e.target.value))}
                className="flex-1 h-2 rounded-lg appearance-none cursor-pointer accent-primary"
              />
              <span className="text-xs text-muted-foreground">1.00</span>
            </div>
          </div>

          {/* --- 4. Dedup Anchors --- */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Deduplication Anchors</label>
            <p className="text-xs text-muted-foreground">
              Fields used as anchor points for entity resolution and deduplication.
            </p>
            <div className="flex flex-wrap gap-x-4 gap-y-1.5">
              {DEDUP_ANCHORS.map((anchor) => (
                <label key={anchor} className="flex items-center gap-1.5 text-sm cursor-pointer capitalize">
                  <input
                    type="checkbox"
                    checked={dedupAnchors.has(anchor)}
                    onChange={() => toggleAnchor(anchor)}
                    className="rounded border-gray-300 text-primary focus:ring-primary h-3.5 w-3.5"
                  />
                  {anchor}
                </label>
              ))}
            </div>
          </div>

          {/* --- 5. Sampling Config --- */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Sampling Configuration</label>
            <p className="text-xs text-muted-foreground">
              QC sampling parameters for human review.
            </p>
            <div className="grid grid-cols-3 gap-3">
              <div className="space-y-1">
                <label className="text-xs text-muted-foreground">Rate (%)</label>
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={samplingRate}
                  onChange={(e) => setSamplingRate(Math.max(1, Math.min(100, parseInt(e.target.value) || 1)))}
                  className="w-full rounded-md border bg-background px-3 py-1.5 text-sm"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs text-muted-foreground">Min</label>
                <input
                  type="number"
                  min={1}
                  value={samplingMin}
                  onChange={(e) => setSamplingMin(Math.max(1, parseInt(e.target.value) || 1))}
                  className="w-full rounded-md border bg-background px-3 py-1.5 text-sm"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs text-muted-foreground">Max</label>
                <input
                  type="number"
                  min={1}
                  value={samplingMax}
                  onChange={(e) => setSamplingMax(Math.max(1, parseInt(e.target.value) || 1))}
                  className="w-full rounded-md border bg-background px-3 py-1.5 text-sm"
                />
              </div>
            </div>
          </div>

          {/* --- 6. Storage Policy --- */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Storage Policy</label>
            <p className="text-xs text-muted-foreground">
              STRICT: no raw PII stored. INVESTIGATION: encrypted PII with retention window.
            </p>
            <div className="flex gap-6">
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input
                  type="radio"
                  name="storage_policy"
                  value="strict"
                  checked={storagePolicy === "strict"}
                  onChange={() => setStoragePolicy("strict")}
                  className="text-primary focus:ring-primary h-3.5 w-3.5"
                />
                <span className="font-medium">Strict</span>
                <span className="text-xs text-muted-foreground">(hash only)</span>
              </label>
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input
                  type="radio"
                  name="storage_policy"
                  value="investigation"
                  checked={storagePolicy === "investigation"}
                  onChange={() => setStoragePolicy("investigation")}
                  className="text-primary focus:ring-primary h-3.5 w-3.5"
                />
                <span className="font-medium">Investigation</span>
                <span className="text-xs text-muted-foreground">(encrypted + retention)</span>
              </label>
            </div>
          </div>

          {/* --- 7. Export Fields (reorderable) --- */}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Export Fields</label>
            <p className="text-xs text-muted-foreground">
              Columns included in CSV exports. Drag to reorder.
            </p>
            <div className="rounded-md border divide-y">
              {exportFields.map((field, idx) => (
                <div
                  key={`${field}-${idx}`}
                  className="flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-muted/50 group"
                >
                  <GripVertical className="h-3.5 w-3.5 text-muted-foreground/50" />
                  <span className="flex-1 font-mono text-xs">{field}</span>
                  <button
                    type="button"
                    onClick={() => moveField(idx, "up")}
                    disabled={idx === 0}
                    className="p-0.5 rounded hover:bg-accent disabled:opacity-20"
                    title="Move up"
                  >
                    <ChevronUp className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => moveField(idx, "down")}
                    disabled={idx === exportFields.length - 1}
                    className="p-0.5 rounded hover:bg-accent disabled:opacity-20"
                    title="Move down"
                  >
                    <ChevronDown className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => removeField(idx)}
                    className="p-0.5 rounded hover:bg-red-100 text-muted-foreground hover:text-red-600 opacity-0 group-hover:opacity-100 transition-opacity"
                    title="Remove field"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
            </div>
            <div className="flex gap-2 mt-1.5">
              <input
                type="text"
                placeholder="Add custom field..."
                value={newFieldInput}
                onChange={(e) => setNewFieldInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault()
                    addField()
                  }
                }}
                className="flex-1 rounded-md border bg-background px-3 py-1.5 text-sm"
              />
              <button
                type="button"
                onClick={addField}
                disabled={!newFieldInput.trim()}
                className="rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-40"
              >
                Add
              </button>
            </div>
          </div>

          {/* --- 8. Show Raw JSON toggle --- */}
          <div className="border-t pt-4 space-y-2">
            <button
              type="button"
              onClick={() => {
                const next = !showRawJson
                setShowRawJson(next)
                if (next && !rawJsonMode) {
                  setRawJsonOverride(configJsonStr)
                }
              }}
              className="flex items-center gap-2 text-xs font-medium text-muted-foreground hover:text-foreground"
            >
              <Code className="h-3.5 w-3.5" />
              {showRawJson ? "Hide raw JSON" : "Show raw JSON"}
            </button>

            {showRawJson && (
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <label className="flex items-center gap-1.5 text-xs cursor-pointer">
                    <input
                      type="checkbox"
                      checked={rawJsonMode}
                      onChange={(e) => {
                        setRawJsonMode(e.target.checked)
                        if (e.target.checked) {
                          setRawJsonOverride(configJsonStr)
                        }
                      }}
                      className="rounded border-gray-300 h-3 w-3"
                    />
                    Edit raw JSON directly (overrides form fields)
                  </label>
                </div>
                <textarea
                  value={rawJsonMode ? rawJsonOverride : configJsonStr}
                  onChange={(e) => {
                    if (rawJsonMode) setRawJsonOverride(e.target.value)
                  }}
                  readOnly={!rawJsonMode}
                  className={`w-full rounded-md border px-3 py-2 text-xs font-mono min-h-[180px] ${
                    rawJsonMode
                      ? "bg-background"
                      : "bg-muted/50 text-muted-foreground cursor-default"
                  }`}
                />
              </div>
            )}
          </div>

          {/* --- Submit / Cancel --- */}
          <div className="flex gap-2 pt-2">
            <button
              type="submit"
              disabled={creating || !pcName.trim()}
              className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium disabled:opacity-50 flex items-center gap-2"
            >
              {creating && <Loader2 className="h-4 w-4 animate-spin" />}
              {creating ? "Creating..." : "Create Protocol"}
            </button>
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border px-4 py-2 text-sm font-medium hover:bg-accent"
            >
              Cancel
            </button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Protocol configs tab
// ---------------------------------------------------------------------------

function ProtocolsTab({ project }: { project: ProjectDetailType }) {
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)

  function handleCreated() {
    setShowForm(false)
    queryClient.invalidateQueries({ queryKey: ["project", project.id] })
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">Protocol Configurations</h3>
        <button
          onClick={() => setShowForm(!showForm)}
          className="rounded-md bg-primary text-primary-foreground px-3 py-1.5 text-xs font-medium flex items-center gap-1"
        >
          <Plus className="h-3 w-3" />
          Add Protocol
        </button>
      </div>

      {showForm && (
        <ProtocolCreateForm
          projectId={project.id}
          onCreated={handleCreated}
          onCancel={() => setShowForm(false)}
        />
      )}

      {project.protocols.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
          <Shield className="h-10 w-10 mb-2 opacity-40" />
          <p className="text-sm">No protocol configurations yet</p>
        </div>
      ) : (
        <div className="space-y-3">
          {project.protocols.map((pc) => (
            <ProtocolConfigCard key={pc.id} config={pc} />
          ))}
        </div>
      )}
    </div>
  )
}

function ProtocolConfigCard({ config }: { config: ProtocolConfigSummary }) {
  const [expanded, setExpanded] = useState(false)
  const style = PROTOCOL_STATUS_STYLES[config.status] ?? ""

  return (
    <div className="rounded-md border">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-4 py-3 text-sm hover:bg-accent/50 transition-colors"
      >
        <div className="flex items-center gap-2">
          {config.status === "locked" ? (
            <Lock className="h-4 w-4 text-muted-foreground" />
          ) : (
            <Shield className="h-4 w-4 text-muted-foreground" />
          )}
          <span className="font-medium">{config.name}</span>
          {config.base_protocol_id && (
            <span className="text-xs text-muted-foreground">
              ({config.base_protocol_id})
            </span>
          )}
        </div>
        <Badge variant="outline" className={`${style} text-xs`}>
          {config.status}
        </Badge>
      </button>
      {expanded && (
        <div className="border-t px-4 py-3 space-y-2">
          <div className="text-xs text-muted-foreground">
            Created {config.created_at ? formatDistanceToNow(parseISO(config.created_at), { addSuffix: true }) : "—"}
          </div>
          <div>
            <p className="text-xs font-medium text-muted-foreground mb-1">Configuration</p>
            <pre className="text-xs bg-muted/50 rounded-md p-3 overflow-x-auto max-h-64">
              {JSON.stringify(config.config_json, null, 2)}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Catalog summary tab
// ---------------------------------------------------------------------------

function CatalogTab({ projectId }: { projectId: string }) {
  const { data: catalog, isLoading, isError } = useQuery({
    queryKey: ["catalog-summary", projectId],
    queryFn: () => getCatalogSummary(projectId),
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        Loading catalog...
      </div>
    )
  }

  if (isError || !catalog) {
    return (
      <div className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-800">
        Failed to load catalog summary.
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Document Catalog</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-3 gap-4 text-center">
            <div>
              <p className="text-2xl font-bold">{catalog.total_documents}</p>
              <p className="text-xs text-muted-foreground">Total Documents</p>
            </div>
            <div>
              <p className="text-2xl font-bold text-green-600">{catalog.auto_processable}</p>
              <p className="text-xs text-muted-foreground">Auto-processable</p>
            </div>
            <div>
              <p className="text-2xl font-bold text-amber-600">{catalog.manual_review}</p>
              <p className="text-xs text-muted-foreground">Manual Review</p>
            </div>
          </div>

          {Object.keys(catalog.by_file_type).length > 0 && (
            <div>
              <p className="text-xs font-medium text-muted-foreground mb-2">By File Type</p>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(catalog.by_file_type).map(([type, count]) => (
                  <Badge key={type} variant="secondary" className="text-xs">
                    {type}: {count}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          {Object.keys(catalog.by_structure_class).length > 0 && (
            <div>
              <p className="text-xs font-medium text-muted-foreground mb-2">By Structure Class</p>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(catalog.by_structure_class).map(([cls, count]) => (
                  <Badge key={cls} variant="outline" className="text-xs">
                    {cls}: {count}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          {catalog.total_documents === 0 && (
            <p className="text-sm text-muted-foreground text-center py-4">
              No documents cataloged yet. Run a job with this project to populate the catalog.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Density tab
// ---------------------------------------------------------------------------

function DensityTab({ projectId }: { projectId: string }) {
  const { data: density, isLoading, isError } = useQuery({
    queryKey: ["density", projectId],
    queryFn: () => getDensity(projectId),
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        Loading density data...
      </div>
    )
  }

  if (isError || !density) {
    return (
      <div className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-800">
        Failed to load density data.
      </div>
    )
  }

  const ps = density.project_summary

  return (
    <div className="space-y-4">
      {/* Project-level summary */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Project Density Summary</CardTitle>
        </CardHeader>
        <CardContent>
          {ps ? (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div className="text-center">
                  <p className="text-2xl font-bold">{ps.total_entities}</p>
                  <p className="text-xs text-muted-foreground">Total Entities</p>
                </div>
                <div className="text-center">
                  <p className="text-2xl font-bold">{ps.confidence ?? "—"}</p>
                  <p className="text-xs text-muted-foreground">Confidence</p>
                </div>
              </div>

              {ps.by_category && Object.keys(ps.by_category).length > 0 && (
                <div>
                  <p className="text-xs font-medium text-muted-foreground mb-2">By Category</p>
                  <div className="flex flex-wrap gap-1.5">
                    {Object.entries(ps.by_category).map(([cat, count]) => (
                      <Badge key={cat} variant="secondary" className="text-xs">
                        {cat}: {count}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}

              {ps.by_type && Object.keys(ps.by_type).length > 0 && (
                <div>
                  <p className="text-xs font-medium text-muted-foreground mb-2">By Entity Type</p>
                  <div className="flex flex-wrap gap-1.5">
                    {Object.entries(ps.by_type).map(([type, count]) => (
                      <Badge key={type} variant="outline" className="text-xs">
                        {type}: {count}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground text-center py-4">
              No density data available. Run the density scoring task to populate.
            </p>
          )}
        </CardContent>
      </Card>

      {/* Per-document summaries */}
      {density.document_summaries.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              Per-Document Density ({density.document_summaries.length})
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="rounded-lg border">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/50">
                    <th className="px-4 py-2 text-left font-medium">Document ID</th>
                    <th className="px-4 py-2 text-right font-medium">Entities</th>
                    <th className="px-4 py-2 text-left font-medium">Confidence</th>
                    <th className="px-4 py-2 text-left font-medium">Categories</th>
                  </tr>
                </thead>
                <tbody>
                  {density.document_summaries.map((ds) => (
                    <tr key={ds.id} className="border-b last:border-0">
                      <td className="px-4 py-2 font-mono text-xs">
                        {ds.document_id ? `${ds.document_id.slice(0, 8)}...` : "—"}
                      </td>
                      <td className="px-4 py-2 text-right font-medium">
                        {ds.total_entities}
                      </td>
                      <td className="px-4 py-2">
                        <Badge variant="outline" className="text-xs">
                          {ds.confidence ?? "—"}
                        </Badge>
                      </td>
                      <td className="px-4 py-2">
                        {ds.by_category ? (
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(ds.by_category).map(([cat, count]) => (
                              <span key={cat} className="text-xs text-muted-foreground">
                                {cat}:{count}
                              </span>
                            ))}
                          </div>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Exports tab
// ---------------------------------------------------------------------------

function ExportsTab({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient()
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const { data: exports, isLoading } = useQuery({
    queryKey: ["exports", projectId],
    queryFn: () => listExports(projectId),
    refetchInterval: 15_000,
  })

  async function handleCreateExport() {
    setCreating(true)
    setError(null)
    try {
      await createExport(projectId, {})
      queryClient.invalidateQueries({ queryKey: ["exports", projectId] })
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create export")
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">CSV Exports</h3>
        <button
          onClick={handleCreateExport}
          disabled={creating}
          className="rounded-md bg-primary text-primary-foreground px-3 py-1.5 text-xs font-medium flex items-center gap-1 disabled:opacity-50"
        >
          {creating ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Plus className="h-3 w-3" />
          )}
          New Export
        </button>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 border border-red-200 px-4 py-2 text-sm text-red-800">
          {error}
        </div>
      )}

      {isLoading ? (
        <div className="flex items-center justify-center py-12 text-muted-foreground">
          <Loader2 className="h-5 w-5 animate-spin mr-2" />
          Loading exports...
        </div>
      ) : !exports || exports.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
          <Download className="h-10 w-10 mb-2 opacity-40" />
          <p className="text-sm">No exports yet</p>
          <p className="text-xs mt-1">Create an export to generate a masked CSV</p>
        </div>
      ) : (
        <div className="space-y-2">
          {exports.map((exp) => (
            <ExportJobCard key={exp.id} job={exp} projectId={projectId} />
          ))}
        </div>
      )}
    </div>
  )
}

function ExportJobCard({ job, projectId }: { job: ExportJobSummary; projectId: string }) {
  const statusStyle =
    job.status === "completed"
      ? "bg-green-100 text-green-800 border-green-300"
      : job.status === "failed"
        ? "bg-red-100 text-red-800 border-red-300"
        : "bg-yellow-100 text-yellow-800 border-yellow-300"

  const createdAgo = job.created_at
    ? formatDistanceToNow(parseISO(job.created_at), { addSuffix: true })
    : null

  return (
    <div className="rounded-md border px-4 py-3 flex items-center justify-between">
      <div className="flex items-center gap-3">
        {job.status === "completed" ? (
          <CheckCircle className="h-4 w-4 text-green-600" />
        ) : (
          <Download className="h-4 w-4 text-muted-foreground" />
        )}
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium font-mono">
              {job.id.slice(0, 8)}...
            </span>
            <Badge variant="outline" className={`${statusStyle} text-xs`}>
              {job.status}
            </Badge>
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground mt-0.5">
            {job.row_count != null && <span>{job.row_count} rows</span>}
            {createdAgo && <span>{createdAgo}</span>}
          </div>
        </div>
      </div>
      {job.status === "completed" && (
        <a
          href={getExportDownloadUrl(projectId, job.id)}
          className="rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-accent flex items-center gap-1"
          download
        >
          <Download className="h-3 w-3" />
          Download
        </a>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function ProjectDetail() {
  const { id } = useParams<{ id: string }>()
  const projectId = id ?? ""
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState<TabId>("overview")

  const { data: project, isLoading, isError } = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => getProject(projectId),
    enabled: !!projectId,
  })

  function handleUpdated() {
    queryClient.invalidateQueries({ queryKey: ["project", projectId] })
    queryClient.invalidateQueries({ queryKey: ["projects"] })
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground">
        <Loader2 className="h-6 w-6 animate-spin mr-2" />
        <span className="text-sm">Loading project...</span>
      </div>
    )
  }

  if (isError || !project) {
    return (
      <div className="space-y-4">
        <Link
          to="/projects"
          className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Projects
        </Link>
        <div className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-800">
          Project not found or failed to load.
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Breadcrumb */}
      <div className="flex items-center gap-1 text-sm text-muted-foreground">
        <Link to="/projects" className="hover:text-foreground">
          Projects
        </Link>
        <span>&gt;</span>
        <span className="text-foreground font-medium">{project.name}</span>
      </div>

      {/* Header */}
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold">{project.name}</h1>
        <Badge
          variant="outline"
          className={`${PROJECT_STATUS_STYLES[project.status] ?? ""} text-xs font-medium`}
        >
          {project.status}
        </Badge>
      </div>

      {/* Tabs */}
      <div className="flex border-b">
        {TABS.map(({ id: tabId, label, icon: Icon }) => (
          <button
            key={tabId}
            onClick={() => setActiveTab(tabId)}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === tabId
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            <Icon className="h-4 w-4" />
            {label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {activeTab === "overview" && (
        <OverviewTab project={project} onUpdated={handleUpdated} />
      )}
      {activeTab === "protocols" && <ProtocolsTab project={project} />}
      {activeTab === "catalog" && <CatalogTab projectId={projectId} />}
      {activeTab === "density" && <DensityTab projectId={projectId} />}
      {activeTab === "exports" && <ExportsTab projectId={projectId} />}
    </div>
  )
}
