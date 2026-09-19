import { Icon } from "../atoms/Icon"
import { TextField } from "../atoms/TextField"

interface SearchFieldProps {
  placeholder: string
  value: string
  onChange: (value: string) => void
}

export function SearchField({ placeholder, value, onChange }: SearchFieldProps) {
  return (
    <div className="group flex items-center rounded-xl bg-surface-cream px-3 py-2.5 shadow-[0_1px_4px_rgba(41,37,36,0.03)] transition-shadow focus-within:shadow-[0_0_0_2px_rgba(234,88,12,0.2)]">
      <Icon
        name="search"
        size={20}
        className="mr-2 text-text-warm-muted transition-colors group-focus-within:text-accent-terracotta"
      />
      <TextField placeholder={placeholder} value={value} onChange={(event) => onChange(event.target.value)} />
    </div>
  )
}
