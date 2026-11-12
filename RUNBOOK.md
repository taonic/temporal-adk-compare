# Runbook

For whoever presents the demo. About 21 minutes for all four acts and the wrap-up. For a short slot, run Acts 1 and 4 (about 10 minutes): who restarts the work, and what a crash after a side effect costs.

## Before the meeting

1. `make clean && make demo`. The browser opens at <http://localhost:8400>.
2. Check the header: **Idempotency key: off**, **Auto-restart: on**. Both columns show host or Worker A with a green dot.
3. Rehearse Acts 1 and 2 once. Each act starts with a reset, so a rehearsal leaves nothing behind.
4. Open the Temporal UI (<http://localhost:8233>) in a second tab. Some audiences want to see a real Workflow history.
5. Make the browser zoom fit both columns side by side (about 1440px wide). Under 1100px the columns stack.
6. If you will use Gemini instead of the fake model, rehearse with it. A real model may word its tool calls differently, and the captions assume the fake model's steps.

## Running an act

- Each act has two cards under the act tabs, ADK and Temporal: read them out as the act's thesis before pressing **Start**. Act 1's cards are the key message: ADK resumes an interrupted run only when something sends an explicit resume request with the original invocation ID; Temporal's durable execution picks the Workflow up on any available Worker, with no resume request from your code. Each links to that sentence in the vendor's own docs.
- **→** or **Next** advances a step and runs that step's actions. **←** only moves the caption back; it undoes nothing.
- A yellow *Waiting for …* line means the step's outcome hasn't arrived yet. Talk through it. Nothing breaks if you press Next early, but the picture won't match the caption.
- **Autoplay** advances 6 seconds after each step's outcome arrives. Use it for a booth or a recording, not a conversation.
- Click any node in either diagram: **Code** shows the source behind it, and **What's persisted** shows what this run recorded for it.
- Each column's **Process log** is collapsed; its summary line shows the latest output. Click it to open the full log.
- **Manual ▾** in the header starts a run by hand, starts a model outage, resets, and links to the Temporal UI. The player does all of this for you in the acts.
- **⚡ Crash** in each column arms a crash point by hand.

### Act 1 · Recovery: automatic vs on request (3 min)

| Step | Point at | Say |
|---|---|---|
| Set the stage | The two stack lines under the column titles; four processes running, auto-restart **off** | Same `agent.py` on both sides. ADK is configured properly: resumability on, a persistent session database. A spare is ready on each side. |
| Refund $500 | Temporal completes within about 5s | |
| Who picked it up? | Temporal's *Who resumes?* panel names the other Worker; ADK host B is running, invocation *unfinished* | Click ADK's *Who resumes?* panel to show `recovery.py`. |
| Run the recovery scan | ADK completes, still one refund | ADK resumability works. The question is who owns triggering it. |

The `StaleSessionError` mentioned in the caption really happened while building this demo: a test resumed an invocation that was still running. It's a good example of why "find unfinished invocations" is harder than it looks.

### Act 2 · Retries: durable vs in memory (4 min, including the outage)

| Step | Point at | Say |
|---|---|---|
| Same retry schedule | Click ADK's *LLM decides*: `MODEL_RETRY` in the code view | Both sides retry the model on the same schedule. ADK's is configured exactly as you would configure `Gemini`. |
| The model is down | Header chip *Model outage*. ADK *LLM decides*: *in-process retry 2 of 10*. Temporal: *attempt 2, retrying* | The same so far. ADK's count comes from the host's log: nothing is written to the session database. |
| Kill both mid-retry | ADK *model call never finished · invocation stopped*, its *Who resumes?* panel red. Temporal still *attempt N, retrying* | ADK's retry loop was memory in a dead process. Temporal's next attempt is a record on the Temporal Service. |
| The model is back | Temporal *completed*. In **What's persisted** for *LLM decides*: `ActivityTaskStarted` with `attempt` 3 or more | The attempt count carried on across the restart. |
| Resume ADK | ADK completes, one refund | In-process retries are only as durable as the process. |

Don't spend more than about 20s on the second step: its outage lasts 30s, and once it ends there is nothing left to retry. If the kill lands while Worker A is mid-attempt, Temporal first waits out the model Activity's 10s start-to-close timeout, so *The model is back* can take up to 20s.

### Act 3 · Timers: durable vs in memory (4 min, including a 20s wait)

| Step | Point at | Say |
|---|---|---|
| A policy with a deadline | | Both pause with ADK's own tool confirmation, and both enforce the deadline: ADK with an `asyncio` timer in the host, Temporal with a durable timer. |
| Refund $500 | Both *manager approval* nodes are amber; countdown; each approval panel says where its timer lives | They are equal so far. ADK's process log: *timer held in this process's memory*. |
| Stop every process | Both process chips *stopped* | Both paused runs survive. ADK's timer doesn't: it was memory in host A. Wait for the countdown to pass zero. |
| Bring processes back | Temporal: *escalated and denied*, a row in *Escalations*; ADK: *Deadline passed Ns ago. Nothing woke this invocation.* | The timer fired on the Temporal Service with no Worker running. |
| Late approval | ADK refunds | Resumable, yes. Acting on time needs infrastructure. Leave the act's two cards on screen during questions. |

### Act 4 · Tool results: recorded on completion vs saved later (6 min)

| Step | Point at | Say |
|---|---|---|
| A crash right after the side effect | ⚡ *after return* on both `refund_payment` nodes | No idempotency key yet. |
| Refund $500 | `refund_payment` on both sides | Both processes died in the same instant. Left: *money moved, result NOT recorded*. Right: *recorded*. Click the Temporal node's **What's persisted** to show `ActivityTaskCompleted`. |
| Both came back | ADK chip *unfinished · nobody working on it*; Temporal *completed* | As in Act 1: the Worker found its work on the task queue. The ADK host didn't. |
| Resume ADK | Ledger: **ADK refunded $1,000.00** | Quote the ADK docstring in the caption. This is documented behaviour, not a bug. |
| So is Temporal exactly-once? | Temporal `refund_payment` shows **attempt 2** after about 6s | Be the first to say it: no. |
| Resume ADK | Both show **$ moved ×2** | Exactly-once execution of an external side effect is not something any runtime gives you. |
| Key on, crash again | Both show **deduped ×1** | The function call id is stable on both runtimes. |
| Resume ADK | Ledger rows *duplicate request: original returned* | Recap the three differences in the caption. |

### Wrap-up (3 min)

Content only: the runtime columns and the ledger are hidden.

| Slide | Say |
|---|---|
| Durable execution, in four results | One row per act: automated process recovery, durable retries, durable timers, strongly consistent tool execution. Point at the note before anyone asks: a crash *inside* a tool needs an idempotency key on either runtime. |
| What each runtime gives you | The full comparison, with where the demo showed each row. Human-in-the-loop is native on both. |
| Use them together | Temporal owns the business process; ADK owns the agent's reasoning. Same `agent.py`. |
| Questions to ask any agent runtime | Leave it on screen during questions. |

## Resetting

- Each act's first step resets everything: ledger cleared, crash points disarmed, model outage ended, only host A and Worker A running, auto-restart on, idempotency key off.
- **Manual ▾ → Reset everything** does the same at any time.
- `make clean` wipes all state, including the dev server's Workflow histories. Restart `make demo` after it.

## If something goes wrong

| Symptom | Fix |
|---|---|
| A step waits forever | Open both process logs. Use **Manual ▾ → Reset everything** and restart the act. |
| `address already in use` on start | Another process is using port 8400, 8411, 8412 or 8420. Free the port, or set `API_PORT` and the other port variables from the README. |
| The Temporal column says it can't reach the server | Another Temporal dev server may be holding port 7233 with a different namespace. Stop it, or point `TEMPORAL_ADDRESS` at it. |
| A process shows *crashed* and won't come back | Auto-restart is off (Act 1 turns it off). Press its **start** button. |
| The UI shows stale data | Reload the page. The API holds no refund state; everything is re-read on each poll. |
| The header still says *Model outage* | Click the chip to end the outage, or use **Manual ▾ → Reset everything**. |

## Questions you will get

**"You crippled ADK. Real deployments use Agent Engine, Agent Executor or a database service."**
The ADK side uses ADK's own resumability and a persistent session service, exactly as documented. Act 4's gap is about *when* the result is written relative to the side effect; a different session store doesn't change the order. Acts 1, 2 and 3 are about who triggers recovery, where retry state lives, and who acts on deadlines. A managed runtime may provide that, which is exactly the question to ask its vendor (README, "Questions worth asking").

**"Temporal also refunded twice."**
Yes, in the crash-inside-the-tool case, and the demo shows it on purpose. Durable execution records a completed Activity exactly once and never re-runs it; it executes an Activity at least once. Side effects need idempotency on any runtime.

**"Why not just put the whole ADK agent in one Activity?"**
Then a crash re-runs the agent from the start, including the model's decision and every tool call. The plugin's one-Activity-per-call design limits a crash to the call in flight. It also means each model answer is paid for once.

**"Isn't the fake model cheating?"**
It removes nondeterminism from the stage, nothing else. Model calls still go through ADK and, on Temporal, through `TemporalModel` Activities. With `REFUND_DEMO_MODEL=gemini-2.5-flash` the same code calls Gemini.

**"What about ADK graph workflows and their retry policies?"**
They add retries and timeouts per node in the agent graph. They don't change Act 4, where the problem is a side effect that happened but was not recorded, or Acts 1 and 3, where the problem is who triggers recovery and deadlines. For node retries, ask the Act 2 question: where does the retry state live when the process dies?

**"How does Temporal know the Worker died?"**
Timeouts. A Workflow task the Worker never completes times out (4s here), and so does an Activity attempt (6s here). The work then goes to any Worker polling the queue. In the history, the first shows as `WorkflowTaskTimedOut`. A retried Activity shows as `ActivityTaskStarted` with `attempt: 2`, because Temporal records an Activity's attempts in that event rather than one event per attempt.

**"So add a lease to the ADK recovery code."**
Yes, and that's the point: a lease with an expiry, a way to tell a dead host from a slow one, and fencing so a host that lost its lease stops writing. That's a distributed-systems component to build and operate. A Temporal task queue already gives each task to one Worker at a time.

**"Gemini has retries built in."**
Yes, and Act 2 turns them on: `Gemini(retry_options=HttpRetryOptions(...))`, off by default, retries inside the process. They work until the process dies; then the retry state is gone and the invocation waits for your recovery code. An outage longer than the retry budget ends the invocation the same way.

**"What happens when the agent's code changes mid-run?"**
Temporal replays history against the new code, so changes to Workflow-side logic need Worker Versioning or patching. That includes the agent loop, prompts and tool lists here. ADK resumes against whatever code the resuming host runs, with no built-in check. The demo doesn't cover this; it's question 5 in the README.
