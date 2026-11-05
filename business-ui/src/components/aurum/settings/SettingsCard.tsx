import type { ReactNode } from "react";

/** A titled card on the settings page; every settings block uses it so the
 * page reads as one surface of the design system. */
export function SettingsCard({
  id,
  title,
  description,
  children,
}: {
  id?: string;
  title?: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section
      aria-labelledby={title && id ? id : undefined}
      className="space-y-4 rounded-card border border-aurum-border-soft bg-aurum-surface p-5 shadow-card"
    >
      {title ? (
        <div>
          <h2 id={id} className="text-sm font-semibold text-aurum-text">
            {title}
          </h2>
          {description ? <p className="mt-1 text-sm text-aurum-text-3">{description}</p> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}
