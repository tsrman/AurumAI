"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/primitives";

export function RoleBadge({ role }: { role: string }) {
  const t = useTranslations("people.roles");
  return role === "org_admin" ? <Badge color="violet">{t("org_admin")}</Badge> : <Badge color="blue">{role === "member" ? t("member") : role}</Badge>;
}

export function StatusBadge({ status }: { status: string }) {
  const t = useTranslations("people.statuses");
  const known = status === "active" || status === "inactive" || status === "suspended";
  return (
    <Badge color={status === "active" ? "green" : "rose"}>{known ? t(status) : status}</Badge>
  );
}
