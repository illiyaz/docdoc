import {
  Bot,
  User,
  AlertTriangle,
  Scale,
  CheckCircle,
  Mail,
  GitMerge,
  Shield,
} from "lucide-react"
import { formatDistanceToNow, parseISO } from "date-fns"
import type { AuditEvent } from "@/api/client"

const EVENT_ICONS: Record<string, typeof Bot> = {
  ai_extraction: Bot,
  human_review: User,
  escalation: AlertTriangle,
  legal_review: Scale,
  approval: CheckCircle,
  notification_sent: Mail,
  rra_merge: GitMerge,
  protocol_applied: Shield,
}

export function AuditTimeline({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-muted-foreground">No audit events yet.</p>
  }

  return (
    <div className="space-y-4">
      {events.map((ev, i) => {
        const Icon = EVENT_ICONS[ev.event_type] ?? Bot
        const ts = ev.timestamp
          ? formatDistanceToNow(parseISO(ev.timestamp), { addSuffix: true })
          : ""

        return (
          <div key={i} className="flex gap-3 items-start">
            <div className="mt-0.5 rounded-full bg-muted p-1.5">
              <Icon className="h-4 w-4 text-muted-foreground" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">
                  {ev.event_type.replace(/_/g, " ")}
                </span>
                <span className="text-xs text-muted-foreground">{ts}</span>
              </div>
              <p className="text-xs text-muted-foreground">
                by {ev.actor}
                {ev.decision ? ` \u2014 ${ev.decision}` : ""}
              </p>
            </div>
          </div>
        )
      })}
    </div>
  )
}
