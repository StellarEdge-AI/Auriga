# Workbench and Experiment Library Web UI

The web interface attaches to a supervisor-owned Codex PTY and displays synchronized camera videos, stage timelines, and structured Harness records. Selecting a stage updates the video position and recorded context pane, and can scroll the terminal view to a unique matching string—without ever replacing the PTY process or injecting terminal input. Submitting new tasks or resolving native approval prompts requires acquiring a Gateway writer lease. Closing a browser tab releases its writer lease when reachable and never terminates the underlying run; newly opened tabs start in read-only monitoring mode and must claim input ownership explicitly.

## Building and Running Synthetic Checks

Install pinned frontend dependencies, run static checks, build the bundle, and run the Python terminal unit tests:

```sh
npm --prefix web ci
npm --prefix web run check
npm --prefix web run build
mamba run -n Auriga python -m unittest discover -s tests -p test_terminal.py -v
```

To launch the deterministic synthetic browser fixture (which requires no model or GPU and tags every emitted record with `synthetic_fixture`):

```sh
mamba run -n Auriga python web/fixture-server.py --port 8778
```

In a second shell, run the automated Playwright check against the fixture server (using either a Playwright-managed browser or a system Chromium/Chrome binary via `AURIGA_BROWSER=chromium AURIGA_BROWSER_EXECUTABLE=/usr/bin/google-chrome`):

```sh
AURIGA_URL=http://127.0.0.1:8778 \
AURIGA_SYNTHETIC=1 \
AURIGA_BROWSER=firefox \
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_SCREENSHOT_DIR=runs/web-synthetic-check \
node web/check-browser.mjs
```

Start a fresh `fixture-server.py` process before each full synthetic run because native approval requests are intentionally one-shot and cannot be reopened once resolved. The suite verifies independent motion and turn states, two-window writer lease handoffs (including rejecting a duplicate client ID lacking its lease token), native approvals and questions, task submission idempotency, trace identities and missing-feedback states, missing media handling, keyboard navigation, SSE deduplication and gap recovery, HTTP/WebSocket origin checks, and PTY session continuity across page refreshes, resizes, and tab close/reopen cycles. It also verifies that program-internal observations are distinguished from images delivered to the primary actor model, saving screenshots at `320`, `736`, `1024`, and `1440` pixel widths alongside a JSON report.

### Live / Recorded Gateway Checks

To validate against a running real Gateway service, omit `AURIGA_SYNTHETIC`, point `AURIGA_URL` at the gateway, and set `AURIGA_REQUIRE_ACTION=1` to require at least one completed physical action. In this mode, the script verifies the action's input, start, and terminal RoboDojo observations, cross-checks run and action IDs, verifies readable planned/dispatched/actual trajectory artifacts (recorded under `action_evidence`), and—when an immutable scene version contains a tracked proxy—queries its conditional geometry and creates a verified geometry snapshot (recorded under `scene_evidence`), without submitting any new task or robot action.

## Phase Playlist and Recorded Video Verification

The primary review interface is a **phase playlist**: each native tool call groups its correlated Harness actions, the active turn prompt, and the model messages and reasoning immediately preceding and following the call. Multiple physical actions inside a single tool call remain individually selectable clips, while non-moving or unmapped stages retain their text records and are skipped during continuous video playback. Raw observations, events, and video exports remain accessible in collapsible evidence sections showing each camera's run, episode, generation, sampling metadata, and full `frame_mapping`.

Selecting an action stage enables start/end video seeking only when the recording contains exact simulation-step-to-PTS boundary records matching the action's physical identity (or an exact terminal-observation frame anchor when a declared step domain is absent; otherwise the UI explicitly reports the missing mapping). Container FPS metadata is displayed for reference only and is never used to synthesize playback PTS timestamps. If the browser lacks a required video codec, a direct download link to the original MP4 remains available.

Run the read-only video playback check against an existing replay server (uses only `GET`/`HEAD` requests, decodes all three camera streams, seeks across action start/end PTS boundaries, captures native canvas frames, verifies HTTP Range headers, and checks cross-generation rejection):

```sh
AURIGA_URL=http://127.0.0.1:8767 \
AURIGA_BROWSER=firefox \
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_SCREENSHOT_DIR=runs/web-replay-video-check \
node web/check-replay-video.mjs
```

Set `AURIGA_VIDEO_EXPORT_EVENT_ID` to target a specific immutable video export when multiple exports exist.

## Permanent Experiment Library

The experiment library (typically served on `http://127.0.0.1:8780` over `~/exp/auriga`; see [Experiment Library](../docs/development/experiment-library.md)) organizes runs into collapsible research directions. Selecting a research direction or experimental scheme displays an unlimited-column comparison matrix where every row is a benchmark case/scene and every column is a scheme version or registered baseline, showing the score, exact UTC-backed start/end timestamps, and inline video preview in each cell. Sticky row/column headers and independent scrolling keep large matrices navigable. Video players load lazily near the viewport, while clicking row playback loads and synchronizes all cells in that row by recording-relative elapsed seconds. Explicitly registered recovery runs merge into their logical parent experiment while preserving every attempt's timestamps, detail links, and the first formal score (including `0`). Switching to the **All Runs** tab displays the full searchable run catalog and detail drawer.

