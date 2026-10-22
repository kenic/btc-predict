# Phase 5c — preregistered Claude hourly RV side observation

## Manifest

- Version: `claude-side-v1`; preparation base: `75bd4990d90a2751d8b9841a535a9e9679f905bc`.
- Start: next UTC hour after explicit `claude_observation.py init`.
- End: **2026-11-02 00:00:00 UTC, exclusive**, confirmed by the operator.
  Last possible target is 2026-11-01 23:00 UTC. Requests and response eligibility
  must also precede this boundary. Fixed calendar window; no optional stopping,
  target replacement, performance-based suspension or extension after failures.
- Population: every hourly target, independent of ACTIVE classification or
  completion of Phase 6 / 6b. Failures and missed slots remain in the denominator.
- Target: UTC Unix start of predicted 1h candle, matching 5b `target_candle_time`
  and Phase 6 / 6b `target`.
- Actual RV: unchanged `volatility.realized_volatility`,
  `100*sqrt(sum(12 squared consecutive 5m close-to-close log returns))`;
  13 closes including target-300, percent, nonannualized. Evaluation after
  target+3660 uses existing `candles.db` read-only. Missing actuals remain NULL.
- Inside the 5b registered window, wait for and reuse **exactly its saved context**,
  previous RV and snapshot hash, even when its provider call fails. No rebuilding
  a missing overlapping input. The minute poll waits until the 600s deadline;
  unavailable inputs then become missed observations. Outside the 5b window only,
  use the unchanged `phase4_runner.build_snapshot` for continued all-hour coverage.
- Input instruction is the exact literal from `next_stages.regression_call`;
  no added system prompt, tools, news, external facts or outcome data.
- Claude: **`claude-sonnet-4-6`**, `temperature=1.0`, `max_tokens=1024`,
  `thinking={"type":"disabled"}`; top_p/top_k omitted; one user message;
  API version `2023-06-01`; endpoint `https://api.anthropic.com/v1/messages`.
  No retries or redirects. Timeout 120 seconds, capped by remaining live window.
  This model was chosen to allow explicit temperature and disabled thinking.
  Official model reference: https://platform.claude.com/docs/en/models/sonnet-4-6/overview
  API reference: https://platform.claude.com/docs/en/api/messages/create
- GPT sampling/reasoning defaults remain untouched. Exact semantic inputs and
  instructions are matched; this does not claim identical provider internals,
  compute budgets, sampling distributions or permanently immutable weights.
- Init saves actual start/end, prompt text, parameters, Python/package versions,
  source hashes, paths and Git commit in `claude_config`; each cycle verifies them.
  Model changes or source drift fail closed. There is no automatic model fallback.
  Returned model, raw response and usage are preserved per response.
- Actual API availability/credits have **not** been probed; no real API calls in
  preparation. API key is read only from `ANTHROPIC_API_KEY`, never config/DB/git.
  HTTP exception bodies/headers are not logged or stored. No existing `.env` is read.

## Isolation and schema

New file `claude-observation.db` contains only four new tables. Existing `btc.db`
and `candles.db` are read-only connections. No original file, gate, predictor set,
ensemble, stopping rule or frozen manifest is edited. No shared experiment lock
is acquired; no network call holds a DB transaction. Provider calls run in the
new service only; failure cannot propagate into original workers. CPU/network
capacity is still shared on the host.

| Table | Key | Contents |
|---|---|---|
| `claude_config` | singleton=1 | start/end, frozen JSON/hash, code SHA, source/candle paths |
| `phase5c_predictions` | target | status, timestamps, exact context/hash/origin, previous/predicted/actual RV, returned model/raw response, safe error type, API attempt timestamp |
| `phase6c_events` | cohort,target | exact accepted context/hash, source gate JSON, original realized outcome/return and availability timestamp |
| `phase6c_calls` | cohort,target,slot | status/timestamps, UP/DOWN, p_up/p_down, returned model/raw response, safe error type, correct/Brier/signed PnL |

`waiting` means awaiting 5b input. `started` reserves the only attempt atomically;
crashes leave uncertain attempts that are never retried. `complete` is valid and
on time; `late` is retained but excluded; `failed` is terminal; `missed` means no
live opportunity and no hindsight request. Concurrent calls cannot claim a key
twice. A network crash may have incurred a provider charge: `api_started_at`
counts dispatch reservations, not independently verified billing transactions.
After the end, cycles only record remaining missed slots and recover actuals;
no additional prediction targets or provider calls.

