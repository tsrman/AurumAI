"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/utils";

export interface PickerMenuItem {
  id: string;
  label: string;
  description?: string;
  badge?: React.ReactNode;
}

export interface PickerMenuProps {
  trigger: React.ReactNode;
  items: PickerMenuItem[];
  activeId: string;
  onSelect: (id: string) => void;
  ariaLabel: string;
}

export function PickerMenu({
  trigger,
  items,
  activeId,
  onSelect,
  ariaLabel,
}: PickerMenuProps) {
  const [open, setOpen] = React.useState(false);
  const triggerRef = React.useRef<HTMLElement>(null);
  const menuRef = React.useRef<HTMLDivElement>(null);
  const menuId = React.useId();
  const prevOpenRef = React.useRef(open);
  const closeReasonRef = React.useRef<"keyboard" | "selection" | "blur">("blur");

  // Portaled to <body> with position:fixed, so it can open upward when the
  // trigger sits near the bottom of the viewport (e.g. the chat composer)
  // instead of running off-screen below the window. maxHeight is capped to
  // whatever space is actually available in the chosen direction — without
  // it, a long list (e.g. a real provider's full model list) just grows
  // past the viewport edge with nothing to scroll it into view: not merely
  // awkward, those options are permanently unreachable.
  const [menuStyle, setMenuStyle] = React.useState<{
    top: number;
    left: number;
    maxHeight: number;
  } | null>(null);

  const positionMenu = React.useCallback(() => {
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!trigger) return;
    const triggerRect = trigger.getBoundingClientRect();
    // scrollHeight, not offsetHeight: offsetHeight is already clamped by
    // whatever max-height CSS applied on the *previous* render (including
    // the 320px placeholder on first mount), so it understates how tall the
    // content actually wants to be. scrollHeight reports the true unclamped
    // content size even under overflow-y-auto, which is what the openUpward
    // decision and the upward top calculation both need — using the clamped
    // value here positioned the box assuming it would end up much shorter
    // than the max-height it was about to receive, letting the real
    // (properly capped) box overflow past the bottom of the viewport.
    const menuHeight = menu?.scrollHeight ?? 0;
    const gap = 8;
    const viewportMargin = 16;
    const spaceBelow = window.innerHeight - triggerRect.bottom;
    const spaceAbove = triggerRect.top;
    const openUpward = spaceBelow < menuHeight + gap && spaceAbove > spaceBelow;
    const maxHeight = Math.max(120, (openUpward ? spaceAbove : spaceBelow) - gap - viewportMargin);
    const top = openUpward
      ? Math.max(gap, triggerRect.top - Math.min(menuHeight, maxHeight) - gap)
      : triggerRect.bottom + gap;
    setMenuStyle({ top, left: triggerRect.left, maxHeight });
  }, []);

  React.useLayoutEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect -- positioning needs the
       mounted menu's real height, which only exists after commit: there is
       nothing to derive during render, and useLayoutEffect applies it before
       paint, so the user never sees the intermediate position. */
    if (!open) {
      setMenuStyle(null);
      return;
    }
    // Runs before paint: first commit mounts the menu (off-screen, so its
    // real height can be measured), this recomputes its final position.
    positionMenu();
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [open, items, positionMenu]);

  React.useEffect(() => {
    if (!open) return;
    window.addEventListener("resize", positionMenu);
    return () => window.removeEventListener("resize", positionMenu);
  }, [open, positionMenu]);

  React.useLayoutEffect(() => {
    if (open) {
      const selected = menuRef.current?.querySelector<HTMLElement>(
        `[role="option"][id="${menuId}-item-${activeId}"]`
      );
      const target =
        selected ?? menuRef.current?.querySelector<HTMLElement>('[role="option"]');
      target?.focus();
    }
  }, [open, items, activeId, menuId]);

  React.useEffect(() => {
    if (prevOpenRef.current && !open && closeReasonRef.current !== "blur") {
      triggerRef.current?.focus();
    }
    prevOpenRef.current = open;
  }, [open]);

  React.useEffect(() => {
    if (!open) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeReasonRef.current = "keyboard";
        setOpen(false);
        return;
      }

      const options = Array.from(
        menuRef.current?.querySelectorAll<HTMLElement>('[role="option"]') ?? []
      );
      if (options.length === 0) return;

      const activeIndex = options.indexOf(document.activeElement as HTMLElement);

      if (e.key === "ArrowDown") {
        e.preventDefault();
        const nextIndex = activeIndex >= 0 ? (activeIndex + 1) % options.length : 0;
        options[nextIndex]?.focus();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        const prevIndex =
          activeIndex >= 0
            ? (activeIndex - 1 + options.length) % options.length
            : options.length - 1;
        options[prevIndex]?.focus();
      }
    };

    const handlePointerDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        menuRef.current?.contains(target) ||
        triggerRef.current?.contains(target)
      ) {
        return;
      }
      closeReasonRef.current = "blur";
      setOpen(false);
    };

    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("pointerdown", handlePointerDown);

    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("pointerdown", handlePointerDown);
    };
  }, [open]);

  const handleTriggerClick = React.useCallback(
    (e: React.MouseEvent) => {
      setOpen((prev) => !prev);
      if (React.isValidElement(trigger)) {
        (
          trigger.props as { onClick?: (e: React.MouseEvent) => void }
        ).onClick?.(e);
      }
    },
    [trigger]
  );

  const handleTriggerKeyDown = React.useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setOpen(true);
      }
      if (React.isValidElement(trigger)) {
        (
          trigger.props as { onKeyDown?: (e: React.KeyboardEvent) => void }
        ).onKeyDown?.(e);
      }
    },
    [trigger]
  );

  const handleBlur = React.useCallback((e: React.FocusEvent<HTMLDivElement>) => {
    const relatedTarget = e.relatedTarget as Node | null;
    const menu = menuRef.current;
    const trigger = triggerRef.current;
    if (!relatedTarget) {
      closeReasonRef.current = "blur";
      setOpen(false);
      return;
    }
    if (menu?.contains(relatedTarget) || trigger?.contains(relatedTarget)) {
      return;
    }
    closeReasonRef.current = "blur";
    setOpen(false);
  }, []);

  const triggerElement = React.isValidElement(trigger) ? (
    React.cloneElement(
      trigger as React.ReactElement<
        React.HTMLAttributes<HTMLElement> & React.RefAttributes<HTMLElement>
      >,
      {
        ref: triggerRef,
        "aria-expanded": open,
        "aria-haspopup": "listbox",
        "aria-controls": menuId,
        onClick: handleTriggerClick,
        onKeyDown: handleTriggerKeyDown,
      }
    )
  ) : (
    <span ref={triggerRef as React.RefObject<HTMLSpanElement>}>{trigger}</span>
  );

  return (
    <div className="relative inline-block" onBlur={handleBlur}>
      {triggerElement}
      {open &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            ref={menuRef}
            id={menuId}
            role="listbox"
            aria-label={ariaLabel}
            style={{
              top: menuStyle?.top ?? -9999,
              left: menuStyle?.left ?? -9999,
              maxHeight: menuStyle?.maxHeight ?? 320,
            }}
            className="fixed z-50 min-w-[260px] overflow-y-auto rounded-badge border border-aurum-border bg-aurum-surface p-2 shadow-card"
          >
            {items.map((item) => {
              const itemId = `${menuId}-item-${item.id}`;
              const selected = item.id === activeId;
              return (
                <button
                  key={item.id}
                  id={itemId}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  tabIndex={-1}
                  onClick={() => {
                    closeReasonRef.current = "selection";
                    onSelect(item.id);
                    setOpen(false);
                  }}
                  className={cn(
                    "flex w-full items-start gap-3 rounded-badge px-3 py-2 text-left transition hover:bg-aurum-surface-3 focus:outline-none focus:bg-aurum-surface-3 focus:ring-2 focus:ring-aurum-accent/50",
                    selected && "bg-aurum-accent-soft"
                  )}
                >
                  {item.badge && (
                    <span className="shrink-0">{item.badge}</span>
                  )}
                  <div className="min-w-0">
                    <div className="text-sm font-semibold text-aurum-text">
                      {item.label}
                    </div>
                    {item.description && (
                      <div className="text-xs text-aurum-text-3 line-clamp-2">
                        {item.description}
                      </div>
                    )}
                  </div>
                </button>
              );
            })}
          </div>,
          document.body
        )}
    </div>
  );
}
