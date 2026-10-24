import { Search } from "lucide-react";
import { cn } from "@/lib/utils";

export interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  className?: string;
}

export function SearchInput({ value, onChange, placeholder, className }: SearchInputProps) {
  return (
    <label className={cn("relative block w-full max-w-sm", className)}>
      <span className="sr-only">{placeholder}</span>
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-aurum-text-3" aria-hidden="true" />
      <input
        type="search"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="w-full rounded-input border border-aurum-border bg-aurum-surface py-2 pl-9 pr-3 text-sm text-aurum-text outline-none transition placeholder:text-aurum-text-3 focus:border-aurum-accent focus:ring-2 focus:ring-aurum-accent/20"
      />
    </label>
  );
}
