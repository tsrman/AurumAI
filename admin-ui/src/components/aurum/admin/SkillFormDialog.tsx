"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertCircle, Check, FileArchive, Loader2, Search, Wrench } from "lucide-react";
import { Button } from "@/components/primitives/Button";
import { Field } from "@/components/primitives/Field";
import { Textarea } from "@/components/primitives/Textarea";
import { SegmentedControl } from "@/components/primitives/SegmentedControl";
import { cn } from "@/lib/utils";
import { LocalizedError, useErrorMessage } from "@/lib/errors";
import {
  useCreateSkill,
  useUpdateSkill,
  useDiscoverMcpTools,
  useUploadCodeSkill,
  useReplaceCodeSkillBundle,
} from "@/hooks/useSkills";
import { useAuth } from "@/providers/AuthProvider";
import { GrantEditor } from "@/components/authorization/GrantEditor";
import type { McpToolInfo, Skill, SkillCreate } from "@/lib/api";
import { useTranslations } from "next-intl";

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export interface SkillFormDialogProps {
  open: boolean;
  skill: Skill | null;
  onClose: () => void;
}

function parseJsonObject(text: string, fallback: Record<string, unknown> = {}) {
  const trimmed = text.trim();
  if (!trimmed) return fallback;
  try {
    return JSON.parse(trimmed) as Record<string, unknown>;
  } catch {
    throw new LocalizedError("invalidJsonObject");
  }
}

