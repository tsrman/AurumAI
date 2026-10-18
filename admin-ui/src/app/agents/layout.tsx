import { AdminNav } from "@/components/AdminNav";

export default function AgentsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="mx-auto max-w-6xl p-6">
      <AdminNav />
      {children}
    </div>
  );
}
