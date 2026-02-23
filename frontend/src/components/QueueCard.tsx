import { Badge } from "@/components/ui/badge"

const QUEUE_LABELS: Record<string, string> = {
  low_confidence: "Low Confidence",
  escalation: "Escalation",
  qc_sampling: "QC Sampling",
  rra_review: "RRA Review",
}

const QUEUE_ROLES: Record<string, string> = {
  low_confidence: "REVIEWER",
  escalation: "LEGAL_REVIEWER",
  qc_sampling: "QC_SAMPLER",
  rra_review: "REVIEWER",
}

interface QueueCardProps {
  queueType: string
  count: number
  onClick: () => void
}

export function QueueCard({ queueType, count, onClick }: QueueCardProps) {
  const label = QUEUE_LABELS[queueType] ?? queueType
  const role = QUEUE_ROLES[queueType] ?? "REVIEWER"

  return (
    <button
      onClick={onClick}
      className="w-full text-left rounded-xl border bg-card p-6 shadow-sm transition-colors hover:bg-accent focus:outline-none focus:ring-2 focus:ring-ring"
    >
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-muted-foreground">{label}</h3>
        <Badge variant="outline" className="text-xs">{role}</Badge>
      </div>
      <p className="text-3xl font-bold">{count}</p>
      <p className="text-xs text-muted-foreground mt-1">pending tasks</p>
    </button>
  )
}
