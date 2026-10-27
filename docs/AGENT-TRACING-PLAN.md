# Agent tracing implementation plan

Status: proposed; documentation only. No implementation is authorized by this document.
Prepared: 25 October 2026.
Repository: PromptKeep.
Planning baseline: the local source inspected for this proposal; schema version 8.

## 1. Decision and intended outcome

Add a small, explicit way to record an agent task and the application tools it uses.
Continue to use the existing wrapped model calls, prompt versions, checks, SQLite file,
background writer, and history API.

A developer should be able to inspect a task and answer:
- Which model calls happened, and which prompt versions drove them?
- Which application tools actually ran?
- What inputs did a tool receive and what output or error did it produce?
- In what order did operations start, how long did each take, and which scopes contained them?
- Which operations failed, were cancelled, or were blocked?
- What token usage and provider-reported cost is known for the model calls?

The implementation should explain the execution the application explicitly instrumented.
It must not claim that uninstrumented functions or provider-internal work were observed.

### 1.1 Change from the earlier broad proposal

The earlier discussion proposed separate agent-run and agent-step tables, a tool execution
helper, CLI commands, and dashboard views in one release.

This plan deliberately reduces that first release:

1. Use one new table for agent, tool, and model spans.
2. Add one optional span reference to existing LLM run rows.
3. Expose two context managers rather than a tool executor, decorator family, or agent runtime.
4. Deliver Python history reads first.
5. Add a small CLI view next, and a dashboard only if the first slice proves useful.
6. Keep provider-specific tool parsing and framework integrations outside the core release.

This is a scope reduction in response to the requirement to avoid codebase bloat.
The database design below is a proposal to review before implementation, not an existing schema.

### 1.2 Product boundary

PromptKeep records operations the application performs. The application remains responsible
for choosing tools, invoking functions, managing retries, awaiting tasks, and consuming streams.

Existing provider support remains the starting point: wrapped Chat Completions and
Responses create calls, including compatible endpoints. An agent scope does not make an
unsupported provider client observable.

## 2. Existing implementation and reuse points

Use current source as the authority. The state and roadmap documents contain historical
sections that do not all describe the latest source.

| Existing code | Current responsibility | Reuse in this feature |
| --- | --- | --- |
| `prompts.py`, `rendering.py` | Prompt identity, immutable templates, normalization, variables | Keep prompt lineage as the source of truth |
| `conversation.py` | Context-local conversation grouping, sync and async scopes | Follow its context-variable pattern; preserve session semantics |
| `models.py` | Peewee models, client-minted run keys | Add a span model and one nullable run link |
| `migrations.py` | Forward-only SQLite migrations; currently version 8 | Add the next migration without editing historical steps |
| `controls.py` | Sampling and redaction before persistence | Extend existing controls to span payloads |
| `storage.py` | Every write, queue dispatch, retention | Add shielded span writes and grouped cleanup here |
| `writer.py` | Bounded queue, batching, drain, fork reset | Reuse the same queue and sink |
| `tracking.py` | Prompt run recording, feedback, flush | Preserve the existing flush contract |
| `integrations/core.py` | Shared call lifecycle, checks, streaming, run recording | Capture one model span per physical call |
| `integrations/base.py` and adapters | Provider shapes and response extraction | No provider shape changes required for the first slice |
| `history.py` | Queries and frozen read dataclasses | Add trace reads and linked run projections |
| `cli.py` | Presentation over history reads | Optional next slice; no direct SQL |
| `dashboard/` | Local, offline, read-only views | Optional follow-up over the same history reads |
| `tests/test_package.py`, `docs/API.md` | Public API and import layering contracts | Update deliberately when public names are added |

### 2.1 Existing details that affect the design

- A call is currently recorded when it includes a tracked Prompt, belongs to a conversation,
  or has checks. An active agent scope must become another reason to record.
- A physical model call can generate several run rows when it carries multiple Prompts.
  Those rows share response information; they must represent one model span.
- Run keys exist before rows reach SQLite. Span keys must follow the same rule.
- Async and streaming orchestration already exists in the shared integration core.
  Extend that orchestration rather than adding a parallel interceptor.
- Implicit telemetry failures are shielded. History reads raise normally.
- The writer can drop old queued items when full. Relationships must tolerate missing rows.
- Redaction happens before data enters the queue. Tool and agent payloads need the same treatment.
- Existing conversation replay is text-only. A trace view must not pretend to be a complete
  provider transcript or an exact replay format.
- Context variables isolate task contexts, but arbitrary new threads and external processes
  do not automatically receive them.
- The original root `plan.md` is historical according to AGENTS.md. Leave it intact.

## 3. Scope and release slices

### 3.1 Slice A: smallest useful release

Required:

- An `agent(...)` context manager for a logical task.
- A `tool(...)` context manager for an application-executed tool.
- Both `with` and `async with` forms.
- A shared returned handle with stable keys and an explicit output method.
- Automatic association of existing wrapped model calls with the active scope.
- One model span per physical model call, including calls with several tracked Prompts.
- Agent and tool input/output capture through the existing redaction path.
- Success, error, cancellation, and blocked-model-call outcomes.
- Ordered spans, explicit containment, and links to existing model run rows.
- Python history reads to list root traces and inspect one trace.
- Existing background, sync, off, disabled, suppression, flush, and retention behavior.
- Focused regression coverage, one offline example, and API documentation.

