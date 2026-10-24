"use client";

import { AgentEditor } from "@/components/agents/AgentEditor";
import { AdminGate } from "@/components/ui";

export default function NewAgentPage() {
  return <AdminGate>{() => <AgentEditor />}</AdminGate>;
}
