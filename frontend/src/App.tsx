import { useState, createContext } from "react"
import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom"
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query"
import {
  LayoutDashboard,
  ClipboardCheck,
  Play,
  Settings as SettingsIcon,
  Shield,
  FolderOpen,
  Loader2,
  CheckCircle,
  XCircle,
} from "lucide-react"
import { getRecentJobs } from "@/api/client"
import type { JobSummary } from "@/api/client"
import { Dashboard } from "@/pages/Dashboard"
import { ReviewQueue } from "@/pages/ReviewQueue"
import { SubjectDetail } from "@/pages/SubjectDetail"
import { JobSubmit } from "@/pages/JobSubmit"
import { Settings } from "@/pages/Settings"
import { Projects } from "@/pages/Projects"
import { ProjectDetail } from "@/pages/ProjectDetail"
import { SegregationReview } from "@/pages/SegregationReview"
import { ExtractionQA } from "@/pages/ExtractionQA"

// ---------------------------------------------------------------------------
// Job ID context — shared across pages so SubjectDetail can fetch results
// ---------------------------------------------------------------------------

export const JobIdContext = createContext<string | null>(null)
export const JobIdSetterContext = createContext<(id: string) => void>(() => {})

// ---------------------------------------------------------------------------
// Query client
// ---------------------------------------------------------------------------

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 10_000 } },
})

// ---------------------------------------------------------------------------
// Navigation items (consolidated 8 → 5)
// ---------------------------------------------------------------------------

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard },
  { to: "/projects", label: "Projects", icon: FolderOpen },
  { to: "/review", label: "Review Queue", icon: ClipboardCheck },
  { to: "/jobs", label: "Jobs", icon: Play },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
]

// ---------------------------------------------------------------------------
// Pipeline Status Bar (C2 — global visibility)
// ---------------------------------------------------------------------------

function PipelineStatusBar() {
  const { data: jobs } = useQuery({
    queryKey: ["activeJobs"],
    queryFn: () => getRecentJobs(false, 10),
    refetchInterval: 3000,
  })

  const activeJobs = (jobs ?? []).filter((j) =>
    ["running", "extracting", "analyzing"].includes(j.status.toLowerCase()),
  )
  const recentFailed = (jobs ?? []).filter(
    (j) => ["failed", "error"].includes(j.status.toLowerCase()),
  ).slice(0, 1)
  const recentComplete = (jobs ?? []).filter(
    (j) => {
      if (!["completed", "complete"].includes(j.status.toLowerCase())) return false
      if (!j.completed_at) return false
      // Only show if completed in last 5 minutes
      return Date.now() - new Date(j.completed_at).getTime() < 5 * 60 * 1000
    },
  ).slice(0, 1)

  if (activeJobs.length === 0 && recentFailed.length === 0 && recentComplete.length === 0) {
    return null
  }

  return (
    <div className="px-3 pb-3 space-y-2">
      {activeJobs.map((job) => (
        <div key={job.id} className="rounded-md bg-blue-500/10 border border-blue-500/20 px-3 py-2">
          <div className="flex items-center gap-2 text-xs font-medium text-blue-700">
            <Loader2 className="h-3 w-3 animate-spin" />
            Pipeline Running
          </div>
          <p className="text-xs text-blue-600 mt-0.5 truncate">
            {job.first_file_name ?? `${job.document_count} doc${job.document_count !== 1 ? "s" : ""}`}
          </p>
        </div>
      ))}
      {recentComplete.map((job) => (
        <div key={job.id} className="rounded-md bg-green-500/10 border border-green-500/20 px-3 py-2">
          <div className="flex items-center gap-2 text-xs font-medium text-green-700">
            <CheckCircle className="h-3 w-3" />
            Complete
          </div>
          <p className="text-xs text-green-600 mt-0.5 truncate">
            {job.first_file_name ?? `${job.document_count} docs`}
          </p>
        </div>
      ))}
      {recentFailed.map((job) => (
        <div key={job.id} className="rounded-md bg-red-500/10 border border-red-500/20 px-3 py-2">
          <div className="flex items-center gap-2 text-xs font-medium text-red-700">
            <XCircle className="h-3 w-3" />
            Failed
          </div>
          <p className="text-xs text-red-600 mt-0.5 truncate">
            {job.error_summary ?? job.first_file_name ?? "Unknown error"}
          </p>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

function Sidebar() {
  // Count active jobs for badge
  const { data: badgeJobs } = useQuery({
    queryKey: ["activeJobs"],
    queryFn: () => getRecentJobs(false, 10),
    refetchInterval: 3000,
  })
  const activeCount = (badgeJobs ?? []).filter((j) =>
    ["running", "extracting", "analyzing"].includes(j.status.toLowerCase()),
  ).length

  return (
    <aside className="w-60 shrink-0 border-r bg-sidebar text-sidebar-foreground flex flex-col">
      <div className="flex items-center gap-2 px-4 py-5 border-b">
        <Shield className="h-6 w-6 text-primary" />
        <span className="text-lg font-bold">Forentis AI</span>
      </div>
      <nav className="flex-1 px-2 py-3 space-y-1">
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            className={({ isActive }) =>
              `flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors ${
                isActive
                  ? "bg-sidebar-accent text-sidebar-accent-foreground font-bold border-l-2 border-primary"
                  : "text-sidebar-foreground hover:bg-sidebar-accent/50"
              }`
            }
          >
            <Icon className="h-4 w-4" />
            {label}
            {to === "/jobs" && activeCount > 0 && (
              <span className="ml-auto inline-flex items-center justify-center h-5 min-w-[20px] px-1 rounded-full bg-blue-500 text-white text-xs font-bold">
                {activeCount}
              </span>
            )}
          </NavLink>
        ))}
      </nav>
      <PipelineStatusBar />
    </aside>
  )
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export default function App() {
  const [jobId, setJobId] = useState<string | null>(null)

  return (
    <QueryClientProvider client={queryClient}>
      <JobIdContext.Provider value={jobId}>
        <JobIdSetterContext.Provider value={setJobId}>
          <BrowserRouter>
            <div className="flex h-screen">
              <Sidebar />
              <main className="flex-1 overflow-y-auto p-8">
                <Routes>
                  <Route path="/" element={<Dashboard />} />
                  <Route path="/projects" element={<Projects />} />
                  <Route path="/projects/:id" element={<ProjectDetail />} />
                  <Route path="/projects/:id/segregation" element={<SegregationReview />} />
                  <Route path="/projects/:id/qa" element={<ExtractionQA />} />
                  <Route path="/review" element={<ReviewQueue />} />
                  <Route path="/subjects/:id" element={<SubjectDetail />} />
                  <Route path="/jobs" element={<JobSubmit />} />
                  <Route path="/settings" element={<Settings />} />
                </Routes>
              </main>
            </div>
          </BrowserRouter>
        </JobIdSetterContext.Provider>
      </JobIdContext.Provider>
    </QueryClientProvider>
  )
}