function parseStringList(text: string): string[] {
  const trimmed = text.trim();
  if (!trimmed) return [];
  if (trimmed.startsWith("[")) {
    try {
      const parsed = JSON.parse(trimmed);
      if (Array.isArray(parsed)) return parsed.map(String);
      throw new Error();
    } catch {
      throw new LocalizedError("invalidJsonList");
    }
  }
  return trimmed
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

const TYPE_OPTIONS = [
  { value: "agent" as const, label: "agent" },
  { value: "tool" as const, label: "tool" },
  { value: "mcp" as const, label: "mcp" },
  { value: "code" as const, label: "code" },
];

const TRANSPORT_OPTIONS = [
  { value: "sse" as const, label: "sse" },
];

export function SkillFormDialog({ open, skill, onClose }: SkillFormDialogProps) {
  const t = useTranslations("skills.form");
  const errorMessage = useErrorMessage();
  const titleId = useId();
  const firstFieldRef = useRef<HTMLInputElement>(null);

  const create = useCreateSkill();
  const update = useUpdateSkill();
  const discover = useDiscoverMcpTools();
  const upload = useUploadCodeSkill();
  const replaceBundle = useReplaceCodeSkillBundle();
  const { user } = useAuth();
  const isOrgAdmin = user?.role === "org_admin";

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [type, setType] = useState<"agent" | "tool" | "mcp" | "code">("agent");
  const [workflowActivity, setWorkflowActivity] = useState("");
  const [mcpTransport, setMcpTransport] = useState<"sse" | "stdio">("sse");
  const [mcpEndpoint, setMcpEndpoint] = useState("");
  const [mcpAuthToken, setMcpAuthToken] = useState("");
  const [inputSchemaText, setInputSchemaText] = useState("{}");
  const [allowedChildrenText, setAllowedChildrenText] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [selectedToolName, setSelectedToolName] = useState<string | null>(null);
  const [bundleFile, setBundleFile] = useState<File | null>(null);
  const [replaceBundleFile, setReplaceBundleFile] = useState<File | null>(null);

  const isEditing = skill != null;

  useEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect -- seeding the form when
       the dialog opens. The React-recommended alternative is remounting the
       dialog with key={skill?.id}, which would move this decision into every
       call site; the fields below are plain edit state and there is nothing to
       derive from props during render. */
    if (!open) return;
    if (skill) {
      setName(skill.name);
      setDescription(skill.description || "");
      setType(skill.type);
      setWorkflowActivity(skill.workflowType || skill.activityName || "");
      setMcpTransport(skill.mcpTransport === "stdio" ? "stdio" : "sse");
      setMcpEndpoint(skill.mcpEndpoint || "");
      // Never pre-fill a stored secret into a visible form field — leave
      // blank and let the "leave empty to keep" placeholder explain it.
      setMcpAuthToken("");
      setInputSchemaText(JSON.stringify(skill.inputSchema || {}, null, 2));
      setAllowedChildrenText(
        skill.allowedChildSkills ? JSON.stringify(skill.allowedChildSkills, null, 2) : ""
      );
    } else {
      setName("");
      setDescription("");
      setType("agent");
      setWorkflowActivity("");
      setMcpTransport("sse");
      setMcpEndpoint("");
      setMcpAuthToken("");
      setInputSchemaText("{}");
      setAllowedChildrenText("");
    }
    setFormError(null);
    setSelectedToolName(null);
    setBundleFile(null);
    setReplaceBundleFile(null);
    discover.reset();
    upload.reset();
    replaceBundle.reset();
    /* eslint-enable react-hooks/set-state-in-effect */
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, skill]);

  useEffect(() => {
    if (open) firstFieldRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open || typeof document === "undefined") return null;

  const endpointChanged = isEditing && (
    mcpEndpoint !== (skill.mcpEndpoint || "") || mcpTransport !== skill.mcpTransport
  );

  const handleDiscover = () => {
    discover.mutate({
      endpoint: mcpEndpoint,
      transport: mcpTransport,
      authToken: mcpAuthToken || undefined,
      // Retain skill-write authorization for editors without create capability.
      // Discovery on a changed address waits for a new token or a saved edit.
      skillId: isEditing ? skill.id : undefined,
    });
  };

  const handleSelectTool = (tool: McpToolInfo) => {
    setSelectedToolName(tool.name);
    setName(tool.name);
    setDescription(tool.description || "");
    setWorkflowActivity(tool.name);
    setInputSchemaText(JSON.stringify(tool.inputSchema || {}, null, 2));
  };

  const buildPayload = (): SkillCreate => {
    const inputSchema = parseJsonObject(inputSchemaText);
    const allowedChildSkills = allowedChildrenText.trim()
      ? parseStringList(allowedChildrenText)
      : undefined;

    return {
      name,
      description: description || undefined,
      // buildPayload() is also used to PATCH a type="code" skill's
      // name/description — the backend's CodeSkillUpdate schema has no
      // `type` field and silently ignores it, so this cast is safe there.
      type: type as "agent" | "tool" | "mcp",
      workflow_type: workflowActivity || undefined,
      activity_name: workflowActivity || undefined,
      mcp_transport: type === "mcp" ? mcpTransport : undefined,
      mcp_endpoint: type === "mcp" ? mcpEndpoint || undefined : undefined,
      // Blank preserves a token only if endpoint/transport are unchanged;
      // otherwise the backend clears it.
      mcp_auth_token: type === "mcp" ? mcpAuthToken || undefined : undefined,
      input_schema: inputSchema,
      allowed_child_skills: allowedChildSkills,
    };
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);

    if (type === "mcp" && mcpTransport !== "sse") {
      setFormError(t("sseRequired"));
      return;
    }

    if (type === "code" && !isEditing) {
      if (!bundleFile) {
        setFormError(t("bundleRequired"));
        return;
      }
      const form = new FormData();
      form.set("name", name);
      if (description) form.set("description", description);
      form.set("bundle", bundleFile);
      upload.mutate(form, { onSuccess: onClose });
      return;
    }

    try {
      // Editing a code skill still goes through the generic PATCH — the
      // backend restricts it to name/description for type="code" skills
      // (bundle replacement is the separate action below), so the extra
      // fields buildPayload() includes here are harmlessly ignored.
      const payload = buildPayload();
      if (isEditing && skill) {
        update.mutate({ id: skill.id, data: payload }, { onSuccess: onClose });
      } else {
        create.mutate(payload, { onSuccess: onClose });
      }
    } catch (err) {
      setFormError(err instanceof Error ? errorMessage(err) : t("formError"));
    }
  };

  const handleReplaceBundle = () => {
    if (!skill || !replaceBundleFile) return;
    const form = new FormData();
    form.set("bundle", replaceBundleFile);
    replaceBundle.mutate(
      { id: skill.id, form },
      { onSuccess: () => setReplaceBundleFile(null) }
    );
  };

  const discoveredTools = discover.data?.data ?? [];
  const needsNewCredential = endpointChanged && skill?.hasMcpAuthToken && !mcpAuthToken;
  const canDiscover = type === "mcp" && mcpTransport === "sse" && mcpEndpoint.trim().length > 0 && !needsNewCredential;
  const isSaving = create.isPending || update.isPending || upload.isPending;

  return createPortal(
    <>
      <div className="fixed inset-0 z-40 bg-black/40" onClick={onClose} aria-hidden="true" />
      <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 py-10">
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          className="w-full max-w-2xl rounded-card border border-aurum-border bg-aurum-surface p-6 shadow-drawer"
          onClick={(e) => e.stopPropagation()}
        >
          <h2 id={titleId} className="text-base font-semibold text-aurum-text">
            {isEditing ? t("editTitle") : t("createTitle")}
          </h2>

          <form onSubmit={handleSubmit} className="mt-4 space-y-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <Field
                ref={firstFieldRef}
                label={t("name")}
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />

              <div>
                <span className="mb-1.5 block text-sm font-medium text-aurum-text-2">{t("type")}</span>
                <SegmentedControl options={TYPE_OPTIONS} value={type} onChange={setType} />
              </div>

              <div className="md:col-span-2">
                <Field
                  label={t("description")}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder={t("descriptionPlaceholder")}
                />
              </div>
            </div>

            {type === "mcp" && (
              <div className="space-y-3 rounded-lg border border-aurum-border-soft bg-aurum-surface-2 p-4">
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                  <div>
                    <span className="mb-1.5 block text-sm font-medium text-aurum-text-2">
                      {t("transport")}
                    </span>
                    <SegmentedControl
                      options={TRANSPORT_OPTIONS}
                      value={mcpTransport}
                      onChange={setMcpTransport}
                    />
                    <p className="mt-1.5 text-xs text-aurum-text-3">
                      {t("transportHint")}
                    </p>
                  </div>
                  <Field
                    label="MCP Endpoint"
                    value={mcpEndpoint}
                    onChange={(e) => setMcpEndpoint(e.target.value)}
                    placeholder="http://mcp-everything:3001/sse"
                  />
                  <div className="md:col-span-2">
                    <Field
                      label={t("token")}
                      type="password"
                      autoComplete="off"
                      value={mcpAuthToken}
                      onChange={(e) => setMcpAuthToken(e.target.value)}
                      placeholder={
                        // skill.mcpAuthToken is always
                        // null to browsers — hasMcpAuthToken is the actual
                        // signal for "a token is saved".
                        endpointChanged && skill?.hasMcpAuthToken
                          ? t("tokenEndpointChanged")
                          : isEditing && skill?.hasMcpAuthToken
                          ? t("tokenSaved")
                          : t("tokenOptional")
                      }
                    />
                  </div>
                </div>

                <div>
                  {needsNewCredential && (
                    <p className="mb-1.5 text-xs text-aurum-text-3">
                      {t("needsNewCredential")}
                    </p>
                  )}
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={!canDiscover || discover.isPending}
                    onClick={handleDiscover}
                  >
                    {discover.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                    ) : (
                      <Search className="h-4 w-4" aria-hidden="true" />
                    )}
                    {t("discover")}
                  </Button>
                  {mcpTransport === "stdio" && (
                    <p className="mt-1.5 text-xs text-aurum-text-3">
                      {t("stdioRetired")}
                    </p>
                  )}
                  {discover.isError && (
                    <p className="mt-1.5 flex items-center gap-1.5 text-sm text-aurum-err-fg">
                      <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
                      {errorMessage(discover.error)}
                    </p>
                  )}
                </div>

                {discoveredTools.length > 0 && (
                  <div className="space-y-1.5">
                    <span className="block text-xs font-medium text-aurum-text-3">
                      {t("discovered", { count: discoveredTools.length })}
                    </span>
                    {discoveredTools.map((tool) => {
                      const isSelected = tool.name === selectedToolName;
                      return (
                        <button
                          key={tool.name}
                          type="button"
                          onClick={() => handleSelectTool(tool)}
                          className={cn(
                            "flex w-full items-center gap-3 rounded-lg border px-3 py-2 text-left transition",
                            isSelected
                              ? "border-aurum-accent bg-aurum-accent-soft"
                              : "border-aurum-border-soft bg-aurum-surface hover:bg-aurum-surface-3"
                          )}
                        >
                          <div
                            className={cn(
                              "flex h-7 w-7 shrink-0 items-center justify-center rounded-full",
                              isSelected
                                ? "bg-aurum-ok-bg text-aurum-ok-fg"
                                : "bg-aurum-surface-3 text-aurum-text-3"
                            )}
                          >
                            {isSelected ? (
                              <Check className="h-4 w-4" aria-hidden="true" />
                            ) : (
                              <Wrench className="h-4 w-4" aria-hidden="true" />
                            )}
                          </div>
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-sm font-medium text-aurum-text">
                              {tool.name}
                            </p>
                            {tool.description && (
                              <p className="truncate text-xs text-aurum-text-3">
                                {tool.description}
                              </p>
                            )}
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            )}

            {type === "code" && !isEditing && (
              <div className="space-y-3 rounded-lg border border-aurum-border-soft bg-aurum-surface-2 p-4">
                <label className="block text-sm font-medium text-aurum-text-2" htmlFor="code-skill-bundle">
                  {t("bundle")}
                </label>
                <input
                  id="code-skill-bundle"
                  type="file"
                  accept=".zip"
                  onChange={(e) => setBundleFile(e.target.files?.[0] ?? null)}
                  className="block w-full text-sm text-aurum-text-2 file:mr-3 file:rounded-md file:border-0 file:bg-aurum-surface-3 file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-aurum-text"
                />
                <p className="text-xs text-aurum-text-3">
                  {t("bundleHint")}
                </p>
                {bundleFile && (
                  <p className="flex items-center gap-1.5 text-sm text-aurum-text">
                    <FileArchive className="h-4 w-4 shrink-0" aria-hidden="true" />
                    {bundleFile.name} ({formatBytes(bundleFile.size)})
                  </p>
                )}
                {upload.isError && (
                  <p className="flex items-center gap-1.5 text-sm text-aurum-err-fg">
                    <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
                    {errorMessage(upload.error)}
                  </p>
                )}
              </div>
            )}

            {type === "code" && isEditing && skill && (
              <div className="space-y-3 rounded-lg border border-aurum-border-soft bg-aurum-surface-2 p-4">
                <div className="flex items-center gap-1.5 text-sm text-aurum-text">
                  <FileArchive className="h-4 w-4 shrink-0" aria-hidden="true" />
                  <span>
                    {skill.bundleSkillName || t("bundleFallbackName")}
                    {typeof skill.bundleSizeBytes === "number"
                      ? ` · ${formatBytes(skill.bundleSizeBytes)}`
                      : ""}
                  </span>
                </div>
                {skill.bundleChecksum && (
                  <p className="font-mono text-xs text-aurum-text-3">
                    sha256: {skill.bundleChecksum.slice(0, 16)}…
                  </p>
                )}
                <div className="flex flex-wrap items-center gap-3">
                  <input
                    type="file"
                    accept=".zip"
                    onChange={(e) => setReplaceBundleFile(e.target.files?.[0] ?? null)}
                    className="text-sm text-aurum-text-2 file:mr-3 file:rounded-md file:border-0 file:bg-aurum-surface-3 file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-aurum-text"
                  />
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={!replaceBundleFile || replaceBundle.isPending}
                    onClick={handleReplaceBundle}
                  >
                    {replaceBundle.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                    ) : (
                      t("replaceBundle")
                    )}
                  </Button>
                </div>
                {replaceBundle.isError && (
                  <p className="flex items-center gap-1.5 text-sm text-aurum-err-fg">
                    <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
                    {errorMessage(replaceBundle.error)}
                  </p>
                )}
                {replaceBundle.isSuccess && !replaceBundleFile && (
                  <p className="text-sm text-aurum-ok-fg">{t("bundleReplaced")}</p>
                )}
              </div>
            )}

            {type !== "code" && (
              <>
                <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                  <Field
                    label={type === "mcp" ? t("toolName") : "Workflow Type / Activity Name"}
                    value={workflowActivity}
                    onChange={(e) => setWorkflowActivity(e.target.value)}
                  />
                </div>

                <Textarea
                  label="Input Schema (JSON)"
                  value={inputSchemaText}
                  onChange={(e) => setInputSchemaText(e.target.value)}
                  className="font-mono text-sm"
                  rows={5}
                />

                <Textarea
                  label={t("allowedChildSkills")}
                  value={allowedChildrenText}
                  onChange={(e) => setAllowedChildrenText(e.target.value)}
                  className="font-mono text-sm"
                  rows={2}
                />
              </>
            )}

            {isEditing && skill && isOrgAdmin ? (
              <GrantEditor
                resourceType="skill"
                resourceId={skill.id}
                resourceLabel={skill.name}
              />
            ) : null}

            {formError && <p className="text-sm text-aurum-err-fg">{formError}</p>}
            {create.isError && (
              <p className="text-sm text-aurum-err-fg">{errorMessage(create.error)}</p>
            )}
            {update.isError && (
              <p className="text-sm text-aurum-err-fg">{errorMessage(update.error)}</p>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <Button type="button" variant="secondary" onClick={onClose}>
                {t("cancel")}
              </Button>
              <Button type="submit" disabled={isSaving}>
                {isSaving
                  ? t("saving")
                  : isEditing
                    ? t("update")
                    : t("createTitle")}
              </Button>
            </div>
          </form>
        </div>
      </div>
    </>,
    document.body
  );
}