## Read-only exploratory analysis

```sh
.venv/bin/python claude_analysis.py --db claude-observation.db --source-db btc.db > /tmp/claude-comparison.json
```

JSON includes paired GPT/Claude RV N, MAE, bias (prediction minus actual),
correlation with actual RV and correlation between predictions. The timestamp JOIN
also requires equal input context/hash, complete predictions and equal actual RV.
Constant series and N<2 yield null correlation. Coverage includes failures/missed
observations; scores use only available paired outcomes. Analyses are exploratory
and may not be used to stop or change these experiments. See PHASE6C.md for direction.

## Deployment and start procedure — NOT executed during implementation

1. Review the additive commit/bundle on an isolated checkout. Confirm production
   is still the reviewed base or an audited descendant, with no tracked edits.
   Inspect the current frozen hashes and original timers. Do not reset/reinitialize
   Phase 5b, 6 or 6b. Back up live SQLite using its backup API, including existing
   side DB if resuming. Do not copy just the main file of a live WAL database.
2. Transfer the reviewed bundle to the server manually. After review, e.g.:

   ```sh
   cd /opt/btc-predict
   git status --short --branch
   git fetch /tmp/claude-side-observation.bundle phase5c-phase6c
   git diff --stat HEAD..FETCH_HEAD
   git merge --ff-only FETCH_HEAD
   .venv/bin/python -m unittest discover -q
   ```

   If the branch has diverged, review an additive cherry-pick in isolation; never
   force/reset. All existing tracked files must remain byte-identical. Existing
   Python dependencies suffice; no SDK installation or requirements change.
3. Confirm `User=ubuntu` matches the existing deployment. Create a root-owned
   mode-0600 `/etc/btc-predict/claude.env` outside the repository using a secure
   editor, containing `ANTHROPIC_API_KEY=<actual key>`. Never paste the key into
   shell history, commits, manifests or this chat. systemd reads the file and
   injects the environment; initialization itself needs no key or network call.
4. Register once after the final code is in place, then enable only the new timer:

   ```sh
   cd /opt/btc-predict
   sudo -u ubuntu .venv/bin/python claude_observation.py init
   sudo install -m 0644 deploy/systemd/btc-claude-observation.service /etc/systemd/system/
   sudo install -m 0644 deploy/systemd/btc-claude-observation.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now btc-claude-observation.timer
   .venv/bin/python claude_observation.py status
   journalctl -u btc-claude-observation.service --no-pager -n 50
   ```

   The timer polls saved input/gates each minute at :15 UTC. It has no dependency
   on, start/stop action for, or restart of any existing experiment or web service.
   `Persistent=false` forbids catch-up requests. Init is idempotent, cannot move
   the start/end or replace a frozen config. Do not run manual cycles for backfill.
   Keep the timer after expiry until the last actuals resolve; it makes no new
   prediction calls after expiry. Any unavoidable outage remains a missing slot.
   Operational shutdown must be recorded, never chosen by interim performance.
5. Verify side rows, attempt counts, missing statuses and frozen hashes; run the
   read-only report. Do not rewrite failed/started records to trigger retries.
   Preserve final DB/config and deployment commit. No key is required to analyze.

Planned maximum calls: `H + 11*A`, where H is registered hourly slots and A is
eligible accepted Phase 6 / 6b targets during that window. Actual calls may be
lower due to missing input, deadline, outage or failures. Main direction is one
request, repeats are **ten additional** requests. No token/cost estimate is claimed.

## Read-only dashboard follow-up

`/phase5c/`, `/phase5c/data.json` and `/analyze/?phase=phase5c` show the
registered calendar window, progress including failures/missed targets, latest
100 predictions, and paired GPT/Claude RV MAE, bias and correlations. Main
navigation and analysis filters include Phase 5c and Phase 6c.

`claude_views.py` uses read-only SQLite connections and public fields only.
It never initializes experiments, calls providers or displays prompts/raw
responses. Missing databases do not get created. Missing or busy source data
leaves progress visible while comparisons are marked unavailable.

This UI follow-up modifies only app.py/ui.py and documents, and adds
claude_views.py/test_claude_views.py. None is in the frozen predictor source
manifest. No observation reinitialization or timer restart is needed. Deploy
the reviewed UI commit and restart only btc-web.service to load its routes.