When loading the comparison matrix, the browser first fetches the compact group summary (with exact paired score intersections), then fetches full case lists for selected groups, and requests individual media manifests on demand for visible video cells. Switching to the **All Runs** tab fetches the paginated run catalog. JSON endpoints use private conditional caching (`ETag` / `If-None-Match`) and compression, while live SSE notifications drive real-time catalog updates (falling back to a 5-second poll only while SSE is disconnected and pausing polling while the tab is hidden).

To build an isolated preview bundle without touching a running server's `web/dist` directory and run the synthetic comparison matrix check:

```sh
node web/build.mjs --outdir /tmp/auriga-comparison-preview
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_BROWSER=firefox \
AURIGA_PREVIEW_ASSETS=/tmp/auriga-comparison-preview \
AURIGA_SCREENSHOT_DIR=/tmp/auriga-comparison-check \
node web/check-comparisons.mjs
```

To run the read-only library check against a live library server:

```sh
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_BROWSER=firefox AURIGA_URL=http://127.0.0.1:8780 \
AURIGA_SCREENSHOT_DIR=~/exp/auriga/service/validation/library \
node web/check-library.mjs
```

Completed original runs also provide a **Download Raw Trajectory** action, exporting a portable ZIP archive that can be passed directly to the [HD Replay CLI](../docs/development/hd-replay.md) for high-resolution re-rendering. The trajectory download check verifies HTTP attachment headers, `HEAD`/`Range` support, SHA-256 integrity, and `hd_replay --inspect` import on two completed runs:

```sh
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_BROWSER=firefox AURIGA_URL=http://127.0.0.1:8780 \
AURIGA_PYTHON=/path/to/Auriga/bin/python \
AURIGA_RUN_IDS=<catalog_id_1>,<catalog_id_2> \
AURIGA_SCREENSHOT_DIR=/path/to/new-validation-output \
node web/check-trajectory-download.mjs
```

## Phase Review, Camera Players, and Native TUI Recovery

The run detail view features two synchronized video players: the head camera (`cam_head`) as the primary view, plus a toggleable left or right wrist camera (`cam_left_wrist` / `cam_right_wrist`). Phase navigation and the **Observations & Actions** timeline share selection state: switching wrist cameras preserves the active phase, and scrubbing the video timeline updates the highlighted phase. Task display labels are defined in `task-labels.js` (leaving unrecognized task identifiers untranslated).

The context pane displays only saved prompts, visible assistant messages and reasoning, exact tool call arguments, tool outputs, and Harness receipts (leaving empty reasoning blocks explicitly empty). For legacy terminal logs lacking an item-to-byte-offset index, the UI scrolls to a unique matching visible text line once history synchronization completes, or notes when a match is non-unique or absent.

Run the read-only phase and camera player verification suite against an archived run with:

```sh
PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs \
AURIGA_BROWSER=chromium AURIGA_BROWSER_EXECUTABLE=/path/to/google-chrome \
AURIGA_URL=http://127.0.0.1:8780 AURIGA_RUN_ID=<catalog_id> \
node web/check-phases.mjs
```

### Asset Cache Busting and Packaging

`node web/build.mjs` stamps every HTML `<script>` and `<link>` asset URL with the SHA-256 digest of its file bytes. The server delivers HTML documents with `Cache-Control: no-store` and static assets with `no-cache, max-age=0, must-revalidate` so browser and CDN caches never serve stale bundles across deployments. For wheel distribution builds, running `node web/build.mjs --outdir ../auriga/web_assets` packages the built assets directly into the Python wheel without maintaining a duplicate source tree.

### Observer Task Review, Native Codex TUI Replay, and Incremental Pagination

- **Observer Task Review (`/v1/task-review`)**: Both the library sidebar and the standalone run viewer display the official task instruction, statically parsed scoring rules, and persisted evaluator summary (rendering human-readable predicate breakdowns when the repository commit and diff fingerprint match the reviewed source hash). Native process scores, normalized scores, and binary success are reported separately, and unscored episodes never fabricate a formal score from partial snapshots.
- **Native Codex TUI Replay (`check-native-terminal.mjs`)**: Opens the native ANSI TUI tab by default. When an original PTY recording is healthy, it streams directly; for completed archives where the PTY log is missing or failed account bootstrap, the library renders a fresh read-only ANSI capture by running the installed Codex CLI against an isolated copy of the saved session (with no credentials or network access) and labels it as a post-hoc rendering alongside a download link for the original log.
- **Incremental Record Cursors (`check-task-review.mjs`)**: `/v1/records` paginates large runs using a 2,000-item current window (retaining up to 5,000 stage events and 96 camera exports in browser memory). Clicking **Earlier Records** loads a 200-item historical window with up to 5,000 correlated events and provides a direct link to the complete `/v1/events?after=0` stream (resumable from any `server_seq`) whenever event limits are reached.