Slice A is releasable on its own. It must be useful without the CLI or dashboard.

### 3.2 Slice B: small inspection surface

After Slice A:

- One CLI trace command with list and detail forms.
- Plain aligned output that uses history functions.
- Optional JSON output through that command if consumers actually need it.
- Useful missing-data and partial-sampling annotations.

Do not introduce a general reporting command framework.

### 3.3 Slice C: optional dashboard

After exercising Slice A and B:

- One trace list page.
- One trace detail page with an ordered, indented list of spans.
- Model spans show existing run details, checks, and prompt-version links.
- Tool spans show redacted input, output, error, and duration.
- Keep the existing server, templates, styles, and offline behavior.

A graphical node editor, graph-layout package, live websocket feed, or new frontend stack
is outside this slice.

### 3.4 Deferred work

These require separate evidence and estimates:

- Framework-specific lifecycle adapters.
- Direct Anthropic SDK support or another provider adapter.
- Automatic recording of provider-hosted web search and other server-side tools.
- Parsing all model tool requests and all tool-output message shapes.
- Tool-call causality inferred from provider call IDs.
- Exact agent replay, automatic re-execution, or state checkpointing.
- New evaluators, agent-level checks, approval workflows, or tool access policies.
- Distributed traces across worker processes, services, or machines.
- Automatic context injection into arbitrary threads and executors.
- Exporters for third-party observability services or OpenTelemetry.
- A generic event bus, plugin framework, or event sourcing subsystem.
- Persistent retries, queues for agent execution, schedulers, or workflow engines.
- Binary artifacts, screenshots, downloaded files, and attachment storage.

## 4. Clarify what “web search support” means

There are two different execution locations.

### 4.1 Application-executed search: included

The application calls a Python search function. Wrap that call with `tool(...)`.
PromptKeep records the supplied input, explicitly supplied output, timing, and outcome.

This works whether the tool uses an HTTP search service, a local index, SQL, or application
code. PromptKeep does not need a search-provider adapter to time a Python scope.

### 4.2 Provider-hosted search: deferred

A model provider may run a search tool internally during one model API request.
There is no application function for the tool context to wrap.

That requires provider-specific response or stream parsing. A future adapter can record
only the fields the provider actually exposes. Tool-internal timing, full search results,
and separate cost must remain unknown when the provider does not return them.

The first release must describe this limit explicitly. It must not advertise automatic
capture of all web search activity.

## 5. Proposed public API

API names below are proposed. Review once before implementation, then keep them small.

### 5.1 Synchronous usage

```python
import promptkeep

# client is already wrapped using promptkeep.wrap(...).
with promptkeep.agent("research", input={"question": question}) as task:
    first = client.responses.create(...)

    with promptkeep.tool("web_search", input={"query": query}) as search_step:
        results = web_search(query)
        search_step.output(results)

    final = client.responses.create(...)
    task.output(final.output_text)

promptkeep.flush()
record = promptkeep.history.trace(task.trace_key)
```

The ellipses are application-specific model requests. This is a usage sketch, not a
complete agent implementation.

### 5.2 Asynchronous usage

```python
async with promptkeep.agent("research", input={"question": question}) as task:
    first = await client.responses.create(...)

    async with promptkeep.tool("web_search", input={"query": query}) as search_step:
        results = await web_search(query)
        search_step.output(results)

    final = await client.responses.create(...)
    task.output(final.output_text)
```

The context managers do not invoke the tool or decide whether to await it.
They surround the application operation, which keeps the API free of callable inspection,
argument binding, wrappers around arbitrary signatures, and sync/async executor variants.

### 5.3 Entry points

Proposed signatures:

```python
agent(name: str, *, input: object = None)
tool(name: str, *, input: object = None)
```

Start with these arguments. Add neither arbitrary metadata nor per-call policy arguments
until a concrete first-release use case demonstrates a need.

The returned handle exposes:

- `span_key`: the scope's client-minted UUID.
- `trace_key`: the top-level agent span's UUID.
- `output(value)`: set the optional output to record on exit.

The concrete handle class need not become another top-level export. Its documented methods
and properties are still part of the public return contract.

### 5.4 Deliberately small API behavior

- Only two new top-level names: `agent` and `tool`.
- Use `agents.py` as the module name to avoid the package-name collision pattern already
  avoided by `prompts.py`.
- There is no `run_tool`, `arun_tool`, decorator API, automatic function patcher, or tool registry.
- Tools are explicitly named. Do not infer names from frames, source files, or stack traces.
- Input is supplied explicitly. Do not introspect locals or scrape function arguments.
- Output is supplied explicitly. Omitted output means “not recorded,” not “the tool returned null.”
- Calling `output(...)` repeatedly keeps the last supplied value; document and test this.
- Distinguish omitted output from explicitly supplied `None` through an internal sentinel.
- Handles expose identifiers immediately even while storage is disabled or rows are queued.
  A key identifies an attempted trace; it does not prove persistence.
- Do not expose counters, mutable internal context objects, queue items, or Peewee records.
- Use one private scope implementation for shared enter/exit behavior.

### 5.5 Usage errors versus telemetry failures

Validate non-empty string names before entry. Invalid explicit API use may raise.

A `tool(...)` scope requires an active `agent(...)` scope. Raise a clear usage error when
entered without one. Do not invent an anonymous root or create another execution mode.

After successful entry, failures in serialization, redaction, queueing, or persistence must
never replace application results or exceptions.

