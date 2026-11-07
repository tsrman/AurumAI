# Changing models and providers

How AurumAI knows which models exist, where a model's name is stored, and what to do
when your gateway adds, renames, replaces or removes one. AurumAI does not version
models: a model is the **exact id** your gateway publishes. A provider's new revision
is, to the platform, a different model.

## Where the catalog comes from

The Admin app's **Models** page reads `GET <LLM_GATEWAY_URL>/v1/models` from the
gateway, using `GATEWAY_API_KEY`, each time it is opened. Nothing is copied into a
local catalog. The status bar shows the gateway's state:

| State | Meaning |
|---|---|
| Available | The gateway answered with a list |
| Unavailable | The request failed or the answer was not a model list; the page lists nothing and saving model settings returns `503` |
| Mock | Only in development (`APP_DEBUG=true`) with no gateway key: three sample ids |

Only chat models are offered. Ids that mark embedding, realtime speech, transcription,
text-to-speech or reranking models (`text-embedding`, `speech-realtime`, `/rerank`,
`whisper`, `tts-`) are hidden from chat pickers, because those models reject chat
completions. Image models are chosen separately under **Models → Images**.

## Where a model's id is stored

An id is copied into each of these. A rename in the gateway does not follow it:

| Stored in | Set in |
|---|---|
| Access grants (who may use the model) | Admin → Models, per model |
| The organization's default model | Admin → Models → Defaults |
| An agent's model | The agent's editor |
| The organization's Hermes model | Admin → Models, Hermes |
| The organization's image generation and editing models, and the chat models marked as taking images | Admin → Models → Images |
| The evaluator | `EVALUATOR_MODEL` in `.env` (with `EVALUATOR_ENABLED=true`) |
| Workflows | Through the agents on their nodes |

Access is **default-deny**: a new id exists for nobody until you grant it. When a model
disappears from the gateway, its grants stay in the database but the Models page stops
listing it, and the default shows as unavailable. A request that still names it reaches
the gateway, which will not serve it.

## Adding a model

1. Add it in the gateway and check that `GET /v1/models` lists it.
2. Admin → Models: grant it to the organization, groups or people who should use it
   (preview first, then apply).
3. Optionally make it the default, mark it as taking images, or pick it for an agent.

## Replacing a model (a new revision, a rename, a provider switch)

Do it in this order so nobody is left without a working model:

1. Add the **new** id in the gateway and grant it exactly as the old one was granted.
2. Move the places in the table above: the default, the Hermes model, image models,
   the evaluator, then the agents that named the old id. Try each with one message.
3. Only then remove the old id from the gateway.
4. Remove the old grants if you want the list tidy; they grant nothing once the id is gone.

An agent or workflow you forget to move keeps naming the old id and fails once the
gateway stops serving it; try each one before step 3.

## Removing a model

Find what names it first: the default, the Hermes and image settings, the evaluator and
the agents. Archive or edit those, then remove the id from the gateway. Every
conversation stores the model it started with: past ones stay readable, but continuing
one needs a model you still serve.

## Switching the gateway

A different gateway means a different set of ids. Set `LLM_GATEWAY_URL` and
`GATEWAY_API_KEY` in `.env` and recreate both API planes
(`docker compose up -d --force-recreate backend backend-admin`), then work through
"Replacing a model" for every id that differs. Keep `GATEWAY_API_KEY` in the secret
store ([secrets](secrets.md)); the runner always calls the backend's proxy with its own
session token, so authorization and the monthly quota apply whichever gateway sits behind.

## Checks

- The Models page says **Available** and lists the new id.
- A member with access sends one message on it; a member without access cannot pick it.
- Hermes, image tools and workflows each run once if the change touched their settings.
- Usage analytics (Admin → Analytics) shows requests and tokens under the new id; the
  monthly request and token limits belong to the organization, not to a model, so a
  switch does not reset them.
