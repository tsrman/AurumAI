import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface UserSettings {
  user_id: string;
  theme: string;
  language: string;
  default_model?: string | null;
  organization_default_model?: string | null;
  code_theme: string;
  enter_to_send: boolean;
  show_timestamps: boolean;
  sidebar_collapsed: boolean;
  notifications: Record<string, boolean>;
  shortcuts: Record<string, string>;
}

export type UserSettingsUpdate = Partial<
  Omit<UserSettings, "user_id" | "organization_default_model">
>;

export function useSettings(options?: { enabled?: boolean }) {
  return useQuery<{ data: UserSettings }>({
    queryKey: ["settings"],
    queryFn: async () => (await api.get("settings/me")).json(),
    enabled: options?.enabled ?? true,
  });
}

export function useUpdateSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (data: UserSettingsUpdate) =>
      (await api.patch("settings/me", { json: data })).json(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
}
