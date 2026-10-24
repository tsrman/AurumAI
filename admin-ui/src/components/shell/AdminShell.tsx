import { AdminSidebar } from "./AdminSidebar";

/** Full-height frame for every signed-in admin page, matching business-ui's AppShell. */
export function AdminShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-screen w-full overflow-hidden bg-aurum-bg text-aurum-text">
      <AdminSidebar />
      <main className="flex min-w-0 flex-1 flex-col overflow-auto">{children}</main>
    </div>
  );
}
