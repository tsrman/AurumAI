"use client";

import { useAuth } from "@/providers/AuthProvider";
import { useTranslations } from "next-intl";

export function LogoutButton() {
  const t = useTranslations("common");
  const { logout } = useAuth();
  return (
    <button type="button" onClick={logout} className="text-sm font-medium hover:text-primary-600">
      {t("logout")}
    </button>
  );
}
