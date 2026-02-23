import { Badge } from "@/components/ui/badge"

const PHI_TYPES = new Set([
  "medical_record", "mrn", "hicn", "npi", "dea", "health_plan", "icd10",
  "nhs_number",
])
const FINANCIAL_TYPES = new Set([
  "credit_card", "financial_account", "routing_number", "iban",
])
const GOV_ID_TYPES = new Set([
  "ssn", "us_ssn", "aadhaar", "passport", "drivers_license", "government_id",
])
const CONTACT_TYPES = new Set(["email", "phone", "address"])

function categoryColor(type: string): string {
  const t = type.toLowerCase()
  if (PHI_TYPES.has(t)) return "bg-red-50 text-red-700 border-red-200"
  if (FINANCIAL_TYPES.has(t)) return "bg-orange-50 text-orange-700 border-orange-200"
  if (GOV_ID_TYPES.has(t)) return "bg-yellow-50 text-yellow-700 border-yellow-200"
  if (CONTACT_TYPES.has(t)) return "bg-blue-50 text-blue-700 border-blue-200"
  return "bg-gray-50 text-gray-700 border-gray-200"
}

export function PIIBadge({ type }: { type: string }) {
  return (
    <Badge variant="outline" className={`${categoryColor(type)} text-xs`}>
      {type.toUpperCase()}
    </Badge>
  )
}
