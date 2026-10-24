"use client";

import Link from "next/link";
import { useState } from "react";
import { BarChart3, Users } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { useAdminUsers } from "@/hooks/useAdminAnalytics";
import { useErrorMessage } from "@/lib/errors";
import { AdminGate, EmptyState, Notice, PageBody, PageHeader, SearchInput, Table, Td, Th, Tr } from "@/components/ui";
import { PeopleTabs } from "@/components/people/PeopleTabs";
import { Pagination } from "@/components/people/Pagination";
import { RoleBadge, StatusBadge } from "@/components/people/UserBadges";
import { formatDate, formatDateTime, initials } from "@/components/people/format";
import { useDebouncedValue } from "@/components/people/useDebouncedValue";

const PAGE_SIZE = 50;

export default function AdminUsersPage() {
  const t = useTranslations("people.users");
  return (
    <>
      <PageHeader title={t("title")} description={t("subtitle")} tabs={<PeopleTabs />} />
      <AdminGate>{() => <UsersContent />}</AdminGate>
    </>
  );
}

function UsersContent() {
  const t = useTranslations("people.users");
  const locale = useLocale();
  const errorMessage = useErrorMessage();
  const [search, setSearch] = useState("");
  const query = useDebouncedValue(search.trim());
  const [page, setPage] = useState({ query: "", offset: 0 });
  // A new search always starts from its first page.
  const offset = page.query === query ? page.offset : 0;
  const users = useAdminUsers({ limit: PAGE_SIZE, offset, search: query });
  const rows = users.data?.data ?? [];
  const total = users.data?.pagination?.total ?? undefined;

  return (
    <PageBody>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <SearchInput value={search} onChange={setSearch} placeholder={t("search")} />
        {total !== undefined ? <span className="text-sm text-aurum-text-3">{t("count", { count: total })}</span> : null}
      </div>

      <Notice>{t("keycloakHint")}</Notice>

      {users.error ? <Notice tone="error">{errorMessage(users.error)}</Notice> : null}

      {users.isLoading ? (
        <p role="status" className="text-sm text-aurum-text-3">{t("loading")}</p>
      ) : rows.length === 0 && !users.error ? (
        <EmptyState
          icon={<Users className="h-8 w-8" aria-hidden="true" />}
          title={query ? t("noMatches") : t("empty")}
          description={query ? undefined : t("emptyHint")}
        />
      ) : rows.length > 0 ? (
        <Table label={t("title")}>
          <thead>
            <tr>
              <Th>{t("columns.user")}</Th>
              <Th>{t("columns.role")}</Th>
              <Th>{t("columns.status")}</Th>
              <Th>{t("columns.lastActive")}</Th>
              <Th>{t("columns.created")}</Th>
              <Th className="text-right">{t("columns.actions")}</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((user) => {
              const name = user.fullName || user.email;
              return (
                <Tr key={user.id}>
                  <Td>
                    <div className="flex items-center gap-3">
                      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-aurum-accent-soft text-xs font-bold text-aurum-text" aria-hidden="true">
                        {initials(name)}
                      </span>
                      <div className="min-w-0">
                        <p className="truncate font-medium text-aurum-text">{name}</p>
                        {user.fullName ? <p className="truncate text-xs text-aurum-text-3">{user.email}</p> : null}
                      </div>
                    </div>
                  </Td>
                  <Td><RoleBadge role={user.role} /></Td>
                  <Td><StatusBadge status={user.status} /></Td>
                  <Td className="whitespace-nowrap">{user.lastLoginAt ? formatDateTime(user.lastLoginAt, locale) : t("never")}</Td>
                  <Td className="whitespace-nowrap">{formatDate(user.createdAt, locale)}</Td>
                  <Td className="text-right">
                    <Link
                      href={`/admin/analytics?userId=${encodeURIComponent(user.id)}`}
                      aria-label={t("usageAria", { user: name })}
                      className="inline-flex items-center gap-1.5 rounded-btn px-2 py-1.5 text-xs font-medium text-aurum-text-2 hover:bg-aurum-surface-3 hover:text-aurum-text"
                    >
                      <BarChart3 className="h-4 w-4" aria-hidden="true" />
                      {t("usage")}
                    </Link>
                  </Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
      ) : null}

      <Pagination
        offset={offset}
        pageSize={PAGE_SIZE}
        shown={rows.length}
        total={total}
        hasMore={users.data?.pagination?.hasMore ?? false}
        onChange={(next) => setPage({ query, offset: next })}
      />
    </PageBody>
  );
}
