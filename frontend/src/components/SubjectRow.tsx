import { PIIBadge } from "@/components/PIIBadge"
import { StatusBadge } from "@/components/StatusBadge"
import type { MaskedSubject } from "@/api/client"

interface SubjectRowProps {
  subject: MaskedSubject
  onSelect: (id: string) => void
}

export function SubjectRow({ subject, onSelect }: SubjectRowProps) {
  const types = subject.pii_types_found ?? []
  const shown = types.slice(0, 3)
  const extra = types.length - 3

  return (
    <div className="flex items-center gap-4 rounded-lg border px-4 py-3">
      <span className="text-xs font-mono text-muted-foreground w-24 shrink-0">
        {subject.subject_id.slice(0, 8)}&hellip;
      </span>

      <div className="flex flex-wrap gap-1 flex-1 min-w-0">
        {shown.map((t) => (
          <PIIBadge key={t} type={t} />
        ))}
        {extra > 0 && (
          <span className="text-xs text-muted-foreground self-center">
            +{extra} more
          </span>
        )}
      </div>

      <StatusBadge status={subject.review_status} />

      <span
        className={`text-xs font-medium ${
          subject.notification_required
            ? "text-orange-600"
            : "text-muted-foreground"
        }`}
      >
        {subject.notification_required ? "Yes" : "No"}
      </span>

      <button
        onClick={() => onSelect(subject.subject_id)}
        className="text-xs font-medium text-primary hover:underline shrink-0"
      >
        View
      </button>
    </div>
  )
}
