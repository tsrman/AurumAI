"use client";

import { useTranslations } from "next-intl";
import { LinkTabs } from "@/components/ui/Tabs";

/** Users | Groups switcher shared by both people pages. */
export function PeopleTabs() {
  const t = useTranslations("people");
  return (
    <LinkTabs
      label={t("tabsLabel")}
      tabs={[
        { href: "/admin/users", label: t("tabs.users") },
        { href: "/admin/groups", label: t("tabs.groups") },
      ]}
    />
  );
}
