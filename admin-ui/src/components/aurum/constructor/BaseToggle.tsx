import { Toggle } from "@/components/primitives/Toggle";

export interface BaseToggleProps {
  name: string;
  description: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}

export function BaseToggle({ name, description, checked, onChange }: BaseToggleProps) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="text-sm font-medium text-aurum-text">{name}</p>
        <p className="text-xs text-aurum-text-3">{description}</p>
      </div>
      <Toggle checked={checked} onChange={onChange} aria-label={name} />
    </div>
  );
}
