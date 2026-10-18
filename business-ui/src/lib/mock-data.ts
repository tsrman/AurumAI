import type {
  Agent,
  AssistantMessage,
  AssistantMessageView,
  Conversation,
  Model,
  ToolCall,
} from "@/types/aurum";

type BuiltInAgentId = "general" | "analyst" | "rag" | "sql" | "swarm";
type AgentTextKey =
  | "name"
  | "kindLabel"
  | "description"
  | "capability1"
  | "capability2"
  | "capability3"
  | "suggestion1"
  | "suggestion2"
  | "suggestion3"
  | "suggestion4";

/** Translator for the `agents` message namespace. */
export type AgentTranslator = (key: `${BuiltInAgentId}.${AgentTextKey}`) => string;

/** Built-in agents; user-visible text lives in the `agents` message namespace. */
const agentCatalog: Array<Pick<Agent, "badge" | "badgeColor" | "kind"> & { id: BuiltInAgentId }> = [
  { id: "general", badge: "AI", badgeColor: "amber", kind: "chat" },
  { id: "analyst", badge: "BI", badgeColor: "blue", kind: "analyst" },
  { id: "rag", badge: "RAG", badgeColor: "green", kind: "rag" },
  { id: "sql", badge: "SQL", badgeColor: "violet", kind: "sql" },
  { id: "swarm", badge: "MAS", badgeColor: "rose", kind: "swarm" },
];

export const builtInAgentIds: readonly string[] = agentCatalog.map((agent) => agent.id);

export function localizeAgents(t: AgentTranslator): Agent[] {
  return agentCatalog.map((agent) => ({
    ...agent,
    name: t(`${agent.id}.name`),
    kindLabel: t(`${agent.id}.kindLabel`),
    description: t(`${agent.id}.description`),
    capabilities: [
      t(`${agent.id}.capability1`),
      t(`${agent.id}.capability2`),
      t(`${agent.id}.capability3`),
    ],
    suggestions: [
      t(`${agent.id}.suggestion1`),
      t(`${agent.id}.suggestion2`),
      t(`${agent.id}.suggestion3`),
      t(`${agent.id}.suggestion4`),
    ],
  }));
}

// Demo content for the unauthenticated mock mode (development only). Real
// sessions use backend data, so these fixtures are kept in English.
export const models: Model[] = [
  {
    id: "pro",
    name: "AurumAI 2 · Reasoning",
    shortName: "Reasoning",
    description: "Deep analysis with a chain of reasoning",
  },
  {
    id: "fast",
    name: "AurumAI 2 · Fast",
    shortName: "Fast",
    description: "Instant answers without long thinking",
  },
  {
    id: "local",
    name: "AurumAI · Local",
    shortName: "Local",
    description: "On-premises, no internet access",
  },
];

export function generalResp(): AssistantMessage {
  return {
    id: "",
    role: "assistant",
    agentId: "general",
    thinking:
      "Identifying the intent and the best way to answer. If needed, I will suggest a specialized agent that can solve the task more precisely.",
    tools: [],
    content:
      "I am the general assistant of the AurumAI platform. I help with questions and calculations, and for production tasks I bring in specialized agents: the Data analyst works with database metrics, the Procedures assistant answers from documents with source citations, the SQL agent builds database queries, and Agent Swarm solves complex tasks with several agents at once. Pick an agent in the sidebar or describe the task and I will suggest who should handle it.",
    widgets: [],
    sources: [],
  };
}

export function analystResp(): AssistantMessage {
  return {
    id: "",
    role: "assistant",
    agentId: "analyst",
    thinking:
      "A production metrics request. Pull recovery data for the last shift from the production database, aggregate it by hour and compare it with the 84.0% plan. Also check throughput and cyanide consumption to explain deviations.",
    tools: [
      { name: "Production database query", detail: "shift = current · 480 rows" },
      { name: "Hourly aggregation", detail: "AVG(recovery) GROUP BY hour" },
      { name: "Comparison with plan", detail: "plan = 84.0%" },
    ],
    content:
      "Average gold recovery for the last shift was 86.2%, 2.2 pp above the 84.0% plan. The peak was at 14:00 (88.1%) and the minimum at the start of the shift (83.4%) while flotation cells returned to normal mode after maintenance. Cyanide consumption fell by 1.8% while throughput grew by 4.2%, which indicates a stable reagent regime. I recommend keeping pulp pH near the upper bound of the range; it supported recovery in the second half of the shift.",
    widgets: [
      {
        type: "metrics",
        items: [
          {
            label: "Au recovery, actual",
            value: "86.2%",
            delta: "+2.2 pp vs plan",
            isPositive: true,
          },
          { label: "Throughput", value: "1,480 t", delta: "+4.2%", isPositive: true },
          { label: "NaCN consumption", value: "312 g/t", delta: "−1.8%", isPositive: true },
        ],
      },
      {
        type: "bars",
        title: "Gold recovery by shift hour",
        unit: "%",
        max: 90,
        data: [
          { label: "08:00", value: 83.4 },
          { label: "10:00", value: 85.1 },
          { label: "12:00", value: 86.8 },
          { label: "14:00", value: 88.1 },
          { label: "16:00", value: 87.2 },
          { label: "18:00", value: 86.5 },
          { label: "20:00", value: 85.9 },
        ],
      },
    ],
    sources: [],
  };
}

