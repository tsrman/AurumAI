"use client";

import { useMemo, useState } from "react";

import { GrantEditor } from "@/components/authorization/GrantEditor";
import { useAccessResources } from "@/hooks/useAccessResources";
import { useAdminAccess } from "@/hooks/useAdminAccess";
import { useErrorMessage } from "@/lib/errors";
import type { ResourceType } from "@/lib/api";
import { useTranslations } from "next-intl";

const RESOURCE_TYPES: ResourceType[] = [
  "model",
  "skill",
  "knowledge_base",
  "agent_definition",
  "workflow",
];

const CONTROL_CLASS =
  "mt-1 block w-full rounded-md border border-aurum-border bg-aurum-surface px-3 py-2 text-aurum-text";

export default function AdminAccessPage() {
  const t = useTranslations("access.page");
  const accessT = useTranslations("access");
  const errorMessage = useErrorMessage();
  const access = useAdminAccess();
  const [resourceType, setResourceType] = useState<ResourceType>("model");
  const [resourceId, setResourceId] = useState("");
  const [search, setSearch] = useState("");
  const catalog = useAccessResources(resourceType, {
    enabled: access.allowed,
  });
  const resources = useMemo(() => catalog.data ?? [], [catalog.data]);
  const filtered = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("ru");
    return query
      ? resources.filter((item) =>
          `${item.label}\n${item.id}`.toLocaleLowerCase("ru").includes(query)
        )
      : resources;
  }, [resources, search]);
  const selected =
    resources.find((item) => item.id === resourceId) ?? null;

  if (access.isLoading) return <div className="p-6">{accessT("checking")}</div>;
  if (!access.allowed) {
    return (
      <div className="rounded-md border border-red-200 bg-red-50 p-6 text-red-700">
        {access.error ?? accessT("adminRequired")}
      </div>
    );
  }

  return (
    <div className="max-w-4xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{t("title")}</h1>
        <p className="mt-2 text-sm text-aurum-text-3">
          {t("subtitle")}
        </p>
      </div>

      <div className="grid gap-4 rounded-md border border-aurum-border bg-aurum-surface p-4 md:grid-cols-2">
        <label className="text-sm text-aurum-text-2">
          {t("resourceType")}
          <select
            aria-label={t("resourceType")}
            className={CONTROL_CLASS}
            value={resourceType}
            onChange={(event) => {
              setResourceType(event.target.value as ResourceType);
              setResourceId("");
              setSearch("");
            }}
          >
            {RESOURCE_TYPES.map((item) => (
              <option key={item} value={item}>
                {t(`resourceTypes.${item}`)}
              </option>
            ))}
          </select>
        </label>

        <label className="text-sm text-aurum-text-2">
          {t("search")}
          <input
            aria-label={t("search")}
            className={CONTROL_CLASS}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
      </div>

      {catalog.isLoading ? <p>{t("loading")}</p> : null}
      {catalog.error ? (
        <p className="text-aurum-err-fg">
          {errorMessage(catalog.error)}
        </p>
      ) : null}
      {!catalog.isLoading && !catalog.error && resources.length === 0 ? (
        <p>{t("empty")}</p>
      ) : null}
      {!catalog.isLoading && !catalog.error && resources.length > 0 ? (
        <label className="block text-sm text-aurum-text-2">
          {t("resource")}
          <select
            aria-label={t("resource")}
            className={CONTROL_CLASS}
            value={resourceId}
            onChange={(event) => setResourceId(event.target.value)}
          >
            <option value="">{t("chooseResource")}</option>
            {filtered.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label} · {item.id}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      {selected ? (
        <div className="space-y-4">
          <p className="flex flex-wrap items-baseline gap-2 text-sm">
            <span className="font-medium text-aurum-text">
              {selected.label}
            </span>
            <code className="text-aurum-text-3">{selected.id}</code>
          </p>
          <GrantEditor
            key={`${selected.type}:${selected.id}`}
            resourceType={selected.type}
            resourceId={selected.id}
            resourceLabel={selected.label}
          />
        </div>
      ) : null}
    </div>
  );
}