An output call on a closed handle is a usage error; document it. Re-entering the same active
scope object is unsupported and should raise clearly. Creating a new scope per use is normal.

## 6. Trace structure and semantics

### 6.1 Relationship to conversations

A conversation groups model messages for a user session. A trace groups operations for one
agent task. A single conversation can contain several agent tasks.

They may coexist:

```python
with promptkeep.conversation("session-42"):
    with promptkeep.agent("answer-question"):
        ...
    with promptkeep.agent("summarize-results"):
        ...
```

Do not encode the task identity into the conversation title or create synthetic conversation
turns for tools. Tool steps must not alter the meaning of existing conversation replay.

The initial implementation does not need a hard foreign key from trace roots to conversations.
The existing model run links preserve the connection where model calls belong to both.

### 6.2 Span kinds

Use exactly three kinds in the first release:

- `agent`: an explicitly entered agent scope.
- `tool`: an explicitly entered application tool scope.
- `llm`: one physical wrapped model call.

The top-level agent span is also the trace root. Its `trace_key` equals its `span_key`.
An agent entered inside an existing agent or tool becomes a child span in the same trace.

Avoid inventing an abstract task graph. These kinds are the operations required by the feature.

### 6.3 Containment and ordering

- `parent_span_key` expresses the active enclosing scope at operation start.
- Two sequential operations under one agent are siblings.
- Do not infer that a tool was caused by the last model call.
- Do not repurpose `runs.parent_run_key`: it already expresses conversation continuation.
- Sort the timeline by recorded start time with a stable key tie-breaker.
- Concurrent sibling spans can overlap. The UI must not imply they executed sequentially.
- No persistent sequence allocator is needed for the first release.
- Record wall timestamps for inspection and monotonic elapsed time for duration.

A model call inside a tool scope becomes a child of that tool. A nested agent has the same
trace key and an agent-kind parent/child relationship.

### 6.4 Root status

Root status describes the agent scope's own exit:

- Normal exit: `ok`.
- Exception escaping the scope: `error`.
- Cancellation escaping the scope: `cancelled`.
- Entered and not finalized on disk: `running`.

A tool error caught by application code does not automatically change the root exit to error.
Trace read results separately expose child failures so a recovered failure remains visible.

Do not collapse block verdicts, caught errors, and escaped exceptions into a single misleading
“agent succeeded” or “agent failed” judgment.

### 6.5 Cancellation and exception behavior

Inspect cancellation separately from ordinary exceptions. Async cancellation is not reliably
covered by `except Exception` alone.

The context manager must return false from exit handling so the original exception continues.
Never wrap an application exception in a PromptKeep exception.
Do not store traceback frames or local variable snapshots.

### 6.6 Streams and background tasks

Capture trace and parent keys when the model call starts. Do not read the ambient context
again when a stream finishes: the application may have exited or changed scopes.

- Reuse the existing stream proxy and finish-once machinery.
- Store the model span when the existing run-finalization path executes.
- Use current early-close and error behavior without introducing another stream wrapper.
- Do not treat an unconsumed stream as a fully observed completion.
- Callers should await child tasks and consume or close streams before leaving their agent scope.
- Exiting the root does not await arbitrary tasks, join application threads, or drain the writer.
- A late-finishing child may be recorded after the root's lexical exit.
- Trace duration must account for recorded children rather than assuming the root end is last.
- A terminal root does not prove no detached operations are still running.

No second background task registry or automatic stream buffering is part of this feature.

### 6.7 Thread and process boundaries

Context variables propagate through normal async task context inheritance.
They do not automatically propagate into arbitrary application-created threads or processes.

Support explicit scopes within each thread. Document standard context copying when needed.
Do not add propagation helpers, distributed IDs, or executor monkey-patching in Slice A.

Thread safety still matters for shared storage paths and handles. A handle should not be
mutated concurrently across application threads as a supported first-release use case.

## 7. Minimal storage design

### 7.1 One new table

Proposed table name: `spans`.

| Field | Purpose |
| --- | --- |
| `span_key` | Unique UUID minted before storage |
| `trace_key` | UUID of the root agent span |
| `parent_span_key` | Optional containing span key |
| `kind` | agent, tool, or llm |
| `name` | Explicit agent/tool name, or a neutral model-call label |
| `started_at` | UTC wall timestamp captured at start |
| `ended_at` | Optional UTC wall timestamp |
| `duration_ms` | Optional monotonic elapsed duration |
| `status` | running, ok, error, cancelled, or blocked as applicable |
| `input_json` | Optional serialized agent/tool input |
| `output_json` | Optional serialized agent/tool output |
| `error` | Optional exception text |
| `sampled` | Root sampling cohort decision, used to explain problem-only traces |

Use a normal integer primary key only if it fits existing Peewee conventions.
Public relationships use UUID keys, never row numbers.

Initial indexes:

- Unique `span_key`.
- `(trace_key, started_at)` for one trace.
- `(kind, started_at)` for root listing and cleanup.

Add another index only if a concrete first-release query requires it.

### 7.2 One new column on existing runs

Add nullable `runs.span_key`, indexed.

All run rows emitted by one physical model call receive the same span key.
The new model span contains timing and grouping; existing runs retain prompt lineage,
variables, model, usage, cost, checks, feedback, and response text.

This avoids copying LLM payloads and metrics into a second storage model.
There is no need for separate agent-run and agent-step tables.

