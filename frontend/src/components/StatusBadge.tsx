import { Badge } from "@/components/ui/badge"

const STATUS_COLORS: Record<string, string> = {
  AI_PENDING: "bg-gray-100 text-gray-800 border-gray-300",
  HUMAN_REVIEW: "bg-yellow-100 text-yellow-800 border-yellow-300",
  LEGAL_REVIEW: "bg-orange-100 text-orange-800 border-orange-300",
  APPROVED: "bg-green-100 text-green-800 border-green-300",
  NOTIFIED: "bg-blue-100 text-blue-800 border-blue-300",
  REJECTED: "bg-red-100 text-red-800 border-red-300",
}

export function StatusBadge({ status }: { status: string }) {
  const colors = STATUS_COLORS[status] ?? "bg-gray-100 text-gray-800 border-gray-300"
  return (
    <Badge variant="outline" className={`${colors} font-medium`}>
      {status.replace(/_/g, " ")}
    </Badge>
  )
}
