"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/providers/AuthProvider";

export interface AuthGuardProps {
  children: React.ReactNode;
}

const PUBLIC_PATHS = ["/login", "/about"];

/**
 * Same redirect-to-login guard as business-ui's AppShell, minus the
 * Sidebar/chat shell this app doesn't have (admin-ui has no chat surface —
 * each admin page carries its own nav via admin/layout.tsx).
 */
export function AuthGuard({ children }: AuthGuardProps) {
  const { isAuthenticated, isLoading } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const isPublicPath = PUBLIC_PATHS.includes(pathname);

  useEffect(() => {
    if (isLoading || isPublicPath || isAuthenticated) return;
    router.push("/login");
  }, [isLoading, isPublicPath, isAuthenticated, router]);

  return <>{children}</>;
}