### 7.3 Relationship tolerance

Queued records can outlive their parents or be dropped independently. Use key references
that tolerate missing rows, consistent with existing run-parent behavior.

A span must remain readable when:

- Its parent never reached disk.
- Its root start record was dropped.
- Its linked run row was dropped or pruned.
- A linked prompt run failed registration.
- A tool payload was dropped after a redaction failure.

Do not introduce strict foreign keys that turn these expected telemetry losses into failed
batches. Explain missing relationships in the read model.

### 7.4 Root lifecycle and queue items

Use the existing queue with one additional item kind for span persistence.

- On top-level agent entry, enqueue a self-contained root row with status running.
- On root exit, enqueue a complete final row as an upsert.
- Completed tool and nested-agent spans can be submitted at exit.
- Model spans are submitted through existing call finalization.
- Final root records include enough data to restore a dropped start record.
- An upsert must not downgrade a terminal root back to running.
- The writer remains provider-agnostic and does not own agent context.

Root entry/finalization must remain shielded. If an update cannot persist, application work
continues and the trace may remain incomplete.

A process killed during a tool may leave a running root without that unfinished tool.
Do not promise crash recovery or start/end events for every child in the initial release.

### 7.5 Atomicity and missing model rows

Where practical, a model span and its existing run rows should travel through the current
transaction path together. Do not rewrite the whole writer to guarantee that relationship.

If separate queue items are required, readers must tolerate one surviving without the other.
The writer's bounded queue and current loss policy remain the reliability limit.

### 7.6 Migration

At implementation time, inspect the then-current schema version.

If version 8 is still current, add version 9:

1. Create the span table.
2. Add nullable `runs.span_key`.
3. Add required indexes.
4. Stamp the new version in the same migration transaction.

Fresh files use current MODELS. Existing rows retain null span keys.
Do not backfill historical traces from conversation data; that execution was never observed.

Test the immediate predecessor migration and the existing oldest-supported migration path.
Preserve forward-only behavior and document that older packages may not read an upgraded file.

## 8. Integration with existing model calls

### 8.1 Shared call construction

In `integrations/core.py`, snapshot the active agent context when a call is prepared.

An active, unsuppressed context makes the call recordable even if:

- No Prompt object is present.
- No conversation is active.
- No pre or post checks are configured.

Allocate one model span key for that physical call.
Pass it into the common fields for every run row emitted by the call.

### 8.2 Shared finalization

Finalize the model span through the same shared paths that finalize run recording.

Cover:

- Sync non-streaming.
- Async non-streaming.
- Sync streaming.
- Async streaming.
- Provider exceptions.
- Blocked calls.
- Adapter response-reading failures.

Use the current call status and shielded persistence behavior.
Do not add provider-name branches to the shared core.
Do not infer missing output or token usage.

### 8.3 Multiple Prompts in one model call

The existing implementation can write several run rows for one response.
This needs an explicit identity rule:

- One physical API call -> one model span.
- Several prompt lineage rows -> the same span key.
- Checks and feedback remain attached to the primary run according to current behavior.
- Trace token, cost, and model-call counts count the span once.

Do not sum every linked run row. That would multiply cost and tokens when two Prompts
appear in one request.

### 8.4 Suppression and judge calls

Honor existing check suppression:

- LLM-judge calls made under `checks.suppress()` remain untracked.
- Tools and nested agents entered under suppression do not create trace data.
- Suppressed scopes still allow application code to execute normally.
- Do not introduce a second independent suppression mechanism.

Keep dependency direction explicit when using suppression from the new module.

### 8.5 Provider support

Slice A requires no provider-specific tool protocol interpretation.

A model response containing only a function call can still have null `output_text`.
The explicitly entered tool scope records the application operation that follows.

Do not label a model span's empty text as “no tool call.”
The model response text and the actual recorded tool step are different observations.

Future protocol capture belongs in adapters and their contract/SDK fixtures.
It must not be implemented by parsing provider objects in `history.py` or shared core code.

## 9. Redaction, payloads, and operational controls

### 9.1 Redaction before queueing

Treat agent input/output, tool input/output, span names, and error text as potentially
sensitive user data.

- Serialize supported values at the recording boundary.
- Apply the existing redact hook before queue submission.
- Never enqueue the original unredacted serialized payload.
- IDs, timestamps, durations, and status remain structural fields.
- A hook exception or non-string return drops the affected span payload/record according
  to one documented policy; it must never store the original value as a fallback.
- Avoid payload text in warnings about failed persistence.

Prefer the existing fail-closed redaction convention: drop a row on hook failure.
A missing span is safer than silently storing unredacted tool results.

### 9.2 Serialization contract

Use the existing JSON helper conventions where practical.

- Accept values supported by the existing serializer.
- Document any repr fallback; it is lossy and may itself contain sensitive text.
- Shield exceptions raised by custom serialization or repr.
- Explicit `None` serializes as JSON null; omitted values remain absent.
- Store serialized snapshots rather than references that can be mutated before exit.
- Preserve redacted raw text when a redact hook makes a JSON field unparsable; do not quietly
  turn a stored visible output into an unexplained empty value in the trace view.
- Do not design a general serializer registry.

Handle input at entry and output when supplied so later application mutation does not rewrite
history unintentionally.

### 9.3 Payload size and import cost

Tool results may be much larger than a model reply.

