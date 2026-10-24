"use client";

import {
  useMemo,
  useCallback,
  useState,
} from "react";
import { useRouter, usePathname } from "next/navigation";
import {
  Sparkles,
  Plus,
  Bot,
  BookOpen,
  History,
  PanelLeft,
  MessageSquare,
  Loader2,
  LogOut,
  ShieldCheck,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useAppShell } from "@/hooks/useAppShell";
import { useAuth } from "@/providers/AuthProvider";
import { useHistory } from "@/hooks/useHistory";
import { getRuntimeConfig } from "@/lib/runtime-config";
import { Button, IconButton } from "@/components/primitives";
import { Badge } from "@/components/primitives/Badge";
import { ConversationActionsMenu } from "./history/ConversationActionsMenu";
import { NavItem } from "./NavItem";
import { UserCard } from "./UserCard";
import { ThemeToggle } from "./ThemeToggle";
import { LanguageSwitcher } from "./LanguageSwitcher";
import type { Conversation } from "@/types/aurum";
import { useTranslations } from "next-intl";
import { useErrorMessage } from "@/lib/errors";

// Skills/MCP, groups, and the agent constructor moved to admin-ui (separate
// app, separate Keycloak client audience) — see
// docs/ARCHITECTURE.md. This link is deliberately an
// absolute cross-origin URL, not an internal Next.js route.
const ADMIN_URL = getRuntimeConfig().adminUrl;

const STUDIO_LINKS: {
  icon: React.ReactNode;
  labelKey: "links.knowledge" | "links.history" | "links.admin";
  href: string;
  adminOnly: boolean;
}[] = [
  {
    icon: <BookOpen className="h-5 w-5" aria-hidden="true" />,
    labelKey: "links.knowledge",
    href: "/knowledge",
    adminOnly: false,
  },
  {
    icon: <History className="h-5 w-5" aria-hidden="true" />,
    labelKey: "links.history",
    href: "/history",
    adminOnly: false,
  },
  {
    icon: <ShieldCheck className="h-5 w-5" aria-hidden="true" />,
    labelKey: "links.admin",
    href: `${ADMIN_URL}/admin/users`,
    adminOnly: true,
  },
];

interface SidebarConversationItemProps {
  conversation: Conversation;
  onClick: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  isRenaming: boolean;
  isDeleting: boolean;
}

function SidebarConversationItem({
  conversation,
  onClick,
  onRename,
  onDelete,
  isRenaming,
  isDeleting,
}: SidebarConversationItemProps) {
  const t = useTranslations("sidebar");
  const [isEditing, setIsEditing] = useState(false);
  const [editTitle, setEditTitle] = useState(conversation.title);

  function startRename() {
    setEditTitle(conversation.title);
    setIsEditing(true);
  }

  function saveRename() {
    const trimmed = editTitle.trim();
    if (!trimmed) {
      setEditTitle(conversation.title);
      setIsEditing(false);
      return;
    }
    if (trimmed !== conversation.title) {
      onRename(conversation.id, trimmed);
    }
    setIsEditing(false);
  }

  function cancelRename() {
    setEditTitle(conversation.title);
    setIsEditing(false);
  }

  function handleInputKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      saveRename();
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancelRename();
    }
  }

  return (
    <div className="group relative flex w-full items-center">
      <button
        type="button"
        onClick={onClick}
        disabled={isDeleting || isEditing}
        aria-label={t("openConversation", { title: conversation.title })}
        className={cn(
          "flex w-full items-center gap-2 rounded-lg py-2 pl-3 pr-8 text-sm transition",
          "text-aurum-text-2 hover:bg-aurum-surface-3 hover:text-aurum-text",
          "disabled:cursor-not-allowed disabled:opacity-60"
        )}
      >
        <MessageSquare className="h-4 w-4 shrink-0 text-aurum-text-3" aria-hidden="true" />
        {isEditing ? (
          <span className="min-w-0 flex-1" onClick={(e) => e.stopPropagation()}>
            <input
              type="text"
              value={editTitle}
              onChange={(e) => setEditTitle(e.target.value)}
              onBlur={saveRename}
              onKeyDown={handleInputKeyDown}
              disabled={isRenaming}
              autoFocus
              aria-label={t("conversationTitle")}
              className={cn(
                "w-full rounded border border-aurum-border bg-aurum-bg px-2 py-1 text-sm text-aurum-text",
                "focus:outline-none focus:ring-2 focus:ring-aurum-accent/50",
                isRenaming && "opacity-60"
              )}
              onClick={(e) => e.stopPropagation()}
            />
          </span>
        ) : (
          <>
            <span className="truncate">{conversation.title}</span>
            <span className="ml-auto shrink-0 text-xs text-aurum-text-3">
              {conversation.time}
            </span>
          </>
        )}
      </button>

      {!isEditing && (
        <div className="absolute right-1 top-1/2 -translate-y-1/2">
          <ConversationActionsMenu
            conversationTitle={conversation.title}
            isDeleting={isDeleting}
            onRename={startRename}
            onDelete={() => onDelete(conversation.id)}
          />
        </div>
      )}
    </div>
  );
}

