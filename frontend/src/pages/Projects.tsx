import { useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { Plus, FolderOpen, Loader2 } from "lucide-react"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { listProjects, createProject } from "@/api/client"
import type { ProjectSummary } from "@/api/client"
import { formatDistanceToNow, parseISO } from "date-fns"

// ---------------------------------------------------------------------------
// Status badge helper
// ---------------------------------------------------------------------------

const STATUS_STYLES: Record<string, string> = {
  active: "bg-green-100 text-green-800 border-green-300",
  archived: "bg-gray-100 text-gray-600 border-gray-300",
  completed: "bg-blue-100 text-blue-800 border-blue-300",
}

function ProjectStatusBadge({ status }: { status: string }) {
  const style = STATUS_STYLES[status] ?? "bg-gray-100 text-gray-800 border-gray-300"
  return (
    <Badge variant="outline" className={`${style} text-xs font-medium`}>
      {status}
    </Badge>
  )
}

// ---------------------------------------------------------------------------
// Create project form
// ---------------------------------------------------------------------------

function CreateProjectForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [createdBy, setCreatedBy] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    setLoading(true)
    setError(null)
    try {
      await createProject({
        name: name.trim(),
        description: description.trim() || null,
        created_by: createdBy.trim() || null,
      })
      setName("")
      setDescription("")
      setCreatedBy("")
      onCreated()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create project")
    } finally {
      setLoading(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Plus className="h-4 w-4" />
          Create New Project
        </CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-3">
          {error && (
            <div className="rounded-md bg-red-50 border border-red-200 px-4 py-2 text-sm text-red-800">
              {error}
            </div>
          )}
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Project Name *</label>
            <input
              type="text"
              placeholder="e.g. ACME Corp Breach Investigation"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm"
              required
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Description</label>
            <textarea
              placeholder="Brief description of the engagement..."
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm min-h-[60px]"
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Created By</label>
            <input
              type="text"
              placeholder="Your name or ID"
              value={createdBy}
              onChange={(e) => setCreatedBy(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm"
            />
          </div>
          <button
            type="submit"
            disabled={loading || !name.trim()}
            className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium disabled:opacity-50 flex items-center gap-2"
          >
            {loading ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                Creating...
              </>
            ) : (
              <>
                <Plus className="h-4 w-4" />
                Create Project
              </>
            )}
          </button>
        </form>
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Project card
// ---------------------------------------------------------------------------

function ProjectCard({
  project,
  onClick,
}: {
  project: ProjectSummary
  onClick: () => void
}) {
  const createdAgo = project.created_at
    ? formatDistanceToNow(parseISO(project.created_at), { addSuffix: true })
    : null

  return (
    <button
      onClick={onClick}
      className="w-full text-left rounded-xl border bg-card p-5 shadow-sm transition-colors hover:bg-accent/50 focus:outline-none focus:ring-2 focus:ring-ring"
    >
      <div className="flex items-start justify-between gap-2 mb-2">
        <h3 className="text-sm font-semibold leading-tight">{project.name}</h3>
        <ProjectStatusBadge status={project.status} />
      </div>
      {project.description && (
        <p className="text-xs text-muted-foreground line-clamp-2 mb-3">
          {project.description}
        </p>
      )}
      <div className="flex items-center gap-3 text-xs text-muted-foreground">
        {project.created_by && <span>by {project.created_by}</span>}
        {createdAgo && <span>{createdAgo}</span>}
      </div>
    </button>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function Projects() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [showForm, setShowForm] = useState(false)

  const { data: projects, isLoading, isError } = useQuery({
    queryKey: ["projects"],
    queryFn: listProjects,
    refetchInterval: 30_000,
  })

  function handleCreated() {
    setShowForm(false)
    queryClient.invalidateQueries({ queryKey: ["projects"] })
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Projects</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Manage breach investigation engagements
          </p>
        </div>
        <button
          onClick={() => setShowForm(!showForm)}
          className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium flex items-center gap-2"
        >
          <Plus className="h-4 w-4" />
          New Project
        </button>
      </div>

      {showForm && <CreateProjectForm onCreated={handleCreated} />}

      {isLoading ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin mr-2" />
          <span className="text-sm">Loading projects...</span>
        </div>
      ) : isError ? (
        <div className="rounded-md bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-800">
          Failed to load projects. Is the API server running?
        </div>
      ) : !projects || projects.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
          <FolderOpen className="h-12 w-12 mb-3 opacity-40" />
          <p className="text-sm">No projects yet</p>
          <p className="text-xs mt-1">Create a project to get started</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {projects.map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              onClick={() => navigate(`/projects/${project.id}`)}
            />
          ))}
        </div>
      )}
    </div>
  )
}
