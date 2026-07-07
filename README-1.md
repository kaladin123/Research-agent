# Research Agent / Context Engine — Federated MVP (F1872578)

Google ADK implementation of the central Research Agent operating model with a USP pilot LOB agent behind an MCP boundary. Runs end to end locally: the USP LOB agent is an MCP server with its own context engine (per-source retrieval → reciprocal rank fusion → tier-1 conflict handling → synthesis); the central Research Agent is an ADK `SequentialAgent` with a deterministic-exit `LoopAgent` that consults it.

The demo corpus is deliberately seeded with a real conflict (the ingestion SIPOC says retries are manual; the retry coordinator code says automatic), so a single run demonstrates the full acceptance arc: conflict detected → escalated over MCP → loop refines its question → authoritative deployment note resolves it → cross-round reconciliation → cited final answer.

## Layout

```
common/contracts.py                  Shared Pydantic contract (the federation boundary)
common/a2a_wrapper.py                Generic A2A server wrapper (executor + serve helper)
usp_lob_agent/                       USP LOB Agent / Context Engine
  retrievers.py                      Per-source retrieval channels + demo corpus
  rrf.py                             Reciprocal rank fusion
  context_engine.py                  retrieve → fuse → conflicts → synthesize
  server.py                          MCP server (:8001), one coarse tool: usp_research
  agent_card.py                      AgentCard + AgentSkill (A2A compliance)
  a2a_server.py                      A2A server (:8002), same engine, second transport
central_research_agent/              ADK APP — central Research Agent
  config.py                          LiteLLM (Azure GPT-4.1) + transport selection
  agent.py                           root SequentialAgent (exposes root_agent)
  agent_card.py                      AgentCard + AgentSkill (A2A compliance)
  a2a_server.py                      A2A server (:8003) exposing the full pipeline
  a2a_client_tool.py                 usp_research over A2A (LOB_TRANSPORT=a2a)
  sub_agents/
    decompose.py                     step 1
    plan.py                          step 2
    consult.py                       steps 3–4 (transport-selected tool)
    record_finding.py                deterministic state append + failure boundary
    evaluate.py                      step 5a (LoopStatus)
    exit_checker.py                  step 5b (deterministic gate)
    reconcile.py                     step 6 (tier-2, cross-round)
    synthesize.py                    step 7
main.py                              Scripted end-to-end demo with event tracing
a2a_smoke_test.py                    Compliance check: cards + skills + live invocation
```

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in Azure OpenAI credentials

# Terminal 1 — the USP LOB agent (MCP server on :8001)
python -m usp_lob_agent.server

# Terminal 2 — the central Research Agent
python main.py "How does the USP ingestion pipeline handle failures and retries?"
```

`adk web` / `adk run central_research_agent` also work — `root_agent` is exposed the way ADK discovery expects. The USP context engine falls back to a deterministic template for finding synthesis if Azure credentials are absent, so the pipeline mechanics can be exercised without a key (the central agent's LLM steps still need one).

## A2A compliance (AgentCard + AgentSkill — mandatory)

Every deployable agent publishes an AgentCard with at least one AgentSkill and is invocable as an A2A peer:

| Agent | Card (well-known) | Skill | Serve with |
|---|---|---|---|
| USP LOB Agent | `http://localhost:8002/.well-known/agent-card.json` | `usp_research` | `python -m usp_lob_agent.a2a_server` |
| Central Research Agent | `http://localhost:8003/.well-known/agent-card.json` | `federated_research` | `python -m central_research_agent.a2a_server` |

Verify compliance end to end (card resolution + skill presence + a live `message/send` invocation):

```bash
python a2a_smoke_test.py            # USP; add --with-ra for the RA card too
```

Design decisions worth knowing:

- **Skill = the federation contract.** The `usp_research` skill mirrors the MCP tool 1:1 — same coarse-grained request/response. A2A compliance adds a transport and discoverability; it does not move any reasoning back to the center.
- **Dual exposure, one engine.** The USP agent serves the same `context_engine` over MCP (:8001, per F1872578's "MCP as the interaction layer") and A2A (:8002, per the internal rule). No logic is duplicated.
- **Runtime transport is a config switch.** `LOB_TRANSPORT=mcp` (default) uses the MCPToolset; `LOB_TRANSPORT=a2a` swaps in `a2a_client_tool.usp_research` — same function name, same contract, so the consult instruction, findings shape, evaluator, and exit logic are untouched. **Open question to confirm with the architecture owner:** does the internal A2A rule require the RA↔LOB *runtime* path over A2A, or only card exposure? The spec says MCP; this repo supports both so the decision is a one-line env change.
- **Internal pipeline steps don't get cards.** The rule is interpreted as applying to deployable agent *services* (USP agent, central RA), not to sub-agents inside the ADK pipeline (decompose, evaluate, etc.) — those are implementation details of one service, not peers.
- The RA's A2A server runs each incoming message as a fresh, stateless research invocation (new ADK session per request) — consistent with "loop memory lives in session state for one run only."

## New-LOB onboarding checklist (now with A2A)

1. Implement the context engine behind the same `<lob>_research` contract.
2. Expose it over MCP (spec) and A2A (rule) — `common/a2a_wrapper.py` makes the A2A side ~30 lines: define an `AgentCard` + `AgentSkill`, point `serve_a2a` at your handler.
3. Add routing knowledge to `plan_agent`. Loop, evaluator, reconciliation: zero changes.

## How the pipeline maps to the spec

| Spec step | Component | Notes |
|---|---|---|
| Decompose query | `decompose_agent` | `output_schema=Decomposition` |
| Plan domain/LOB consultation | `plan_agent` | Explicit plan artifact in state — auditable proof of central orchestration; the only step that grows when new LOBs join |
| Invoke LOB agent | `consult_agent` | `MCPToolset` → `usp_research`; no `output_schema` (ADK: schema disables tools) |
| LOB retrieval/ranking/RRF/synthesis | `context_engine.py` | Entirely behind the MCP boundary — RA never sees raw documents |
| Iterative refinement | `evaluate_agent` + `exit_checker` | `conflict_detected` promoted to a `LoopStatus` object (conflict / incomplete / competing); exit is code, not sampling |
| Resolve cross-step conflicts | `reconcile_agent` | Separate from synthesis so resolution is a visible event; stand-in for future cross-LOB reconciliation |
| Assemble final response | `synthesis_agent` | Cites source_ids, states resolved conflicts, caveats partial results |

## The loop exit contract

Exit when `verdict == done` OR no progress (newest finding repeats a previous one, by hash) OR `max_iterations=4`. On forced exits, downstream agents see `loop_exit.cause` and must caveat — the answer to "what happens when USP is down or unhelpful?" is a partial, honest answer, not a hang.

Failure boundary: `record_finding` converts unparseable/failed consultations into a synthetic `coverage: "none"` finding so the loop and synthesis handle outages gracefully.

## The MCP contract (federation boundary)

One coarse-grained tool per LOB — `usp_research(sub_question, context, constraints)` — returning `finding`, `confidence`, `coverage`, `evidence[]` (with `fused_rank` proving RRF ran behind the boundary), `unresolved_conflicts[]`, and `suggested_followups[]`.

Deliberately NOT `search_sipocs` / `search_codebase` / etc. — fine-grained tools would pull source selection and merging back into the central agent and silently violate federation.

Conflict policy inside the LOB: resolve what it can (authoritative sources like production deployment notes win), escalate what it can't via `unresolved_conflicts`. `coverage`/`confidence` give the evaluator structured signals so the loop decision never depends on vibe-checking prose.

Onboarding a new LOB (Optum Rx, XLOB) = implement the same tool shape + add routing knowledge to `plan_agent`. Loop, evaluator, reconciliation: zero changes.

## Version notes / caveats

- **a2a-sdk is pinned to 0.x** (`a2a-sdk[http-server]>=0.2.16,<1.0`) — the API line Google ADK's A2A integration is built against (`AgentCard`/`AgentSkill` as pydantic models, `A2AStarletteApplication`). 1.x is a protobuf-based rewrite with a different server layout; migrate deliberately. Within 0.3.x, `A2AClient` is deprecated in favor of `ClientFactory` — it still works and matches all current ADK-era samples; swap when you upgrade.
- The card is served at `/.well-known/agent-card.json` (current path; older SDKs used `/.well-known/agent.json` — `A2ACardResolver` handles this).
- ADK moves fast. `config.usp_mcp_toolset()` tries the current `StreamableHTTPConnectionParams` import and falls back to the older `StreamableHTTPServerParams`. If your ADK version differs, this is the only place to touch.
- Instruction templating uses `{state_key}` (and `{key?}` for optional keys) — avoid literal `{...}` JSON inside instruction strings, ADK will treat braces as state references.
- State writes from custom agents go through `EventActions(state_delta=...)` (persisted correctly across session services), not direct `ctx.session.state` mutation.
- `output_schema` on an `LlmAgent` disables tool use — that's why `consult_agent` outputs raw JSON parsed by `record_finding` instead.

## Path to production

- Replace `retrievers.py` with real channels over the migrated USP vector stores (post one-time migration), code search, and SharePoint — the channel interface and RRF stay as-is.
- Replace claim-based conflict detection with an LLM comparison pass over retrieved excerpts (keep the resolve-locally-vs-escalate policy).
- Add auth on both paths (API credentials for the operational path; MCP identity for the research path) — currently absent.
- Wire your httpx event-hook logging into the MCP client session for per-call tracing, and log `plan`, `loop_status`, `loop_exit`, and `reconciliation` state keys — together they are the demo evidence for the acceptance criteria.
- Build a small golden set of USP queries with expected evidence to make "high-quality runtime research" measurable.
