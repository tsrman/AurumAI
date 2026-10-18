import Link from "next/link";
import { ThemeToggle } from "@/components/aurum/ThemeToggle";
import { getServerRuntimeConfig } from "@/lib/runtime-config.server";
import { LogoutButton } from "@/components/LogoutButton";
import { useTranslations } from "next-intl";
import { LanguageSwitcher } from "@/components/LanguageSwitcher";

// Mirror of business-ui's adminUrl (Sidebar.tsx), pointing the other way.
// admin-ui is a separate app on its own origin, so this is an absolute
// cross-origin URL, not a Next.js route. AdminNav is a Server Component, so
// this reads the real per-request runtime config directly — no window
// trick needed here.
export function AdminNav() {
  const t = useTranslations("nav");
  const { appUrl: APP_URL } = getServerRuntimeConfig();
  return (
    <nav className="mb-6 flex items-center justify-between gap-4 border-b pb-4">
      <div className="flex gap-4">
        {/* Plain <a>, not <Link>: a different origin entirely, so there is
            no client-side route to prefetch. Without it, an admin who
            opened the admin UI had no way back to the chat other than
            editing the URL by hand. */}
        <a
          href={APP_URL}
          className="text-sm font-medium hover:text-primary-600"
        >
          {t("backToChat")}
        </a>
        <Link
          href="/admin/groups"
          className="text-sm font-medium hover:text-primary-600"
        >
          {t("groups")}
        </Link>
        <Link
          href="/admin/models"
          className="text-sm font-medium hover:text-primary-600"
        >
          {t("models")}
        </Link>
        <Link
          href="/admin/access"
          className="text-sm font-medium hover:text-primary-600"
        >
          {t("access")}
        </Link>
        <Link
          href="/admin/workflows"
          className="text-sm font-medium hover:text-primary-600"
        >
          {t("workflows")}
        </Link>
        <Link
          href="/admin/analytics"
          className="text-sm font-medium hover:text-primary-600"
        >
          {t("analytics")}
        </Link>
        <Link
          href="/admin/agents"
          className="text-sm font-medium hover:text-primary-600"
        >
          {t("agents")}
        </Link>
        <Link
          href="/agents/constructor"
          className="text-sm font-medium hover:text-primary-600"
        >
          {t("constructor")}
        </Link>
      </div>
      <div className="flex items-center gap-4">
        <Link href="/about" className="text-sm underline">{t("about")}</Link>
        <ThemeToggle />
        <LanguageSwitcher />
        <LogoutButton />
      </div>
    </nav>
  );
}
