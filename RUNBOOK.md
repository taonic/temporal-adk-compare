# Runbook

For whoever presents the demo. About 24 minutes for all four scenarios and the wrap-up. For a short slot, run Scenarios 1 and 3 (about 10 minutes): who restarts the work, and what a crash after a side effect costs.

## Before the meeting

1. `make clean && make demo`. The browser opens at <http://localhost:8400>.
2. Both columns show host or Worker A with a green dot.
3. Rehearse Scenarios 1 and 2 once. Each scenario starts with a reset, so a rehearsal leaves nothing behind.
4. Open the Temporal UI (<http://localhost:8233>) in a second tab. Some audiences want to see a real Workflow history.
5. Make the browser zoom fit both columns side by side with the player docked on the right (1440px wide or more). The player docks at 1200px and wider; narrower, it sits above the columns and shows only the current step.
6. If you will use Gemini instead of the fake model, rehearse with it. A real model may word its tool calls differently, and the captions assume the fake model's steps.

## Running a scenario

- Each scenario opens with its claim, one line per runtime, typed into the dialog with the phrase that matters highlighted: read the two out as the scenario's thesis before pressing **Start**. Once the scenario starts, they stay pinned under the scenario tabs as one line per runtime (the highlighted phrase), with the playing side's line at full strength; click the strip for the full claims and their sources. Scenario 1's claims are the key message: ADK resumes an interrupted run only when something sends an explicit resume request with the original invocation ID; Temporal's durable execution picks the Workflow up on any available Worker, with no resume request from your code. Each links to that sentence in the vendor's own docs.
- The player is the pane on the right, a guide's conversation: each step's message types itself out, then **Next →** (and **← Back**, **▶ Auto**) appear right under it. Press **→** or click the message to finish the typing; press **→** again to move on. Earlier steps fold into the *N earlier* chip at the top; click one to show its caption again (nothing is undone). Scroll the page freely; the pane stays put.
- One column plays at a time. Each beat plays on the ADK column first, then the same refund on the Temporal column. The step title is tagged **ADK** or **TEMPORAL**, and the other column dims until its turn (hover it to read it at full strength).
- **→** or **Next** advances a step and runs that step's actions. **←** only moves the caption back; it undoes nothing.
- A yellow *Waiting for …* line means the step's outcome hasn't arrived yet. Talk through it: **Next** (and **→**) stay disabled until it arrives. If a run has already finished on that side (you resumed it by hand, say), the wait counts as done. If the outcome still hasn't arrived after 45 seconds, a quiet **Skip waiting →** appears; or click the scenario's tab to start it over.
- **Autoplay** advances 6 seconds after each step's outcome arrives. Use it for a booth or a recording, not a conversation.
- Click any node in either diagram: **Code** shows the source behind it, and **What's persisted** shows what this run recorded for it.
- Each column's **Process log** is collapsed; its summary line shows the latest output. Click it to open the full log.
- The header holds only a GitHub link (and, during an outage, the *Model outage* chip). The player drives everything else; the Temporal UI is linked from each Temporal column's event history.
- A **⚡ crash point** marker on a column's flow shows where its process will die; the scenario arms it. Hover it for the exact place.

### Scenario 1 · Recovery: automatic vs on request (3 min)

| Step | Point at | Say |
|---|---|---|
| Set the stage | The two stack lines under the column titles; ADK hosts A and B and Worker A running, auto-restart **off** | Same `agent.py` on both sides. ADK is configured properly: resumability on, a persistent session database. ADK's spare is ready; Temporal's starts once Worker A is gone. |
| ADK: host A dies | Host A *crashed*, host B running; chip *unfinished · nobody working on it* | Click ADK's *Who resumes?* panel to show `recovery.py`. |
| Run the recovery scan | Next waits: **you** click *Run recovery scan + resume* in the ADK column (it pulses). ADK completes, still one refund | ADK resumability works. The question is who owns triggering it: here, you did. The resumes in Scenarios 2 and 3 work the same way. |
| Temporal: Worker A dies | *by Worker A* on `refund_payment` and `record_refund`; no Worker running | The Workflow waits on the Temporal Server. |
| Start Worker B | Temporal completes within about 5s; `notify_customer` reads *by Worker B*, the earlier steps still *by Worker A* | Nobody called anything: the next Worker on the task queue took it. |

The `StaleSessionError` mentioned in the caption really happened while building this demo: a test resumed an invocation that was still running. It's a good example of why "find unfinished invocations" is harder than it looks.

### Scenario 2 · Retries: durable vs in memory (4 min, including two outages)

| Step | Point at | Say |
|---|---|---|
| Same retry schedule | Click ADK's *LLM decides*: `MODEL_RETRY` in the code view | Both sides retry the model on the same schedule. ADK's is configured exactly as you would configure `Gemini`. |
| ADK: the model is down | Header chip *Model outage*. ADK *LLM decides*: *in-process retry 2 of 10* | ADK's count comes from the host's log: nothing is written to the session database. |
| Kill ADK host A | ADK *model call never finished · invocation stopped*, its *Who resumes?* panel red | The retry loop was memory in a dead process. The step waits for the outage to end too. |
| Resume ADK | ADK completes, one refund | In-process retries are only as durable as the process. |
| Temporal: the model is down | A new 30s outage. Temporal *LLM decides*: *attempt 2, retrying* | The same as ADK so far. |
| Kill Worker A | Temporal *completed*. In **What's persisted** for *LLM decides*: `ActivityTaskStarted` with `attempt` 3 or more | The next attempt was a record on the Temporal Server, and the attempt count carried on across the restart. |

Don't linger on the Temporal outage step: its outage lasts 30s, and once it ends there is nothing left to retry. If the kill lands while Worker A is mid-attempt, Temporal first waits out the model Activity's 10s start-to-close timeout, so the last step can take up to 20s.

### Scenario 3 · Tool results: recorded on completion vs saved later (7 min)

| Step | Point at | Say |
|---|---|---|
| A crash right after the side effect | ⚡ *crash point* beside both `refund_payment` nodes, which are dashed orange | No idempotency key yet. |
| ADK: host A dies after the refund | `refund_payment`: *money moved, result NOT recorded*; chip *unfinished · nobody working on it* | Host A came back idle, as in Scenario 1. |
| Resume ADK | Ledger: **ADK refunded $1,000.00** | Quote the ADK docstring in the caption. This is documented behaviour, not a bug. |
| Temporal: Worker A dies after the refund | `refund_payment`: *recorded*; Temporal *completed*, one refund | Click the Temporal node's **What's persisted** to show `ActivityTaskCompleted`. The Worker found its work on the task queue. |
| ADK: crash inside the tool | ADK *money moved, result NOT recorded* again | Now neither runtime can know the money moved. |
| Resume ADK | ADK **$ moved ×2** | |
| Temporal: same crash | Temporal `refund_payment` shows **attempt 2** after about 6s, then **$ moved ×2** | Be the first to say it: Temporal isn't exactly-once either. No runtime makes an external side effect exactly-once. |
| ADK: key on, crash again | The step turns the key on; the ledger's *idempotency key* column fills in | The function call id is stable on both runtimes. |
| Resume ADK | ADK **deduped ×1**; ledger row *duplicate request: original returned* | |
| Temporal: same crash | Temporal **deduped ×1** | Recap the three differences in the caption. |

### Scenario 4 · Timers: durable vs in memory (5 min, including two 20s waits)

| Step | Point at | Say |
|---|---|---|
| A policy with a deadline | | Both pause with ADK's own tool confirmation, and both enforce the deadline: ADK with an `asyncio` timer in the host, Temporal with a durable timer. |
| ADK: waits for a manager | *manager approval* amber; countdown; the approval panel says where the timer lives | ADK's process log: *timer held in this process's memory*. |
| Stop ADK host A | Host A *stopped*; the countdown passes zero | The paused run survives in the session DB. Its timer doesn't. |
| Bring host A back | *Deadline passed Ns ago. Nothing woke this invocation.* | The restarted host doesn't know a deadline exists. |
| Late approval | ADK refunds | Resumable, yes, but the deadline passed and nothing denied it: a refund the policy should have refused went through. Resuming late is not acting on time. |
| Temporal: waits for a manager | `TimerStarted` in the event history | Equal to ADK so far. |
| Stop Worker A | No Worker running; `TimerFired` appears in the history when the countdown ends | The timer fired on the Temporal Server with no Worker running. |
| Bring Worker A back | *escalated and denied*, a row in *Escalations* | A timeout argument and an exception handler. Click the pinned claims to open them in full during questions. |

### Wrap-up (3 min)

Content only: the runtime columns and the ledger are hidden.

| Slide | Say |
|---|---|
| Durable execution, in four results | One row per scenario: automated process recovery, durable retries, strongly consistent tool execution, durable timers. Point at the note before anyone asks: a crash *inside* a tool needs an idempotency key on either runtime. |
| What each runtime gives you | The full comparison, with where the demo showed each row. Human-in-the-loop is native on both. |
| Use them together | Temporal owns the business process; ADK owns the agent's reasoning. Same `agent.py`. |
| Questions to ask any agent runtime | Leave it on screen during questions. |

## Resetting

- Each scenario's first step resets everything: ledger cleared, crash points disarmed, model outage ended, only host A and Worker A running, auto-restart on, idempotency key off.
- Clicking a scenario's tab (the current one too) does the same at any time.
- `make clean` wipes all state, including the dev server's Workflow histories. Restart `make demo` after it.

## If something goes wrong

| Symptom | Fix |
|---|---|
| A step waits forever | Open both process logs. Click the scenario's tab to reset the stage and start it again. |
| `address already in use` on start | Another process is using port 8400, 8411, 8412 or 8420. Free the port, or set `API_PORT` and the other port variables from the README. |
| The Temporal column says it can't reach the server | Another Temporal dev server may be holding port 7233 with a different namespace. Stop it, or point `TEMPORAL_ADDRESS` at it. |
| A process shows *crashed* and won't come back | Auto-restart is off (Scenario 1 turns it off). Press its **start** button. |
| The UI shows stale data | Reload the page. The API holds no refund state; everything is re-read on each poll. |
| The header still says *Model outage* | Click the chip to end the outage, or click the scenario's tab to reset. |

## Questions you will get

**"You crippled ADK. Real deployments use Agent Engine, Agent Executor or a database service."**
The ADK side uses ADK's own resumability and a persistent session service, exactly as documented. Scenario 3's gap is about *when* the result is written relative to the side effect; a different session store doesn't change the order. Scenarios 1, 2 and 4 are about who triggers recovery, where retry state lives, and who acts on deadlines. A managed runtime may provide that, which is exactly the question to ask its vendor (README, "Questions worth asking").

**"Temporal also refunded twice."**
Yes, in the crash-inside-the-tool case, and the demo shows it on purpose. Durable execution records a completed Activity exactly once and never re-runs it; it executes an Activity at least once. Side effects need idempotency on any runtime.

**"Why not just put the whole ADK agent in one Activity?"**
Then a crash re-runs the agent from the start, including the model's decision and every tool call. The plugin's one-Activity-per-call design limits a crash to the call in flight. It also means each model answer is paid for once.

**"Isn't the fake model cheating?"**
It removes nondeterminism from the stage, nothing else. Model calls still go through ADK and, on Temporal, through `TemporalModel` Activities. With `REFUND_DEMO_MODEL=gemini-2.5-flash` the same code calls Gemini.

**"What about ADK graph workflows and their retry policies?"**
They add retries and timeouts per node in the agent graph. They don't change Scenario 3, where the problem is a side effect that happened but was not recorded, or Scenarios 1 and 4, where the problem is who triggers recovery and deadlines. For node retries, ask the Scenario 2 question: where does the retry state live when the process dies?

**"How does Temporal know the Worker died?"**
Timeouts. A Workflow task the Worker never completes times out (4s here), and so does an Activity attempt (6s here). The work then goes to any Worker polling the queue. In the history, the first shows as `WorkflowTaskTimedOut`. A retried Activity shows as `ActivityTaskStarted` with `attempt: 2`, because Temporal records an Activity's attempts in that event rather than one event per attempt.

**"So add a lease to the ADK recovery code."**
Yes, and that's the point: a lease with an expiry, a way to tell a dead host from a slow one, and fencing so a host that lost its lease stops writing. That's a distributed-systems component to build and operate. A Temporal task queue already gives each task to one Worker at a time.

**"Gemini has retries built in."**
Yes, and Scenario 2 turns them on: `Gemini(retry_options=HttpRetryOptions(...))`, off by default, retries inside the process. They work until the process dies; then the retry state is gone and the invocation waits for your recovery code. An outage longer than the retry budget ends the invocation the same way.

**"What happens when the agent's code changes mid-run?"**
Temporal replays history against the new code, so changes to Workflow-side logic need Worker Versioning or patching. That includes the agent loop, prompts and tool lists here. ADK resumes against whatever code the resuming host runs, with no built-in check. The demo doesn't cover this; it's question 5 in the README.
