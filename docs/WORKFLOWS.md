# Creating and running workflows

A workflow is a versioned task graph. Configure it in Admin UI at
`/admin/workflows`; run it by selecting it in the Business UI chat composer.
Saving a definition does not start a run.

## Roles and data flow

| Concept | Purpose |
| --- | --- |
| Supervisor | An existing agent that prepares one brief per step, then synthesizes the final response |
| Agent | A configured model, system prompt and allowed skill/knowledge references |
| Step/node | One task instruction assigned to an agent; the same agent may serve several roles |
| Connection | An execution dependency that passes predecessor results to the next step |
| Workflow example | Local illustrative draft with a sample request; no resources or grants are created by previewing it |
| Saved template | Server-side versioned snapshot used to create independent workflow/agent copies |

Each step receives the current request, its instruction, the supervisor brief and
outputs of its direct dependencies. The supervisor phases are automatic: do not
add a supervisor node to the graph. Up to 20 task nodes and parallelism 1–4 are
supported. Internal briefs and node outputs are not public chat messages; only a
successful final synthesis is persisted as the assistant answer.

## The five-step editor

1. **Task:** open Examples, inspect a complete scenario and choose Use this example,
   or choose Create workflow. Enter a task-oriented name and description.
2. **Agents:** assign a supervisor and each step's agent. Reusing one existing agent
   is valid; Use supervisor for all steps makes this choice explicit. An example
   does not invent agent IDs. Create an agent in the constructor if none exists.
3. **Steps:** select a graph card to edit its name, agent and task instruction.
   Connect a right output handle to the next card's left input. Add or remove steps
   as needed. Technical keys and bounded loop controls are in advanced step settings.
4. **Check:** resolve the listed missing fields, validate on the server, then save.
   Every edit invalidates validation. A server conflict or error retains the draft.
5. **Launch:** configure explicit workflow/dependency access, open chat, select the
   saved workflow and send the example request or your own task. Example requests
   can be copied from this step. Saving alone does not execute the process.

Navigation asks before discarding an unsaved draft. Reloading or closing a dirty
editor uses the browser's leave-page confirmation; the draft is not persisted in
browser storage. A background detail refresh cannot overwrite the draft or silently
advance its optimistic-concurrency version.

Model IDs are configurable. `deepseek-flash-free` can be selected when the gateway
reports it and the initiating user has the required live access. Model defaults
are configuration, not access grants. Intended users need access to the workflow,
its agents/models and any referenced skills/knowledge resources. New template
copies do not copy permissions or enable automatic routing.

## Connections, branches and confirmation

- On success runs the successor after successful execution. On failure requires an
  actual failed execution; an answer saying "needs revision" is still a successful
  answer and is not a business-condition branch.
- Multiple incoming connections use AND semantics: all predecessor outcomes must
  match. Do not merge mutually exclusive success/failure paths as an OR-join.
- Independent roots may run concurrently within the configured bound. An unhandled
  failure stops successful synthesis; blocked downstream tasks can be skipped.
- Confirmation pauses BEFORE a selected node executes. It does not approve an
  unimplemented email, payment or deployment action. The conversation owner approves
  or rejects through chat progress.
- Loops are outcome-based and bounded to 2–20 iterations. They are not an implicit
  quality evaluator. Nested/overlapping loop bodies, confirmation inside a loop and
  non-tail fan-out are unsupported. See [architecture](ARCHITECTURE.md).

## Examples

The following inputs are complete illustrative tasks, not records of performed
external operations. They require a configured agent/model and no external tools.
Select an example to load its graph, then explicitly map the agents before saving.
For the supervisor, use instructions to prepare specific step briefs and produce
an evidence-based final response without claiming unperformed actions. For task
agents, request fact/assumption separation and use of the provided context.

### 1. Customer support reply

Prepare a policy-compliant response to a delayed delivery, ready for an operator to send.

Check facts → Draft reply → Review reply. Each step waits for its predecessor. Optional confirmation on the review step pauses before review starts.

Sample chat request:

> Draft an English reply to Maya about order A-1042. Delivery was promised on October 20; today is October 24. Carrier update: delayed at sorting center, estimated delivery October 26. Our supplied policy allows a shipping-fee refund after more than two days of delay. The fee was EUR 8. Product refunds require a carrier investigation. Apologize, explain the available option and ask for consent. Do not claim a refund was already issued.

