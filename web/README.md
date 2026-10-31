# Workbench checks

The webpage reattaches to a supervisor-owned Codex PTY; stage selections update the video and recorded context, and can scroll the existing terminal to a unique text match. They never replace the PTY or send input. Task and native request input use a gateway writer lease. Closing a page releases its lease when reachable and never stops the run. A new page begins in monitoring mode and must claim input explicitly.

Build and check the pinned dependencies:

```sh
npm --prefix web ci
npm --prefix web run check
npm --prefix web run build
mamba run -n Auriga python -m unittest discover -s tests -p test_terminal.py -v
```

The synthetic browser fixture starts no model or GPU and marks every record `synthetic_fixture`:

```sh
mamba run -n Auriga python web/fixture-server.py --port 8778
```

In another shell, run the browser check. Use an existing Playwright installation or install it outside the runtime dependencies. Use an available Playwright browser. A system Chromium/Chrome also works with `AURIGA_BROWSER=chromium AURIGA_BROWSER_EXECUTABLE=/path/to/google-chrome` (for example `/usr/bin/google-chrome`).

```sh
AURIGA_URL=http://127.0.0.1:8778 \
AURIGA_SYNTHETIC=1 \
AURIGA_BROWSER=firefox \
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_SCREENSHOT_DIR=runs/web-synthetic-check \
node web/check-browser.mjs
```

Start a fresh fixture process for each full synthetic check: native requests are deliberately resolved once and cannot be re-opened by browser retries. The check covers separate motion/turn states, two-window ownership (including a repeated public client ID without its lease token), native approvals/questions, task idempotency, trace identities and missing feedback, missing media, keyboard navigation, event deduplication/gaps, HTTP/WS origins, and PTY identity across refresh, resize and close/reopen. It distinguishes observations delivered to a program from images delivered to the Astra model. It saves screenshots at 320, 736, 1024 and 1440 pixels plus a JSON report.

For an already running real service, omit `AURIGA_SYNTHETIC`, set its URL and use `AURIGA_REQUIRE_ACTION=1` to require a completed real action. This mode also reads that action's input, execution-start and result RoboDojo observations, checks their run and result action identities, and requires readable planned, dispatched and actual trace artifacts. The report preserves these observation IDs, step domains and trace refs under `action_evidence`. When an immutable version contains a tracked proxy, the runtime check queries its conditional geometry and creates a geometry snapshot with a verified artifact closure, recorded under `scene_evidence`. It does not submit a new task or robot action. Synthetic results establish UI/protocol behavior, while real model/robot behavior requires a separate runtime report.

Scene operations use explicit immutable versions and evidence observation IDs. Geometry build and alignment are internal jobs. A geometry snapshot does not establish a physical initial state; the UI reports physics as unavailable until an independent worker is implemented and validated.

The primary review is a phase playlist: each native tool call groups explicitly correlated Harness actions, the current turn prompt, and the recorded messages/reasoning immediately before and after that call. Multiple actions within one tool remain individual clips. Nonmoving and unmapped stages retain their text but are skipped by continuous playback. Raw observations, events and recordings remain in folded evidence sections, preserving each camera's run, episode, generation, sampling description and complete `frame_mapping`. Selecting action evidence enables start/end seek only when that recording has exact step-to-PTS boundary records and the physical identity matches. A missing declared step domain requires an exact terminal-observation frame anchor; otherwise the UI reports the missing mapping. Encoded fps is displayed separately and is never used to calculate playback PTS. Browser codec failures retain a downloadable original MP4; compatible derivatives retain their source artifact reference.

The recorded-video acceptance check uses only GET/HEAD requests against an existing replay service. It decodes all three camera videos, seeks through the action buttons to recorded start/end PTS, saves native canvas frames, verifies MP4 HEAD/range responses and rejects mapping across generations:

```sh
AURIGA_URL=http://127.0.0.1:8767 \
AURIGA_BROWSER=firefox \
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_SCREENSHOT_DIR=runs/web-replay-video-check \
node web/check-replay-video.mjs
```

Use `AURIGA_VIDEO_EXPORT_EVENT_ID` to select a specific immutable export when several exports exist. This check creates screenshots and a browser report; it runs on previously recorded media without a model or GPU.

## Permanent experiment library

The default library view organizes experiments into collapsible research directions. Selecting a direction or scheme includes every version in that direction and every explicitly recorded baseline, without a column limit. One table displays every scene as a row and every scheme as a column, with score, exact start/end timestamps and video in each cell. Sticky labels and an independently scrollable table keep large comparisons usable. Media loads near the visible cells; explicit row playback loads and synchronizes the whole row. Shared playback aligns recording-relative seconds, not physical action phases. Explicitly registered recoveries share one logical experiment; old URLs resolve through aliases, and every attempt retains its timestamps and detail link. The first formal score, including zero, remains unchanged. The **全部运行** tab retains the archive and detail interface.

