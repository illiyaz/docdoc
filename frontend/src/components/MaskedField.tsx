interface MaskedFieldProps {
  label: string
  value: string | null
}

export function MaskedField({ label, value }: MaskedFieldProps) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      {value ? (
        <p className="text-sm" title="Value masked for security">
          {value}
        </p>
      ) : (
        <p className="text-sm text-muted-foreground">&mdash;</p>
      )}
    </div>
  )
}
