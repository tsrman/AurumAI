export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  model?: string;
}

export interface ConversationResponse {
  id: string;
  title: string | null;
  model: string;
  temperature: number;
  status: string;
  lastMessageAt: string | null;
  createdAt: string;
  messageCount: number;
  tokenCount: number;
  costUsd: number;
}

export interface MessageResponse {
  id: string;
  role: string;
  content: string | null;
  model: string | null;
  tokensIn: number;
  tokensOut: number;
  costUsd: number;
  latencyMs: number | null;
  toolCalls: unknown[] | null;
  // Keyed by toolCalls[].call_id — see business-backend's MessageResponse.
  toolResults: Record<string, unknown> | null;
  createdAt: string;
  faithfulnessScore?: number | null;
  faithfulnessReason?: string | null;
  turnId?: string | null;
  sourceIds?: string[];
  unsupportedSourceIds?: string[];
}

export type QuestionStatus = "pending" | "answered" | "expired" | "cancelled";

export interface QuestionOptionResponse {
  id: string;
  label: string;
}

export interface QuestionAnswer {
  text?: string;
  optionId?: string;
}

export interface QuestionResponse {
  questionId: string;
  turnId: string;
  text: string;
  options: QuestionOptionResponse[];
  status: QuestionStatus;
  version: number;
  expiresAt: string;
  createdAt: string;
  phase: "ordinary" | "node" | "supervisor_prepare" | "supervisor_synthesize";
  nodeKey: string | null;
  agentName: string | null;
  answer: QuestionAnswer | null;
  answeredAt: string | null;
}

export type TurnStatus = "active" | "completed" | "failed" | "cancelled";

export interface TurnQuestionState {
  questionId: string;
  status: QuestionStatus;
  version: number;
}

export interface TurnSnapshotResponse {
  turnId: string;
  status: TurnStatus;
  version: number;
  questions: TurnQuestionState[];
}

export type TurnStreamEvent =
  | {
      type: "question-status";
      data: TurnQuestionState & { turnId: string };
    }
  | {
      type: "turn-status";
      data: { turnId: string; status: TurnStatus; version: number };
    };

export interface ChatRequest {
  conversation_id: string;
  messages: ChatMessage[];
  model: string;
  options?: {
    temperature?: number;
    max_tokens?: number;
    tools?: string[];
  };
}

export type WorkflowRunSSEStatus =
  | "pending"
  | "planning"
  | "running"
  | "synthesizing"
  | "completed"
  | "failed"
  | "cancelled";

export type WorkflowNodeSSEStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type SSEEvent =
  | { type: "message-start"; data: { role: string; turn_id: string } }
  | { type: "thinking-delta"; data: { delta: string; turn_id: string } }
  | { type: "content-delta"; data: { delta: string; turn_id: string } }
  | {
      type: "tool-calls";
      data: {
        // Matches conversation-runner's publish_event("tool-calls", ...)
        // payload — the raw parsed <tool_call>{"skill_id":...,"args":...}
        // JSON, not "name"/"arguments".
        calls: Array<{
          call_id: string;
          skill_id?: string;
          tool?: "web_search" | "web_read";
          label?: string;
          args?: Record<string, unknown>;
        }>;
        turn_id: string;
      };
    }
  | {
      type: "tool-result";
      data: {
        call_id: string;
        skill_id?: string;
        tool?: "web_search" | "web_read";
        label?: string;
        result: unknown;
        turn_id: string;
      };
    }
  // Multi-round code-skill sandbox progress (CodeSkillSessionWorkflow):
  // one run-command/run-result pair per executed shell command.
  | {
      type: "run-command";
      data: { skill_id: string; round: number; command: string; turn_id: string | null };
    }
  | {
      type: "run-result";
      data: {
        skill_id: string;
        round: number;
        result: {
          stdout?: string;
          stderr?: string;
          exit_code?: number | null;
          timed_out?: boolean;
        };
        turn_id: string | null;
      };
    }
  | { type: "message-end"; data: { role: string; content: string; turn_id: string } }
  | { type: "done"; data: { turn_id: string; evaluation_pending?: boolean } }
  | {
      type: "workflow-run-status";
      data: {
        turnId: string;
        workflowRunId: string;
        status: WorkflowRunSSEStatus;
        errorCode?: string;
      };
    }
  | {
      type: "workflow-node-status";
      data: {
        turnId: string;
        workflowRunId: string;
        nodeKey: string;
        nodeName: string;
        status: WorkflowNodeSSEStatus;
        errorCode?: string;
      };
    }
  | {
      type: "message-score";
      data: { turn_id: string; score: number | null; reason: string | null };
    }
  | TurnStreamEvent;
