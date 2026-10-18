"use client";

import { useAuth } from "@/providers/AuthProvider";
import { useTranslations } from "next-intl";

export function useAdminAccess() {
  const t = useTranslations("access");
  const { user, isLoading } = useAuth();
  const isOrgAdmin = user?.role === "org_admin";

  return {
    allowed: isOrgAdmin,
    canWrite: isOrgAdmin,
    error: isOrgAdmin ? null : t("adminRequired"),
    isLoading,
  };
}
