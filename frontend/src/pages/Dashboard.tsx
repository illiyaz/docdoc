import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { getDashboardSummary } from "@/api/client"
import type { DashboardSummary } from "@/api/client"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import {
  FolderOpen,
  AlertCircle,
  Briefcase,
  FileText,
  Clock,
  CheckCircle2,
  Download,
  Plus,
  Loader2,
  ArrowRight,
} from "lucide-react"

function relativeTime(iso: string | null): string {
  if (!iso) return "—"
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return "just now"
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

const ACTIVITY_ICONS: Record<string, typeof CheckCircle2> = {
  job_completed: CheckCircle2,
  document_reviewed: FileText,
  export_completed: Download,
  project_created: Plus,
}

export function Dashboard() {
  const navigate = useNavigate()

  const { data, isLoading, isError } = useQuery<DashboardSummary>({
    queryKey: ["dashboard-summary"],
    queryFn: getDashboardSummary,
    refetchInterval: (query) => {
      const d = query.state.data as DashboardSummary | undefined
      return d?.running_jobs?.length ? 10_000 : 30_000
    },
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (isError || !data) {
    return (
      <div className="text-center py-12 text-muted-foreground">
        Failed to load dashboard. Check server connection.
      </div>
    )
  }

  const { stats, needs_attention, running_jobs, active_projects, recent_activity } = data

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold">Forentis AI — Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">Command center overview</p>
      </div>

      {/* --- Stat Cards --- */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <Card
          className="cursor-pointer hover:border-primary/40 transition-colors"
          onClick={() => navigate("/projects")}
        >
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground flex items-center gap-2">
              <FolderOpen className="w-4 h-4" /> Active Projects
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">{stats.active_projects}</div>
          </CardContent>
        </Card>

        <Card
          className="cursor-pointer hover:border-primary/40 transition-colors"
          onClick={() => {
            if (needs_attention.length > 0) {
              navigate(`/projects/${needs_attention[0].project_id}`)
            }
          }}
        >
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground flex items-center gap-2">
              <AlertCircle className="w-4 h-4" /> Pending Reviews
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">{stats.pending_reviews}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground flex items-center gap-2">
              <Briefcase className="w-4 h-4" /> Jobs This Week
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">{stats.jobs_this_week}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground flex items-center gap-2">
              <FileText className="w-4 h-4" /> Documents Processed
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-3xl font-bold">{stats.documents_processed}</div>
          </CardContent>
        </Card>
      </div>

      {/* --- Needs Attention --- */}
      {needs_attention.length > 0 && (
        <div>
          <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
            <AlertCircle className="w-5 h-5 text-amber-500" />
            Needs Attention
          </h2>
          <div className="space-y-2">
            {needs_attention.map((item) => (
              <div
                key={item.project_id}
                className="flex items-center justify-between rounded-lg border px-4 py-3 hover:bg-accent/50 transition-colors"
              >
                <div>
                  <span className="font-medium">{item.project_name}</span>
                  <span className="text-sm text-muted-foreground ml-3">
                    {item.pending_count} pending review{item.pending_count !== 1 ? "s" : ""}
                  </span>
                  {item.oldest_pending_at && (
                    <span className="text-xs text-muted-foreground ml-2">
                      (oldest: {relativeTime(item.oldest_pending_at)})
                    </span>
                  )}
                </div>
                <button
                  className="text-sm font-medium text-primary hover:underline flex items-center gap-1"
                  onClick={() => navigate(`/projects/${item.project_id}`)}
                >
                  Review <ArrowRight className="w-3 h-3" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {needs_attention.length === 0 && stats.pending_reviews === 0 && (
        <div className="rounded-lg border px-4 py-3 text-sm text-muted-foreground">
          All clear — no documents waiting for review
        </div>
      )}

      {/* --- Running Jobs --- */}
      {running_jobs.length > 0 && (
        <div>
          <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
            <Loader2 className="w-5 h-5 animate-spin text-blue-500" />
            Running Jobs
          </h2>
          <div className="space-y-2">
            {running_jobs.map((job) => (
              <div
                key={job.job_id}
                className="flex items-center justify-between rounded-lg border px-4 py-3"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium truncate">
                      {job.project_name || "Unlinked job"}
                    </span>
                    <Badge variant="outline" className="text-xs">{job.status}</Badge>
                  </div>
                  <div className="text-xs text-muted-foreground mt-1">
                    {job.document_count} docs · started {relativeTime(job.started_at)}
                  </div>
                </div>
                <div className="flex items-center gap-3 ml-4">
                  <div className="w-24 bg-secondary rounded-full h-2">
                    <div
                      className="bg-primary rounded-full h-2 transition-all"
                      style={{ width: `${Math.min(job.progress_pct, 100)}%` }}
                    />
                  </div>
                  <span className="text-xs font-mono w-10 text-right">{job.progress_pct}%</span>
                  {job.project_id && (
                    <button
                      className="text-xs text-primary hover:underline"
                      onClick={() => navigate(`/projects/${job.project_id}`)}
                    >
                      View
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* --- Active Projects + Recent Activity side by side --- */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Active Projects */}
        <div>
          <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
            <FolderOpen className="w-5 h-5" /> Active Projects
          </h2>
          {active_projects.length === 0 ? (
            <div className="rounded-lg border px-4 py-6 text-center">
              <p className="text-sm text-muted-foreground mb-3">Create your first project</p>
              <button
                className="inline-flex items-center gap-1 px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90"
                onClick={() => navigate("/projects")}
              >
                <Plus className="w-4 h-4" /> New Project
              </button>
            </div>
          ) : (
            <div className="space-y-2">
              {active_projects.map((proj) => (
                <div
                  key={proj.id}
                  className="flex items-center justify-between rounded-lg border px-4 py-3 cursor-pointer hover:bg-accent/50 transition-colors"
                  onClick={() => navigate(`/projects/${proj.id}`)}
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium truncate">{proj.name}</span>
                      {proj.pending_reviews > 0 && (
                        <Badge variant="destructive" className="text-xs">
                          {proj.pending_reviews} pending
                        </Badge>
                      )}
                    </div>
                    <div className="text-xs text-muted-foreground mt-1">
                      {proj.document_count} docs · {proj.completed_jobs} job{proj.completed_jobs !== 1 ? "s" : ""} completed
                    </div>
                  </div>
                  <div className="text-xs text-muted-foreground flex items-center gap-1">
                    <Clock className="w-3 h-3" />
                    {relativeTime(proj.last_activity_at)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Recent Activity */}
        <div>
          <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
            <Clock className="w-5 h-5" /> Recent Activity
          </h2>
          {recent_activity.length === 0 ? (
            <div className="rounded-lg border px-4 py-6 text-center text-sm text-muted-foreground">
              No recent activity
            </div>
          ) : (
            <div className="space-y-1">
              {recent_activity.map((event, idx) => {
                const Icon = ACTIVITY_ICONS[event.type] || FileText
                return (
                  <div
                    key={idx}
                    className="flex items-start gap-3 rounded-lg px-3 py-2 text-sm"
                  >
                    <Icon className="w-4 h-4 mt-0.5 text-muted-foreground shrink-0" />
                    <div className="min-w-0 flex-1">
                      <span className="text-muted-foreground">
                        {event.project_name && (
                          <span className="font-medium text-foreground">{event.project_name}</span>
                        )}
                        {event.project_name ? " — " : ""}
                        {event.detail}
                      </span>
                    </div>
                    <span className="text-xs text-muted-foreground whitespace-nowrap">
                      {relativeTime(event.timestamp)}
                    </span>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
