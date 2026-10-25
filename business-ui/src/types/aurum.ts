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
  // The organization marked this model as accepting attached images.
  vision?: boolean;
}

export interface KnowledgeBase {
  id: string;
  name: string;
  badge: string;
  badgeColor: BadgeColor;
  description: string;
  canWrite: boolean;
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
  // failed means source parsing/indexing failed; see documents.status
  // in rag-api.
  status: "indexed" | "processing" | "failed";
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
  temperature: number;
  lastMessageAt: string;
  status: string;
  messageCount: number;
}

export type Message = UserMessage | AssistantMessageView;

export interface UserMessage {
  id: string;
  role: "user";
  content: string;
  // Ids of images the user attached to this message.
  images?: string[];
  createdAt?: string;
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
  // Ids of images this answer produced; shown under the answer.
  images?: string[];
  sql?: string;
  createdAt?: string;
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
  workflow?: WorkflowProgressState;
}

export type AssistantMessageView = AssistantMessage & AssistantMessageState;

export type WorkflowRunStatus =
  | "pending"
  | "planning"
  | "running"
  | "synthesizing"
  | "completed"
  | "failed"
  | "cancelled";

export type WorkflowNodeStatus =
  | "pending"
  | "running"
  | "waiting_human"
  | "completed"
  | "failed"
  | "skipped"
  | "cancelled";

export interface WorkflowNodeProgress {
  nodeKey: string;
  nodeName: string;
  status: WorkflowNodeStatus;
  errorCode?: string;
}

export interface WorkflowProgressState {
  workflowRunId: string;
  /** Chat turn of the run; matches the persisted final message on rehydration. */
  turnId?: string;
  status: WorkflowRunStatus;
  nodes: Record<string, WorkflowNodeProgress>;
  errorCode?: string;
}

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

export interface KnowledgeSource {
  kind?: "knowledge";
  index: number;
  doc: string;
  section: string;
  score: number;
  knowledgeBase: string;
  snippet: string;
}

export interface WebSource {
  kind: "web";
  index: number;
  sourceId: string;
  url: string;
  title: string;
  snippet: string | null;
  retrievedAt: string;
  publishedAt: string | null;
  truncated: boolean;
}

export interface UnsupportedSource {
  kind: "unsupported";
  index: number;
  sourceId: string;
}

export type Source = KnowledgeSource | WebSource | UnsupportedSource;
