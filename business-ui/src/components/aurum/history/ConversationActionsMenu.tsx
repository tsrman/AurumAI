"use client";

import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Loader2, MoreVertical, Pencil, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { ConfirmDialog } from "@/components/primitives/ConfirmDialog";
import { useTranslations } from "next-intl";

export interface ConversationActionsMenuProps {
  conversationTitle: string;
  isDeleting: boolean;
  onRename: () => void;
  onDelete: () => void;
  /** Extra classes for the 3-dot trigger button (e.g. positioning). */
  triggerClassName?: string;
  menuWidthPx?: number;
}

/** Shared three-dot actions menu for history cards and recent conversations.
 * Portal the fixed-position dropdown to body and measure available space so it
 * opens upward near the viewport bottom. Deletion uses ConfirmDialog rather than
 * native window.confirm, preserving the existing styled/automatable UI behavior.
 */
export function ConversationActionsMenu({
  conversationTitle,
  isDeleting,
  onRename,
  onDelete,
  triggerClassName,
  menuWidthPx = 176,
}: ConversationActionsMenuProps) {
  const t = useTranslations("history.actions");
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState<{ top: number; left: number } | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const menuRef = useRef<HTMLDivElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);

  const positionMenu = useCallback(() => {
    const trigger = menuButtonRef.current;
    const menu = menuRef.current;
    if (!trigger) return;
    const triggerRect = trigger.getBoundingClientRect();
    const menuHeight = menu?.offsetHeight ?? 0;
    const gap = 6;
    const spaceBelow = window.innerHeight - triggerRect.bottom;
    const spaceAbove = triggerRect.top;
    const openUpward = spaceBelow < menuHeight + gap && spaceAbove > spaceBelow;
    const top = openUpward
      ? Math.max(gap, triggerRect.top - menuHeight - gap)
      : triggerRect.bottom + gap;
    const left = Math.min(
      Math.max(gap, triggerRect.right - menuWidthPx),
      window.innerWidth - menuWidthPx - gap
    );
    setMenuPosition({ top, left });
  }, [menuWidthPx]);

  // Runs before paint: first commit mounts the menu (off-screen if
  // menuPosition is still null), this recomputes its final position once
  // its real height is known.
  useLayoutEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect -- positioning needs the
       mounted menu's real height, which only exists after commit: there is
       nothing to derive during render, and useLayoutEffect applies it before
       paint, so the user never sees the intermediate position. */
    if (!menuOpen) {
      setMenuPosition(null);
      return;
    }
    positionMenu();
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [menuOpen, positionMenu]);

  useLayoutEffect(() => {
    if (!menuOpen) return;

    function handleClickOutside(event: MouseEvent) {
      const target = event.target as Node;
      if (
        menuRef.current &&
        !menuRef.current.contains(target) &&
        menuButtonRef.current &&
        !menuButtonRef.current.contains(target)
      ) {
        setMenuOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setMenuOpen(false);
      }
    }

    // Position is computed once per open; a scroll of any ancestor would
    // leave it misaligned, so close instead of tracking it.
    function handleScroll() {
      setMenuOpen(false);
    }

    function handleResize() {
      positionMenu();
    }

    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleKeyDown);
    window.addEventListener("scroll", handleScroll, true);
    window.addEventListener("resize", handleResize);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("scroll", handleScroll, true);
      window.removeEventListener("resize", handleResize);
    };
  }, [menuOpen, positionMenu]);

  return (
    <>
      <button
        ref={menuButtonRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setMenuOpen((v) => !v);
        }}
        disabled={isDeleting}
        aria-label={t("trigger")}
        aria-expanded={menuOpen}
        aria-haspopup="menu"
        className={cn(
          "rounded p-1 text-aurum-text-3 opacity-0 transition hover:bg-aurum-surface-hover hover:text-aurum-text",
          "focus:opacity-100 focus:outline-none focus:ring-2 focus:ring-aurum-accent/50",
          "group-hover:opacity-100",
          menuOpen && "opacity-100",
          isDeleting && "pointer-events-none opacity-0",
          triggerClassName
        )}
      >
        {isDeleting ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        ) : (
          <MoreVertical className="h-4 w-4" aria-hidden="true" />
        )}
      </button>

      {menuOpen &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label={t("menu")}
            style={{
              // Renders off-screen until positionMenu() measures the real
              // height (via the layout effect below) and corrects this —
              // gating the whole render on menuPosition would mean the
              // very first measurement always sees offsetHeight 0, which
              // silently defeats the upward-flip decision.
              top: menuPosition?.top ?? -9999,
              left: menuPosition?.left ?? -9999,
              width: menuWidthPx,
            }}
            className="fixed z-50 rounded-card border border-aurum-border bg-aurum-surface p-1 shadow-card"
          >
            <button
              type="button"
              role="menuitem"
              onClick={(e) => {
                e.stopPropagation();
                setMenuOpen(false);
                onRename();
              }}
              className="flex w-full items-center gap-2 rounded px-3 py-2 text-sm text-aurum-text hover:bg-aurum-surface-hover"
            >
              <Pencil className="h-4 w-4" aria-hidden="true" /> {t("rename")}
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={(e) => {
                e.stopPropagation();
                setMenuOpen(false);
                setConfirmOpen(true);
              }}
              className="flex w-full items-center gap-2 rounded px-3 py-2 text-sm text-aurum-err-fg hover:bg-aurum-err-bg"
            >
              <Trash2 className="h-4 w-4" aria-hidden="true" /> {t("delete")}
            </button>
          </div>,
          document.body
        )}

      <ConfirmDialog
        open={confirmOpen}
        title={t("deleteTitle")}
        description={t("deleteDescription", { title: conversationTitle })}
        confirmLabel={t("delete")}
        cancelLabel={t("cancel")}
        destructive
        onConfirm={() => {
          setConfirmOpen(false);
          onDelete();
        }}
        onCancel={() => setConfirmOpen(false)}
      />
    </>
  );
}