Before merging, select and document a finite payload-size policy for serialized span values.
Prefer one small internal limit, with a visible truncation marker and byte count, over several
new configuration options. Apply the same policy to agent and tool payloads.

Do not recursively walk arbitrary object graphs, download remote content, or serialize
binaries as base64 automatically.

Measure whether the added serialization materially affects the application path.
A stored-size limit does not by itself bound the cost of serializing an arbitrary custom object.
Document that limitation; do not claim constant-time instrumentation.

Keep core runtime dependencies unchanged and imports free of database I/O and thread creation.

### 9.4 Sampling policy

Reuse the existing sampling controls. Avoid buffering an entire trace for possible replay
after a late failure.

Required behavior:

1. Choose a stable successful-span cohort for the trace.
2. When the agent is entered under a conversation, align normal sampling with that
   conversation's existing cohort where it can be resolved through existing storage helpers.
3. Pass the chosen decision to model recording so normal trace steps do not independently
   flip coins.
4. Preserve existing always-keep rules for errors, blocks, and non-ok known verdicts.
5. A problem in an otherwise dropped cohort may therefore produce a partial trace.
6. Do not claim to recover earlier successful steps that were already discarded.
7. Late async checks keep the existing limitation: a verdict cannot restore missing history.
8. Expose a root sampling indicator so readers can identify a problem-only trace.

Per-call conversation overrides can move a model call into another session cohort.
Preserve that call's existing explicit-conversation behavior and mark missing trace links
honestly. Do not redefine conversation sampling merely to make this edge case look complete.

Before implementation, write a short policy test table covering these combinations.
If reusing the existing cohort requires a small optional argument to `keep_run`, prefer that
to a second sampler implementation.

### 9.5 Retention policy

Extend the existing scheduled sweep. Add no cleanup service.

- Expire span data in whole trace groups, based on latest recorded activity.
- Include root and child span timestamps, and relevant linked run activity, when determining age.
- A running root is not pinned forever; an old abandoned trace can expire.
- Delete span payloads and associated model runs/checks consistently when the trace expires.
- Preserve prompt and version rows.
- Prevent ordinary standalone-run pruning from cutting up a surviving trace unnecessarily.
- Preserve the existing conversation deletion rules.
- If conversation pruning removes a linked model run before the trace expires, keep remaining
  spans readable and show that the model record is unavailable.
- Use bounded chunks and the current transaction conventions.
- Keep `prune()`'s existing return meaning unless deliberately documented as an API change.
- Do not add automatic VACUUM or change SQLite pragmas.

A trace crossing conversation boundaries may retain partial links. That is a documented
consequence of preserving existing conversation retention rather than creating a global
lifetime manager for every record.

### 9.6 Disabled, off, and sync modes

- `enabled=False`: scopes still enter/exit and expose keys; no persistence.
- `write_mode="off"`: no span or run telemetry; existing prompt version behavior continues.
- `write_mode="sync"`: persist through the shared storage functions before they return.
- `write_mode="background"`: use the existing bounded queue.
- No active agent: wrapped model calls behave as they do today.
- Queue overflow can create partial traces. It must never block tool execution.

### 9.7 Flush behavior

`flush()` continues to wait for pending existing checks and then drain queued telemetry.

It does not:

- Wait for an open tool scope to finish.
- Await application-owned tasks.
- Close an unconsumed model stream.
- Guarantee an evicted or failed row was written.

Document `True` as “pending checks and queue drained,” not “all possible agent work succeeded.”
Do not add a second shutdown hook or another queue.

### 9.8 Configuration and process binding

A scope captures its database identity at entry. Active configuration changes must not attach
its child span records to an unrelated database.

Use the captured path as an internal guard, not a new database-routing subsystem.
If the configured database changes mid-scope, skip incompatible trace association with a
warning and allow ordinary model recording to follow current configuration.

Forked children must not reuse a parent's active scope as though it were a shared trace
lifecycle. Reuse existing fork-reset conventions or invalidate inherited scope state using
the captured process identity. Do not create cross-process propagation support.

## 10. Module and dependency plan

### 10.1 New production module

Add at most one new production module for Slice A: `src/promptkeep/agents.py`.

It owns:

- Public agent and tool context entry points.
- Private immutable context snapshots.
- UUID allocation and lifecycle timing.
- Returned scope handles.
- Small private helpers for starting/finalizing model spans.
- Delegation to existing storage functions.

It does not own Peewee queries, provider parsing, a queue, template rendering, or history reads.

### 10.2 Dependency direction

Proposed direction:

```text
models -> migrations
config -> writer
config -> controls
models + migrations + controls + writer -> storage
storage + existing suppression support -> agents
agents + tracking + provider adapter interface -> integrations/core
models + storage -> history
history -> CLI / dashboard
```

Arrows mean “is depended on by.”

Storage must not import the ambient agent module.
Capture context at scope/call boundaries and pass primitive keys and prepared data downward.

Verify the actual checks/tracking import graph before implementation.
If a cycle appears, move a genuinely small context-only helper downward; do not add
function-local package imports or a new dependency injection/event bus framework.

### 10.3 Expected file changes