export function Sidebar() {
  const aboutT = useTranslations("about");
  const t = useTranslations("sidebar");
  const errorMessage = useErrorMessage();
  const {
    sideMini,
    toggleSide,
    agents,
    myAgents,
    activeAgentId,
    setActiveAgentId,
    setActiveConversationId,
  } = useAppShell();
  const router = useRouter();
  const pathname = usePathname();
  const { user, logout } = useAuth();

  const {
    historyItems,
    isLoading,
    isError,
    error,
    openConversation,
    renameConversation,
    deleteConversation,
    isRenaming,
    isDeleting,
  } = useHistory({ limit: 10 });

  const allAgents = useMemo(() => [...agents, ...myAgents], [agents, myAgents]);

  const recentConversations = useMemo(
    () => historyItems.flatMap((group) => group.conversations).slice(0, 10),
    [historyItems]
  );

  const isOrgAdmin = user?.role === "org_admin";

  const studioItems = useMemo(
    () =>
      STUDIO_LINKS.filter((link) => !link.adminOnly || isOrgAdmin).map((link) => ({
        ...link,
        active: pathname === link.href,
      })),
    [isOrgAdmin, pathname]
  );

  const handleAgentClick = useCallback(
    (id: string) => {
      setActiveAgentId(id);
      router.push("/chat");
    },
    [router, setActiveAgentId]
  );

  const handleNewChat = useCallback(() => {
    setActiveConversationId(null);
    router.push("/chat");
  }, [router, setActiveConversationId]);

  const handleOpenConversation = useCallback(
    (id: string) => {
      openConversation(id);
    },
    [openConversation]
  );

  return (
    <aside
      aria-label={t("panel")}
      className={cn(
        "flex h-full shrink-0 flex-col border-r border-aurum-border bg-aurum-surface transition-all",
        sideMini ? "w-[68px]" : "w-[264px]"
      )}
    >
      {/* Brand */}
      <div
        className={cn(
          "flex items-center gap-2 border-b border-aurum-border p-4",
          sideMini && "justify-center px-2"
        )}
      >
        <Sparkles
          className="h-6 w-6 shrink-0 text-aurum-accent"
          aria-hidden="true"
        />
        {!sideMini && (
          <span className="text-lg font-bold tracking-wide text-aurum-text">
            AurumAI
          </span>
        )}
      </div>

      <a href="/about" className="px-4 py-2 text-sm underline" aria-label={aboutT("linkAria")}>{sideMini ? "ⓘ" : aboutT("linkShort")}</a>

      {/* New chat */}
      <div className={cn("p-4", sideMini && "px-2")}>
        {sideMini ? (
          <IconButton
            onClick={handleNewChat}
            aria-label={t("newChat")}
            title={t("newChat")}
            className="w-full"
          >
            <Plus className="h-5 w-5" aria-hidden="true" />
          </IconButton>
        ) : (
          <Button onClick={handleNewChat} className="w-full">
            <Plus className="h-5 w-5" aria-hidden="true" />
            <span>{t("newChat")}</span>
          </Button>
        )}
      </div>

      <nav aria-label={t("navigation")} className="flex flex-1 flex-col overflow-hidden">
        {/* Agents */}
        <div className="flex flex-1 flex-col gap-1 overflow-y-auto px-4 py-2">
          {!sideMini && (
            <div className="mb-1 px-3 text-xs font-bold tracking-wider text-aurum-text-3">
              {t("agents")}
            </div>
          )}
          {allAgents.map((agent) => (
            <NavItem
              key={agent.id}
              icon={<Bot className="h-5 w-5" aria-hidden="true" />}
              label={agent.name}
              badge={<Badge color={agent.badgeColor}>{agent.badge}</Badge>}
              active={activeAgentId === agent.id}
              mini={sideMini}
              onClick={() => handleAgentClick(agent.id)}
            />
          ))}
        </div>

        {/* History */}
        {!sideMini && (
          <div className="flex max-h-[40%] flex-col gap-1 overflow-y-auto border-t border-aurum-border px-4 py-3">
            <div className="mb-1 px-3 text-xs font-bold tracking-wider text-aurum-text-3">
              {t("history")}
            </div>

            {isLoading && recentConversations.length === 0 && (
              <div
                className="flex items-center gap-2 px-3 py-2 text-sm text-aurum-text-3"
                role="status"
                aria-live="polite"
              >
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                {t("loading")}
              </div>
            )}

            {isError && recentConversations.length === 0 && (
              <div
                className="rounded-lg border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-err-fg"
                role="alert"
              >
                {t("loadFailed")}
                {error?.message && (
                  <span className="block text-xs text-aurum-err-fg/80">{errorMessage(error)}</span>
                )}
              </div>
            )}

            {!isLoading && !isError && recentConversations.length === 0 && (
              <div className="px-3 py-2 text-sm text-aurum-text-3">
                {t("empty")}
              </div>
            )}

            {recentConversations.map((conversation) => (
              <SidebarConversationItem
                key={conversation.id}
                conversation={conversation}
                onClick={() => handleOpenConversation(conversation.id)}
                onRename={renameConversation}
                onDelete={deleteConversation}
                isRenaming={isRenaming}
                isDeleting={isDeleting}
              />
            ))}
          </div>
        )}

        {/* Studio */}
        <div className="flex flex-col gap-1 border-t border-aurum-border px-4 py-3">
          {!sideMini && (
            <div className="mb-1 px-3 text-xs font-bold tracking-wider text-aurum-text-3">
              {t("studio")}
            </div>
          )}
          {studioItems.map((link) => (
            <NavItem
              key={link.href}
              href={link.href}
              icon={link.icon}
              label={t(link.labelKey)}
              active={link.active}
              mini={sideMini}
            />
          ))}
        </div>
      </nav>

      {/* Bottom controls */}
      <div className={cn("border-t border-aurum-border p-4", sideMini && "px-2")}>
        {/* Collapsed, these three buttons stack instead of sitting in a
            row: the rail is 68px wide (px-2 leaves ~52px usable) while a row
            of three IconButtons plus gaps needs well over twice that, so
            they overflowed the panel and rendered outside its border. */}
        <div
          className={cn(
            "mb-3 flex items-center gap-2",
            sideMini && "flex-col justify-center"
          )}
        >
          <IconButton
            onClick={toggleSide}
            aria-label={sideMini ? t("expand") : t("collapse")}
            title={sideMini ? t("expand") : t("collapse")}
          >
            <PanelLeft
              className={cn(
                "h-5 w-5 transition-transform",
                sideMini && "rotate-180"
              )}
              aria-hidden="true"
            />
          </IconButton>
          <ThemeToggle />
          {!sideMini ? <LanguageSwitcher /> : null}
          <IconButton onClick={logout} aria-label={t("logout")} title={t("logout")}>
            <LogOut className="h-5 w-5" aria-hidden="true" />
          </IconButton>
        </div>
        <UserCard
          name={user?.fullName || user?.email || t("userFallback")}
          role={user?.role || ""}
          mini={sideMini}
        />
      </div>
    </aside>
  );
}
