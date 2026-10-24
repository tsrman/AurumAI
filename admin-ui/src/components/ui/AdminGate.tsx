"use client";

import { ShieldAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useAdminAccess } from "@/hooks/useAdminAccess";

/** Renders its page only for organization administrators. */
export function AdminGate({ children }: { children: (access: { canWrite: boolean }) => React.ReactNode }) {
  const t = useTranslations("access");
  const access = useAdminAccess();
  if (access.isLoading) {
    return <p role="status" className="p-6 text-sm text-aurum-text-3">{t("checking")}</p>;
  }
  if (!access.allowed) {
    return (
      <div className="m-6 flex gap-3 rounded-card border border-transparent bg-aurum-err-bg p-6 text-sm text-aurum-err-fg" role="alert">
        <ShieldAlert className="h-5 w-5 shrink-0" aria-hidden="true" />
        <span>{access.error || t("adminRequired")}</span>
      </div>
    );
  }
  return <>{children({ canWrite: access.canWrite })}</>;
}
