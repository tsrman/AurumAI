export interface Agent {
  id: string;
  name: string;
  badge: string;
  badgeColor: BadgeColor;
  kind: string;
  kindLabel: string;
  description: string;
  capabilities: string[];
  suggestions: string[];
  model?: string;
  prompt?: string;
  tools?: string[];
  bases?: string[];
}

export type BadgeColor = "amber" | "green" | "blue" | "violet" | "rose";

export interface Model {
  id: string;
  name: string;
  shortName: string;
  description: string;
}

export interface KnowledgeBase {
  id: string;
  name: string;
  badge: string;
  badgeColor: BadgeColor;
  description: string;
  docCount: number;
  size: string;
  updated: string;
  status: "indexed" | "processing";
  docs: Document[];
}

export interface Document {
  name: string;
  type: string;
  size: string;
  chunks: number;
  status: "indexed" | "processing";
  date: string;
}

export interface Conversation {
  id: string;
  agentId: string;
  title: string;
  date: string;
  time: string;
  messages: Message[];
  model: string;
  lastMessageAt: string;
  status: string;
  messageCount: number;
}

export type Message = UserMessage | AssistantMessageView;

export interface UserMessage {
  id: string;
  role: "user";
  content: string;
}

export interface AssistantMessage {
  id: string;
  role: "assistant";
  agentId: string;
  thinking: string;
  tools: ToolCall[];
  content: string;
  widgets: Widget[];
  sources: Source[];
  sql?: string;
}

export interface AssistantMessageState {
  thinkingShown: string;
  contentShown: string;
  phase: "think" | "tools" | "answer" | "done";
  thinkingOpen: boolean;
  thinkingSeconds: number;
  startedAt: number;
  // Background faithfulness/hallucination judge result — undefined until the
  // evaluator (if enabled) finishes; null score means it ran but failed.
  faithfulnessScore?: number | null;
  faithfulnessReason?: string | null;
}

export type AssistantMessageView = AssistantMessage & AssistantMessageState;

export interface ToolCall {
  callId?: string;
  name: string;
  detail: string;
  state?: "wait" | "run" | "done";
  isAgent?: boolean;
  tag?: string;
  badgeColor?: BadgeColor;
}

export type Widget = MetricsWidget | BarsWidget | TableWidget;

export interface MetricsWidget {
  type: "metrics";
  items: { label: string; value: string; delta: string; isPositive: boolean }[];
}

export interface BarsWidget {
  type: "bars";
  title: string;
  unit: string;
  max: number;
  data: { label: string; value: number }[];
}

export interface TableWidget {
  type: "table";
  columns: string[];
  rows: string[][];
}

export interface Source {
  index: number;
  doc: string;
  section: string;
  score: number;
  knowledgeBase: string;
  snippet: string;
}

export interface ConstructorState {
  // "hermes": a workflow node run by Hermes Agent; fixed once created.
  kind: "agent" | "hermes";
  name: string;
  description: string;
  model: string;
  prompt: string;
  temperature: number | null;
  tools: Record<string, boolean>;
  bases: Record<string, boolean>;
}