The comparison view first requests the compact group summary (including exact paired score inputs), then fetches full cases for selected groups and the current overview. Video selection uses the media manifest endpoint; full reports are loaded when opening a run's details. The run catalog is fetched when switching to **全部运行**. JSON responses use private conditional caching and negotiated compression. Healthy SSE notifications drive catalog updates; the five-second fallback runs only while the notification connection is unavailable, and hidden pages wait until visible again. Visible or playing comparison videos revalidate only their small media manifests every five seconds, because exports can finish after the catalog's last event. Recordings remain on the experiment machine. Timestamps use explicit archive evidence, UTC storage and Beijing display to the second; registration/import times cannot stand in for missing execution times. URL `scope=direction` follows direction membership, while `scope=custom` and legacy explicit comparison links retain manual selection.

Build an isolated preview so the running service's `web/dist` is not changed, then run the synthetic comparison check. Every network request is intercepted; CPU ffmpeg generates its small MP4 fixture. This checks score intersections, unknown denominators, iteration labels, multi-player decoding/playback, refresh preservation, errors, URL state, keyboard use and mobile layout:

```sh
node web/build.mjs --outdir /tmp/auriga-comparison-preview
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_BROWSER=firefox \
AURIGA_PREVIEW_ASSETS=/tmp/auriga-comparison-preview \
AURIGA_SCREENSHOT_DIR=/tmp/auriga-comparison-check \
node web/check-comparisons.mjs
```

The `comparison-model.test.mjs` regressions run in `npm run check`, while `test_experiment_comparisons` and `test_experiments` cover the Python grouping and HTTP projection. Real archived video and manifest provenance require a separate read-only check; synthetic fixtures are not benchmark evidence.

`http://127.0.0.1:8780` lists and filters experiments under `~/exp/auriga`. It scopes each workbench HTTP/SSE/WebSocket/artifact URL to `/runs/{catalog_id}/`; verified gateways retain their live controls, recorded streams and historical evidence stay read-only. See [experiment-library](../docs/development/experiment-library.md) for storage and service setup.

```sh
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_BROWSER=firefox AURIGA_URL=http://127.0.0.1:8780 \
AURIGA_SCREENSHOT_DIR=~/exp/auriga/service/validation/library \
node web/check-library.mjs
```

This check uses existing archives for run scoping and layout, plus an explicitly browser-only synthetic catalog for update/draft/focus/pagination checks. Use `check-replay-video.mjs` with `AURIGA_URL=http://127.0.0.1:8780/runs/<catalog_id>/` for actual MP4 decode, PTS and generation checks.

Completed original runs also offer **下载原始轨迹**. The portable ZIP can be passed directly to the [HD replay CLI](../docs/development/hd-replay.md) with a chosen resolution. This browser check downloads two real runs, checks attachment/HEAD/Range/SHA256, and imports the downloaded files with `--inspect`; it submits no model or robot requests:

```sh
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_BROWSER=firefox AURIGA_URL=http://127.0.0.1:8780 \
AURIGA_PYTHON=/path/to/Auriga/bin/python \
AURIGA_RUN_IDS=<catalog_id_1>,<catalog_id_2> \
AURIGA_SCREENSHOT_DIR=/path/to/new-validation-output \
node web/check-trajectory-download.mjs
```


## Phase review and camera previews

The experiment library shows Chinese environment/task labels and two direct players: head camera as the main view, plus selectable left/right wrist. Other images and recordings are not enumerated. Source selection, auxiliary-camera choice and playback survive catalog refreshes. Task translations live in `task-labels.js`; unknown identifiers remain explicitly untranslated.

Phase navigation and the “观察与动作” list share a selection. Camera changes retain the phase; seeking within a recording updates the selected phase. Clip boundaries come only from verified `frame_mapping` PTS and action receipts with matching run/episode/environment/generation and step domain. No media is generated or transcoded by the UI. A stage with missing boundaries, a different generation or zero movement is not given an invented clip. SSE catch-up is shown as loading rather than missing video; action reads retry after transient failure, and the stage refresh button retries exhausted reads.

The context pane shows only saved prompt, visible messages/reasoning, exact tool arguments (including `language` when recorded), results and Harness receipts. Empty reasoning remains explicitly empty. The legacy terminal has no item-to-byte-offset index: after its retained history is synchronized, the UI can scroll to a unique matching visible string. Repeated or absent text produces an explicit limitation and leaves the structured stage context available.

Run the read-only browser check against a recorded run. It checks actual camera decoding and PTS, then browser-only fixtures cover pauses/missing videos, generation and boundary rejection, continuous playback, seeking, context, retries and empty runs. These checks submit no robot, model, evaluator or run-control requests:

```sh
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_BROWSER=chromium AURIGA_BROWSER_EXECUTABLE=/path/to/google-chrome \
AURIGA_URL=http://127.0.0.1:8780 AURIGA_RUN_ID=<catalog_id> \
node web/check-phases.mjs
```

