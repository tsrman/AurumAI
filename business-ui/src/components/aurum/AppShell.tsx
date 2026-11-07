"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { cn } from "@/lib/utils";
import { useAuth } from "@/providers/AuthProvider";
import { Sidebar } from "./Sidebar";

export interface AppShellProps {
  children: React.ReactNode;
}

const PUBLIC_PATHS = ["/login", "/about"];

export function AppShell({ children }: AppShellProps) {
  const { isAuthenticated, isLoading } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const isPublicPath = PUBLIC_PATHS.includes(pathname);
  // A visitor who has not signed in has no agents, history or knowledge bases;
  // the sidebar would offer the built-in demo catalog and dead links. While the
  // session is still being resolved the layout stays as it will be for a user.
  const showSidebar = isLoading || isAuthenticated;

  useEffect(() => {
    if (isLoading || isPublicPath || isAuthenticated) return;
    router.push("/login");
  }, [isLoading, isPublicPath, isAuthenticated, router]);

  return (
    <div
      className={cn(
        "flex h-screen w-full overflow-hidden bg-aurum-bg text-aurum-text"
      )}
    >
      {showSidebar ? <Sidebar /> : null}
      <main className="flex flex-1 flex-col overflow-auto">{children}</main>
    </div>
  );
}