| Step key | Name | Instruction |
| --- | --- | --- |
| `triage` | Check facts and policy | Extract the order ID, promised and estimated delivery dates, delay duration, supplied policy and allowed remedy. List prohibited claims. Use only the user request. |
| `reply` | Draft the customer reply | Use the predecessor facts to write a concise, empathetic reply. Present the carrier date as an estimate, offer the supplied remedy and ask for consent. Do not invent refunds or investigation outcomes. |
| `review` | Review the reply | Check the draft against the supplied facts and policy. Return a corrected customer reply and a short operator note on uncertainty. Do not send anything or issue a refund. |

Expected result: A customer reply citing order A-1042, the carrier’s October 26 estimate and the EUR 8 shipping-fee refund option, plus an operator note about uncertainties. No email is sent and no refund is issued.

Set parallelism to 1. Leave auto-routing off for the first explicit test.

### 2. Workshop campaign plan

Combine two parallel specialists into a practical campaign with content, a budget and metrics.

Audience analysis and Content angles run independently, up to two at once. Both connect to Campaign plan, which waits for both successful results.

Sample chat request:

> Prepare a campaign for a free 45-minute online workshop, “Automate your weekly reports”, on November 12 at 14:00 UTC. Audience: operations leads in teams of 10–50. Available channels: 800 opted-in email contacts and the company LinkedIn page. Target: 40 registrations. Budget: EUR 300. Deliver a seven-day schedule, three LinkedIn post drafts, one email draft and success metrics. Do not access accounts or promise registrations.

| Step key | Name | Instruction |
| --- | --- | --- |
| `audience` | Analyze the audience | Analyze the supplied audience, reporting pain points, constraints and registration target. Separate assumptions from supplied facts. Do not invent market statistics. |
| `messages` | Develop content angles | Propose three content angles for the supplied workshop, each with a value proposition, likely objection and call to action. Preserve the event details. Do not publish. |
| `campaign` | Build the campaign plan | Combine both predecessor results into a seven-day schedule with three LinkedIn drafts, one email draft, budget totaling at most EUR 300 and measurable registration metrics. Clearly label proposals. |

Expected result: A seven-day campaign schedule, three post drafts, one email draft, budget allocation of at most EUR 300 and measurable registration metrics. Nothing is published or sent.

Set parallelism to 2. Leave auto-routing off for the first explicit test.

### 3. Technical incident assessment

Turn supplied logs into an evidence-based diagnosis and a reversible recovery plan.

Extract evidence → Rank hypotheses → Draft runbook. The supervisor combines the completed work into the final assessment.

Sample chat request:

> Deployment app-v42 completed at 09:12 UTC, followed by proxy 502 errors. Proxy log: connect() failed (111: Connection refused) to upstream http://app:8000/health. New application log: Server listening on 0.0.0.0:8080. The previous deployment listened on port 8000. No database errors appear in the supplied excerpt. Assess the incident and propose a reversible recovery plan. You have no cluster access. Do not execute commands or claim recovery.

| Step key | Name | Instruction |
| --- | --- | --- |
| `evidence` | Extract incident evidence | Build a timeline and list observed facts, unknowns and the relevant port mismatch. Treat the absence of a database error in this excerpt as limited evidence, not proof. |
| `hypotheses` | Rank likely causes | Use the predecessor evidence to rank plausible causes. Explain supporting facts and read-only checks that would distinguish the causes. Separate confidence from fact. |
| `runbook` | Draft a recovery runbook | Use the ranked causes to propose reversible recovery: verify listener/upstream configuration, align routing or roll back to the known previous deployment, verify health and traffic. Include prerequisites and rollback conditions. Claim no executed checks or recovery. |

Expected result: A diagnosis grounded in the observed 8000/8080 port mismatch, a verification checklist and a reversible recovery proposal. No infrastructure checks or changes are executed.

Set parallelism to 1. Leave auto-routing off for the first explicit test.

## Templates and diagnostics

Use Saved templates for capturing or copying saved, versioned definitions. Template
archival does not alter existing copies. Templates are separate from the built-in
local example gallery.

Run diagnostics is an administrative view. Paste a workflow run UUID from chat
progress, then request diagnostics. Missing or inaccessible runs produce an error.
Retention may remove node output; an expired output does not mean the run never
existed. Keep internal diagnostics out of public SSE and shared support artifacts.

## Testing useful tools

Workflow examples above need no tools. For independent skill/MCP integration checks,
install the [starter tools](STARTER_TOOLS.md), explicitly assign the relevant skill
to an authorized agent, then use the documented CSV, JSON, sum and text examples.
A model answering a sum from memory does not demonstrate an actual MCP call.

See [testing](TESTING.md) for real-provider and browser acceptance contracts.