For pre-publication preview, build the bundle outside `web/dist` and set `AURIGA_PREVIEW_ASSETS` to that directory. Browser routes serve current source HTML/CSS and the preview bundle; run APIs remain read-only. Add `AURIGA_SYNTHETIC_ONLY=1` to run the phase or library check entirely from synthetic browser routes without contacting the service. The phase fixture requires CPU `ffmpeg` to create its small test clip.


Builds stamp every delivered HTML script/style URL with the SHA-256 of its asset bytes. The library serves HTML with `Cache-Control: no-store` and static files with `no-cache, max-age=0, must-revalidate`. This prevents a deployment from reusing an older bundle at the same browser/CDN cache key. `node web/build.mjs --outdir /tmp/auriga-build-preview` builds outside the live static directory. After deployments, check actual served HTML references and cache headers as well as browser behavior; origin-only browser checks do not establish authenticated public delivery through an access-controlling reverse proxy.


Both the selected library sidebar and run detail show the same observer-only task review. The library's `/runs/{catalog_id}/v1/task-review` and standalone Gateway's `/v1/task-review` read the official task instruction, statically parsed scoring rules, and persisted evaluation/summary. Human-readable predicates are enabled only for the reviewed source hashes. Repository commit and diff fingerprints qualify whether installed rules match the historical run. Native process scores, normalized scores and success remain separate; an explicitly unscored episode never acquires a formal score from a partial snapshot. Missing per-condition outcomes stay unknown. These human HTTP APIs and their linked evidence downloads accept only GET/HEAD and are absent from actor tools and prompts. The standalone viewer reads only its current run, without a library catalog scan.

Codex replay opens the native TUI tab by default. Healthy original PTY recordings stream unchanged. For completed archives whose PTY is missing or failed account bootstrap, the library runs the installed native Codex CLI against a private copy of the saved session and captures its real ANSI output. The viewer has no copied credentials, cannot create Internet sockets, and can write only inside its generated cache; no prompt is submitted. The library verifies unchanged turns/items and source hashes before publishing the result. The page labels this as a fresh native rendering of the saved archive, with its recovery time; it is not a recording of the original display timing. The original failed log remains separately downloadable. Phase-synchronized messages and tool records remain available in the supporting tab.

New ChatGPT-authenticated sessions route native account discovery through a separate local HTTPS relay. Only `GET /backend-api/wham/accounts/check` at the fixed default ChatGPT upstream is allowed; redirects, other methods and paths are rejected. The actor receives the public per-run CA and access to this relay port, while signing keys remain private. Model requests still use the existing model proxy. Custom ChatGPT backend URLs fail explicitly rather than being silently redirected.

On desktop, the first downward wheel gestures move the shared search area out of view. The list and selected detail then use nearly the full viewport height and scroll independently. Scrolling upward from the top of either column reveals the search area again; the list header also offers a search shortcut that preserves both column positions. Nested evidence scrollers, camera controls, zoom gestures and horizontal scrolling retain their native behavior. Mobile layouts retain normal document scrolling. The replay link appears above the camera videos.

`check-native-terminal.mjs` validates a real archive's native ANSI stream, recorded prompt and tool counts, visible xterm rendering, default tab, original-log download, read-only controls, cursor reconnects and refresh. Use the same browser environment as the phase check; `AURIGA_RUN_ID` selects the archive. It does not submit a prompt or robot action.

`check-task-review.mjs` checks matching rules and score derivation on both pages, explicit unscored results, phase-synchronized structured messages despite a failed TUI, raw terminal availability, text safety and responsive layouts. It accepts the same preview, real-only and synthetic-only options as `check-phases.mjs`. Continuous playback reuses the running media clock at compatible recorded boundaries; real gaps or source changes still seek and show why. Existing exports are sparse 5-fps observation sequences, so this does not create the unrecorded motion between observations.

The same check now covers final scoring on live-to-replay transitions without a reload or click, incremental record cursors, the 2000-item current view, and explicit access to earlier items. `/v1/records` shares its read projection between the gateway and library; old servers retain their original read routes as a compatibility fallback, preserving the complete returned item/observation/request arrays because those routes cannot page. The browser retains at most 5000 stage events and 96 camera exports. “较早记录” reads a 200-item historical window; its label makes context outside that window explicit. A history window includes at most 5000 correlated events; if that limit or the current stage-event limit is reached, the page links the complete `/v1/events?after=0` stream. Continue that stream from any recorded `server_seq` using `after`; switching to an earlier item window does not recover omitted events for the same item. The original evidence stays on the server, and returning to current records only reloads this viewer.

Source builds still use `web/dist`. Distribution builds can run `node web/build.mjs --outdir ../auriga/web_assets` before building the wheel; the server resolves the packaged assets when no source build is present. No second copy of the web source is maintained. Native archive recovery records `original_recording=false`, the CLI version and a probed database schema contract; an incompatible archive is rejected before the isolated CLI is started.