| File | Expected change |
| --- | --- |
| `agents.py` | Only new production module in Slice A |
| `models.py` | Span model and nullable run span key |
| `migrations.py` | One forward migration |
| `storage.py` | Span recording/upsert, queue dispatch, retention integration |
| `controls.py` | Span redaction and minimal sampling extension |
| `integrations/core.py` | Snapshot context, recordability, model span finalization |
| `history.py` | Trace reads and result types |
| `__init__.py` | Two public entry points |
| `writer.py` | Documentation or narrowly required dispatch support only |
| `tracking.py` | Only if needed to clarify existing flush documentation |
| `tests/` | Focused behavior and migration coverage |
| `docs/API.md`, `README.md`, `CHANGELOG.md` | Public contract, small usage example, release note |
| `examples/` | One offline task with success and failure paths |
| `cli.py` | Slice B only |
| `dashboard/` | Slice C only |

Do not modify prompt normalization, version matching, provider selection, or dataset formats
to accommodate this feature.

## 11. Read API

### 11.1 Proposed functions

```python
history.list_traces(limit=100)
history.trace(trace_key)
```

Keep queries in `history.py`.

Use at most two new public history dataclasses initially:

- `SpanInfo`: persisted span fields and small derived properties.
- `TraceInfo`: one trace's spans and linked run information, with derived totals.

`list_traces()` can return root `SpanInfo` records. Do not fetch all child spans and all
payloads just to list roots.

### 11.2 Query behavior

- Root listing is newest first and bounded by a limit.
- One trace read fetches spans, linked run rows, and checks in bounded batches.
- Reuse existing run projections and batch label reads.
- Avoid one query per span.
- No SQL in the CLI or dashboard.
- Missing roots with surviving children can be loaded by trace key and shown as incomplete.
- Do not invent a successful root when it is absent.
- Preserve current missing-database and disabled-tracking read conventions.
- An unknown trace key should raise a clear lookup error, consistent with existing detail reads.
- Support missing parents without recursion errors.
- Guard traversal against malformed cycles, even though the normal writer should never create them.

### 11.3 Aggregates

For model-related metrics:

- Count each LLM span once.
- Pick one existing primary run for shared usage metrics.
- Include prompt lineage from all linked run rows.
- Keep unknown cost distinct from zero cost.
- Report known cost with an indication that some calls have unknown cost.
- Never add a price table or estimate provider cost.
- Tool API charges are unknown unless explicitly supported by a later feature.
- Trace elapsed duration is the observed wall interval, not the sum of overlapping span durations.
- Root output is the explicitly recorded root output; do not silently choose the latest model text.
- A child error count and root exit status are separate facts.

### 11.4 Replay and export

A trace read is an execution inspection format.
It is not an exact provider message replay.

Leave existing conversation replay and dataset exporters unchanged.
An optional JSON view in Slice B may serialize history results, but do not commit to a
general stable interchange format or agent-training dataset format in Slice A.

## 12. CLI and dashboard boundaries

### 12.1 Slice B CLI

Prefer one command:

```text
promptkeep trace
promptkeep trace TRACE_KEY
```

Follow current `--db` and missing-file conventions.
List mode shows key, agent name, start, duration, and root status.
Detail mode shows ordered indentation, kind, status, elapsed duration, and available payloads.

Output should identify missing roots, sampled-out data, and unavailable linked runs.
Keep the command a presentation layer.

### 12.2 Slice C dashboard

Use existing FastAPI/Jinja templates and styles.

List view:

- Agent name.
- Trace key.
- Start time.
- Duration.
- Root status.

Detail view:

- Root input and output when recorded.
- Nested spans ordered by start.
- Tool payloads in expandable sections.
- Existing model run/prompt/check links.
- Known tokens/cost and missing-cost indication.
- Missing-data annotations without claiming total completeness.

Paginate root lists. Avoid rendering unbounded lists of every trace.
Any per-trace display limit must be visible; never silently hide the last steps of a failure.

Escape all recorded payloads and errors. Search results are untrusted display content.
Use current template escaping; do not render tool output as trusted HTML.

## 13. Verification plan

This section describes future implementation verification. Creating this Markdown document
does not require running the application or the test suite.

Follow existing test fixtures, temporary databases, fakes, and real-SDK mock transport tests.

### 13.1 Scope lifecycle

- Root enter/exit success with and without output.
- Explicit null versus omitted output.
- Input/output snapshots survive later mutation.
- Nested agent and tool containment.
- Original exception identity and traceback propagation are preserved.
- Async cancellation status and propagation.
- Context restoration after success and failure.
- A tool outside an agent produces a clear usage error.
- Re-entry and closed-handle output behavior.
- Disabled/off scopes remain harmless.

### 13.2 Model recording

- Plain model call inside an agent records without a Prompt or conversation.
- A call outside an agent retains current behavior.
- Sync and async non-streaming.
- Sync and async streaming.
- Early stream closure and stream errors.
- Context captured before a stream settles.
- Provider errors and blocked calls.
- Several tracked Prompts produce one model span and several linked runs.
- Tokens and cost are not multiplied.
- Existing response objects and RunHandle behavior are preserved.
- Judge calls under suppression do not appear as trace spans.
- Adapter response-reading failures lose telemetry without breaking the call.

### 13.3 Storage and migration

- Fresh database with the new models.
- Schema 8 to next-version migration.
- Existing legacy migration fixture through the new version.
- Null span links on historical run rows.
- Root final upsert restores a missing start.
- Terminal state cannot regress to running.
- Missing root, parent, span, or linked run is readable.
- Existing registration and concurrent SQLite behavior remain healthy.

### 13.4 Queue and operational behavior