export function ragResp(): AssistantMessage {
  return {
    id: "",
    role: "assistant",
    agentId: "rag",
    thinking:
      "The question concerns the cyanidation process procedure. Find relevant passages in the Procedures and instructions and Plant process charts bases, rerank them by similarity and answer strictly with source citations.",
    tools: [
      { name: "Knowledge base search", detail: "2 bases · 5 passages" },
      { name: "Reranking", detail: "top 3 by relevance" },
    ],
    content:
      "According to the current procedure, pulp pH during cyanidation is kept within 10.5–11.0 [1]. Alkalization with milk of lime is done before cyanide is added; this prevents toxic HCN release and reduces reagent losses [2]. pH is checked at least once an hour and recorded in the shift log [1]. If pH drops below 10.3, lime is fed to the sump automatically [3].",
    widgets: [],
    sources: [
      {
        index: 1,
        doc: "Procedure TR-04 “Cyanidation”",
        section: "§ 4.3 — Reagent regime",
        score: 94,
        knowledgeBase: "Procedures and instructions",
        snippet:
          "Keep pulp pH during cyanidation within 10.5–11.0 by dosing milk of lime. Check the value at least once an hour and record it in the shift log.",
      },
      {
        index: 2,
        doc: "Process chart PC-12",
        section: "Pulp alkalization",
        score: 88,
        knowledgeBase: "Plant process charts",
        snippet:
          "Alkalization is performed before sodium cyanide solution is added to prevent HCN release and wasteful reagent consumption.",
      },
      {
        index: 3,
        doc: "Instruction I-07 “Process control system”",
        section: "Control loops",
        score: 81,
        knowledgeBase: "Procedures and instructions",
        snippet:
          "When pH < 10.3 the controller issues a command to feed milk of lime into the pump station sump.",
      },
    ],
  };
}

export function sqlResp(): AssistantMessage {
  return {
    id: "",
    role: "assistant",
    agentId: "sql",
    thinking:
      "Build SQL over the downtime table, group downtime by shift for the current month, sort by total duration and return the top 5.",
    tools: [
      { name: "SQL generation", detail: "PostgreSQL dialect" },
      { name: "Query execution", detail: "5 rows · 42 ms" },
    ],
    content:
      "Top 5 shifts by downtime in June: shift B-2 on 14.06 leads with 186 min across 7 events. Most of it comes from stops of mill No. 2 and spillage on conveyor C-4. Together the five shifts account for 741 min of downtime, about 31% of the month's unplanned stops. I can break down the causes for any of these shifts.",
    sql: `SELECT shift_id, shift_date,
       SUM(duration_min) AS downtime_min,
       COUNT(*) AS events
FROM downtime
WHERE shift_date >= date_trunc('month', now())
GROUP BY shift_id, shift_date
ORDER BY downtime_min DESC
LIMIT 5;`,
    widgets: [
      {
        type: "table",
        columns: ["Shift", "Date", "Downtime, min", "Events"],
        rows: [
          ["B-2", "14.06", "186", "7"],
          ["A-1", "03.06", "164", "5"],
          ["C-3", "21.06", "142", "6"],
          ["B-1", "09.06", "131", "4"],
          ["A-2", "17.06", "118", "5"],
        ],
      },
    ],
    sources: [],
  };
}

