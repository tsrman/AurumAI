# Agent roles and workflow execution

AurumAI agent definitions describe a configurable model, instructions and allowed
resources. They do not create independent runtime containers. A supervisor and
node AgentWorkflow children execute in the current conversation's isolated runner.

Hermes Agent is the exception: it is a system agent, not an agent definition, and
each conversation with it gets a long-lived sandbox of its own (see
[the Hermes guide](guides/hermes-agent.md)).

## Common roles

- A general assistant answers ordinary conversational requests and selects tools.
- A research/document agent uses explicitly granted knowledge or web resources.
- A code agent executes an uploaded skill through a bounded sandbox session.
- A supervisor prepares node briefs and synthesizes successful node evidence.
- Specialist DAG nodes run their assigned instruction with immutable configuration
  snapshots and current live authorization.

These are authoring patterns, not hardcoded permissions or mandatory product agents.
Name/model/tool choice is configurable. Demo labels are examples, not an authoritative
organization catalog or license to access ungranted resources.

## Context and control

Each node receives the request, instruction, supervisor brief and current dependency
outputs. A loop's next pass additionally receives terminal results of only its
immediately previous pass. Source identities remain bounded/owned so synthesis can
validate citations. Internal briefs/thoughts/node outputs stay off public SSE.

Use versioned definitions/templates, explicit grants and bounded graph concurrency.
Human confirmation and exact-run cancellation are durable owner-controlled actions.
Auto-routing is opt-in and considers accessible workflows; failed classification
falls back for an ordinary unselected turn, not an explicit forbidden selection.

See [architecture](ARCHITECTURE.md), [configuration](CONFIGURATION.md) and
[testing](TESTING.md) for the implemented contracts and limits.
