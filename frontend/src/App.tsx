import { useState, createContext } from "react"
import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  LayoutDashboard,
  ClipboardCheck,
  Play,
  Settings as SettingsIcon,
  Shield,
  FolderOpen,
} from "lucide-react"
import { Dashboard } from "@/pages/Dashboard"
import { ReviewQueue } from "@/pages/ReviewQueue"
import { SubjectDetail } from "@/pages/SubjectDetail"
import { JobSubmit } from "@/pages/JobSubmit"
import { Settings } from "@/pages/Settings"
import { Projects } from "@/pages/Projects"
import { ProjectDetail } from "@/pages/ProjectDetail"

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
// Sidebar
// ---------------------------------------------------------------------------

function Sidebar() {
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
          </NavLink>
        ))}
      </nav>
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