export function swarmResp(): AssistantMessage {
  return {
    id: "",
    role: "assistant",
    agentId: "swarm",
    thinking:
      "A complex task: it needs metrics, a procedure compliance check and a database export. Assign subtasks to three agents and combine the results into one report.",
    tools: [
      {
        name: "Data analyst",
        detail: "recovery and reagent consumption",
        isAgent: true,
        tag: "BI",
        badgeColor: "blue",
      },
      {
        name: "Procedures assistant",
        detail: "allowed pH and NaCN ranges",
        isAgent: true,
        tag: "RAG",
        badgeColor: "green",
      },
      {
        name: "SQL agent",
        detail: "shift downtime export",
        isAgent: true,
        tag: "SQL",
        badgeColor: "violet",
      },
    ],
    content:
      "Three agents prepared the combined report for the last shift. Analyst: recovery 86.2% (+2.2 pp vs plan), NaCN consumption within norm. Procedures assistant: pH stayed within 10.5–11.0, no deviations recorded [1]. SQL agent: total downtime 64 min, mostly mill No. 2 (40 min). Conclusion: the shift performed above plan with no critical procedure violations.",
    widgets: [
      {
        type: "metrics",
        items: [
          { label: "Au recovery", value: "86.2%", delta: "+2.2 pp", isPositive: true },
          {
            label: "Procedure compliance",
            value: "Normal",
            delta: "0 deviations",
            isPositive: true,
          },
          {
            label: "Shift downtime",
            value: "64 min",
            delta: "mill No. 2",
            isPositive: false,
          },
        ],
      },
    ],
    sources: [
      {
        index: 1,
        doc: "Procedure TR-04 “Cyanidation”",
        section: "§ 4.3 — Reagent regime",
        score: 90,
        knowledgeBase: "Procedures and instructions",
        snippet:
          "Keep pulp pH within 10.5–11.0; check it at least once an hour.",
      },
    ],
  };
}

export function customResp(agent: Agent): AssistantMessage {
  const tools = agent.tools ?? [];
  const bases = agent.bases ?? [];
  const toolCalls: ToolCall[] = [];

  if (tools.includes("Knowledge base search") && bases.length > 0) {
    toolCalls.push({
      name: "Knowledge base search",
      detail: bases.join(", "),
    });
  }
  if (tools.includes("SQL over the production database")) {
    toolCalls.push({ name: "Database query", detail: "production" });
  }
  if (tools.includes("Web search")) {
    toolCalls.push({ name: "Web search", detail: "external sources" });
  }
  toolCalls.push({
    name: "Composing the answer",
    detail: agent.model === "fast" ? "fast mode" : "reasoning",
  });

  return {
    id: "",
    role: "assistant",
    agentId: agent.id,
    thinking:
      "Applying my role and configured tools: " +
      (tools.length > 0 ? tools.join(", ") : "no tools") +
      "." +
      (bases.length > 0 ? " Connected bases: " + bases.join(", ") + "." : ""),
    tools: toolCalls,
    content:
      "Agent “" +
      agent.name +
      "” is ready. " +
      (agent.description ?? "") +
      " Describe a specific task and I will use the configured tools" +
      (bases.length > 0 ? " and connected knowledge bases" : "") +
      " to give a precise answer.",
    widgets: [],
    sources: [],
  };
}

let nextAssistantId = 1;

function makeAssistantId(): string {
  return `a${Date.now()}${nextAssistantId++}`;
}

export function doneAssistantMessage(
  agentId: string,
  response: AssistantMessage
): AssistantMessageView {
  return {
    ...response,
    id: response.id || makeAssistantId(),
    role: "assistant",
    agentId,
    thinkingShown: response.thinking,
    contentShown: response.content,
    phase: "done",
    thinkingOpen: false,
    thinkingSeconds: 3,
    startedAt: Date.now(),
    tools: response.tools.map((tool) => ({ ...tool, state: "done" as const })),
  };
}

export const seedConversations: Conversation[] = [
  {
    id: "c1",
    agentId: "analyst",
    title: "Gold recovery for the shift",
    date: "Today",
    time: "09:48",
    model: "pro", temperature: 0.7,
    lastMessageAt: "2026-07-07T09:48:00Z",
    status: "active",
    messageCount: 2,
    messages: [
      {
        id: "u1",
        role: "user",
        content: "Show gold recovery for the last shift and compare it with the plan",
      },
      doneAssistantMessage("analyst", analystResp()),
    ],
  },
  {
    id: "c2",
    agentId: "rag",
    title: "pH requirements during cyanidation",
    date: "Yesterday",
    time: "17:20",
    model: "pro", temperature: 0.7,
    lastMessageAt: "2026-07-06T17:20:00Z",
    status: "active",
    messageCount: 2,
    messages: [
      {
        id: "u2",
        role: "user",
        content:
          "What pulp pH does the current procedure require during cyanidation?",
      },
      doneAssistantMessage("rag", ragResp()),
    ],
  },
  {
    id: "c3",
    agentId: "sql",
    title: "Top 5 shifts by downtime",
    date: "Yesterday",
    time: "11:05",
    model: "pro", temperature: 0.7,
    lastMessageAt: "2026-07-06T11:05:00Z",
    status: "active",
    messageCount: 2,
    messages: [
      {
        id: "u3",
        role: "user",
        content: "Top 5 shifts by equipment downtime this month",
      },
      doneAssistantMessage("sql", sqlResp()),
    ],
  },
];
