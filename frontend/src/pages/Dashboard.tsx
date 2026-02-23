import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { getQueues, getRecentAudit } from "@/api/client"
import { QueueCard } from "@/components/QueueCard"
import { AuditTimeline } from "@/components/AuditTimeline"

const QUEUE_ORDER = ["low_confidence", "escalation", "qc_sampling", "rra_review"]

export function Dashboard() {
  const navigate = useNavigate()

  const queues = useQuery({
    queryKey: ["queues"],
    queryFn: getQueues,
    refetchInterval: 30_000,
  })

  const audit = useQuery({
    queryKey: ["audit-recent"],
    queryFn: getRecentAudit,
    refetchInterval: 30_000,
  })

  const queueData = queues.data ?? {}
  const totalPending = Object.values(queueData).reduce((a, b) => a + b, 0)

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold">Cyber NotifAI — Review Dashboard</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {totalPending} pending task{totalPending !== 1 ? "s" : ""} across all queues
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {QUEUE_ORDER.map((qt) => (
          <QueueCard
            key={qt}
            queueType={qt}
            count={queueData[qt] ?? 0}
            onClick={() => navigate(`/queues/${qt}`)}
          />
        ))}
      </div>

      <div>
        <h2 className="text-lg font-semibold mb-4">Recent Audit Events</h2>
        {audit.isLoading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : audit.isError ? (
          <p className="text-sm text-destructive">Failed to load audit events.</p>
        ) : (
          <AuditTimeline events={audit.data ?? []} />
        )}
      </div>
    </div>
  )
}
