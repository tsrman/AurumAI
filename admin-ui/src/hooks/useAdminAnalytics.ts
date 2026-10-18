"use client";

import { useQuery } from "@tanstack/react-query";

import {
  type AdminUsageFilters,
  type AdminUserFilters,
  getAdminUsage,
  getAdminUsers,
} from "@/lib/api";


function withoutEmpty<T extends object>(values: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(values).filter(([, value]) => value !== undefined && value !== "")
  ) as Partial<T>;
}


export function useAdminUsage(filters: AdminUsageFilters) {
  const normalized = withoutEmpty(filters) as AdminUsageFilters;
  return useQuery({
    queryKey: ["admin-usage", normalized],
    queryFn: () => getAdminUsage(normalized),
  });
}


export function useAdminUsers(filters: AdminUserFilters) {
  const normalized = withoutEmpty(filters) as AdminUserFilters;
  return useQuery({
    queryKey: ["admin-users", normalized],
    queryFn: () => getAdminUsers(normalized),
  });
}