- Background mode and explicit flush.
- Root close before a late child finishes.
- Queue overflow removing start, child, or model run rows.
- Batch failure leaves the worker alive.
- Sync/off/disabled modes.
- Context isolation across concurrent async tasks.
- Documented thread-boundary behavior.
- Inherited scope invalidation after fork where supported.
- Database-path changes during an active scope.
- No extra writer thread or import-time I/O.

### 13.5 Redaction and sampling

- Agent/tool input, output, names, and errors are redacted before queue submission.
- Redaction failure never stores original text.
- Serialization and repr failure are shielded.
- Unparseable redacted JSON remains inspectable as redacted text.
- Payload truncation is visible.
- Successful trace cohort decisions are consistent.
- Conversation-cohort behavior and explicit conversation override edge cases.
- Known errors/blocks can create clearly partial problem-only traces.
- Async post-check limitations are documented rather than hidden.

### 13.6 Retention and reads

- Expired trace removes agent/tool payloads and appropriate linked records.
- Active recent trace keeps eligible standalone model rows.
- Old abandoned running root expires.
- Conversation pruning can remove model links without breaking trace reads.
- Prompt/version history remains.
- Trace listing is bounded.
- One trace read does not issue one query per span.
- Unknown and partial costs are distinguishable.
- Missing parent/cyclic data cannot hang traversal.
- CLI/dashboard tests are added only with their respective slices.

### 13.7 Repository gates

When implementation exists, run the appropriate focused tests first, then the repository's
required lint, package-contract, and coverage checks.

Follow AGENTS.md's public API, docstring, formatting, and import-DAG conventions.
Add real SDK response-object or mock-transport cases when provider-shape behavior changes.
Slice A's provider-neutral context capture should avoid unnecessary provider API changes.

No live paid calls are required for core verification.
Any later live smoke run should be explicit about endpoint, credentials, and expected cost.

## 14. Implementation sequence

### Step 1: settle the small contract

Deliverables:

- Confirm two public entry points and returned handle behavior.
- Confirm one spans table and one nullable run link.
- Write the sampling/retention edge-case policy table.
- Select the payload cap and truncation representation.
- Inspect module dependencies to confirm the proposed placement is acyclic.

Exit condition: no unresolved API or data-lifetime decision blocks implementation.
Do not create generic abstractions during this step.

### Step 2: schema and storage primitives

Deliverables:

- Span model.
- Next schema migration.
- Shielded record/upsert path.
- Existing writer item dispatch.
- Redaction and payload handling.
- Focused migration and persistence coverage.

Exit condition: a completed tool span and a root lifecycle can be written and read safely
through existing modes without an agent runtime or a second queue.

### Step 3: explicit agent and tool scopes

Deliverables:

- New `agents.py`.
- Context restoration and nesting.
- Shared handle/output behavior.
- Success/error/cancellation handling.
- Suppression and disabled behavior.

Exit condition: a tiny offline application records a root and an explicitly wrapped tool.

### Step 4: existing model-call association

Deliverables:

- Active trace becomes a recording reason.
- One model span per physical call.
- Common span key on all linked Prompt run rows.
- Shared finalization across current call paths.
- No provider-specific branches in core.

Exit condition: a trace contains model -> tool -> model in observed start order, with existing
prompt lineage and metrics available and no duplicated totals.

### Step 5: controls and retention integration

Deliverables:

- Sampling behavior integrated with the existing sampler.
- Redaction-before-queue verified for every new text field.
- Whole-trace expiry and missing-link behavior.
- Flush, overflow, fork, and database-binding behavior documented and covered.

Exit condition: the feature honors current operational controls without leaving tool payloads
outside the configured retention policy.

### Step 6: history and usable example

Deliverables:

- Two history queries and minimal read dataclasses.
- Bounded linked reads and derived totals.
- Missing-root and partial-data presentation.
- One offline example showing success, a caught tool failure, and an async case if useful.
- README/API/CHANGELOG updates and intentional public API snapshot updates.

Exit condition: Slice A is useful and reviewable through Python alone.

### Step 7: optional CLI

Implement Slice B without changing the data model.
Use this to inspect real task traces before committing to dashboard work.

### Step 8: optional dashboard

Implement Slice C only after the history result shapes have settled.
Do not add a graph library for an ordered indented span list.

## 15. Effort estimate

These are planning estimates for one engineer familiar with Python and this repository.
They include implementation, meaningful regression coverage, documentation, and review fixes.
They are not a delivery guarantee and are not based on a completed implementation prototype.

| Slice | Focused engineering effort | Deliverable |
| --- | --- | --- |
| A: small core, controls, history, docs | 7-11 days | Usable Python agent/tool trace feature |
| B: CLI inspection | +0.5-1.5 days | List/detail trace command |
| C: simple dashboard | +1.5-3 days | Local trace list and detail pages |
| Integration contingency | +1-2 days | Queue, retention, async edge cases found during review |
| A+B+C with contingency | About 10-18 days | Complete local inspection feature |

The estimates deliberately retain time for the existing library's reliability contracts.
Reducing code volume does not remove the work needed to handle background writes, multiple
Prompt rows, cancellation, redaction, and migration correctly.

Expected calendar time for A+B+C is roughly two to four focused work weeks for one engineer.
Slice A should be independently releasable before that.

Potential additions, excluded from the totals:

| Addition | Initial planning allowance | Why separate |
| --- | --- | --- |
| Provider-hosted search visibility on one Responses surface | 2-4 days | Requires validating exact SDK payloads and exposed data |
| First framework lifecycle adapter | 3-7 days | Hooks, nesting, duplicate instrumentation, async and streams |
| Direct provider SDK adapter | Re-estimate after choosing provider | Broader than tracing; current provider contract coverage applies |
| Distributed propagation or exact replay | Separate design project | Different reliability and data contracts |

Do not add all these allowances into a single default release.
Re-estimate after Slice A using actual diff size, failures, and the offline example.

## 16. Anti-bloat budget and review gates

These are review triggers, not reasons to sacrifice correct error handling or tests.

### 16.1 Structural budget

For Slice A:

- At most one new production Python module.
- One new database table.
- One nullable indexed column on existing runs.
- Two new top-level public entry points.
- Two new public history query functions.
- At most two new public history result types initially.
- No new runtime dependencies.
- No new worker thread, executor, scheduler, queue, or shutdown hook.
- No new provider-specific parsing in the shared core.
- No new public configuration/environment switches by default.

If implementation needs more, stop expanding it silently. Explain the concrete requirement,
first try an existing mechanism, then revise this plan.

### 16.2 Code size budget

Aim for roughly 400-700 net production Python lines for Slice A.
Treat growth beyond roughly 900 net production lines as a scope-review trigger.
Tests, docs, and examples are separate from that review signal.

This is a planning target, not a source-size prediction. Do not compress code, remove
docstrings, or hide complexity to meet a number.

A large diff should be explained by actual lifecycle/control cases, not new abstractions.

### 16.3 Avoid speculative hooks

Do not add:

- A span backend interface with only one implementation.
- A storage repository/service hierarchy over existing storage.
- Generic event names and event-handler registration.
- An agent-provider class hierarchy.
- Separate model telemetry copies in the span table.
- Automatic frame/source/argument inspection.
- A tool permission policy engine.
- Rich metadata schemas or arbitrary graph relationships.
- Helpers for frameworks that are not in the first release.
- Configuration flags for hypothetical future consumers.
- New dashboard infrastructure.
- Refactors of unrelated rendering, checks, datasets, or provider selection.

### 16.4 Review questions

Before merging each slice, answer:

1. Can each new type/module/field be tied to a first-release use case?
2. Are we storing model data twice?
3. Does any new code duplicate an existing call lifecycle or queue?
4. Are errors still shielded at implicit telemetry boundaries?
5. Are tool data and errors redacted before queueing?
6. Can multiple Prompts multiply metrics?
7. Are missing roots/parents/linked runs handled honestly?
8. Can existing configuration unexpectedly leave new tool data forever?
9. Does the public API have two ways to do the same operation?
10. Did any optional follow-up leak into the core release?

## 17. Acceptance checklist

### Core behavior

- [ ] A root agent context exposes stable trace and span keys.
- [ ] Tool scopes explicitly record application-executed tools.
- [ ] Omitted output is distinguishable from explicit null output.
- [ ] Original function return values and exceptions are preserved.
- [ ] Nested scopes restore context correctly.
- [ ] Cancellation propagates and is distinguishable from ordinary errors.
- [ ] Wrapped plain model calls inside a trace are recorded.
- [ ] One physical model call produces one model span.
- [ ] All Prompt run rows for that call link to that span.
- [ ] Existing check suppression remains effective.
- [ ] Stream finalization uses the captured trace context.

### Storage and controls

- [ ] Existing databases upgrade through a forward-only migration.
- [ ] Historical data is not assigned invented traces.
- [ ] Root finalization can recover a lost root start.
- [ ] Partial relationships do not fail a batch or a read.
- [ ] Redaction covers every new user-data field before queueing.
- [ ] Payload size/truncation behavior is finite and documented.
- [ ] Sampling and problem-only traces are documented honestly.
- [ ] Retention covers all new stored payloads.
- [ ] Existing disabled/off/sync/background modes work.
- [ ] Flush semantics remain bounded to checks and queued records.
- [ ] Fork and mid-scope database changes do not silently cross-contaminate traces.

### Readability and maintenance

- [ ] Python history can list roots and inspect one trace.
- [ ] Costs/tokens are counted once per model span.
- [ ] Known cost, unknown cost, and root/child outcomes stay distinguishable.
- [ ] Queries avoid one database round trip per span.
- [ ] Tool outputs are not inserted into existing conversation replay.
- [ ] Core imports stay acyclic.
- [ ] Public API snapshots and docs are updated intentionally.
- [ ] One offline example demonstrates the feature.
- [ ] Production changes remain within the agreed structural budget or have a written reason.

### Optional inspection slices

- [ ] CLI uses history reads and current missing-database conventions.
- [ ] Dashboard uses current templates and styles.
- [ ] Views escape tool input/output.
- [ ] Lists are bounded and partial data is visible.
- [ ] No additional frontend/runtime dependency is introduced.

## 18. Release and follow-up decision

Release Slice A when its acceptance criteria and required repository checks pass.
Treat it as a minor feature release under the current pre-1.0 policy.
Choose the actual version from repository state at implementation time.

Before adding integrations, use the example and a small real application to identify:

- Whether explicit tool scopes provide the expected visibility.
- Whether consumers need a decorator for ergonomics.
- Whether provider-hosted tools are the actual missing use case.
- Which framework users truly need.
- Whether the simple history model is sufficient.
- Whether the payload and sampling policies are understandable.

Add a follow-up only when a demonstrated use case exceeds the small core contract.
