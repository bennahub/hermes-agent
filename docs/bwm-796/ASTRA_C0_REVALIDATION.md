# Astra C0 independent revalidation — candidate for deployed acceptance

Date: 2026-09-05. Status: **INCOMPLETE; engineering candidate, not Owner acceptance.**
The previous PASS/READY documents are historical evidence. This report does not
replace the mandatory public executor journey, independent public browser review,
Owner public-page acceptance, or later GitHub BrowserIdentity canary.

## Provenance

- Fork main / C0 base: `1d48bd42cffdf7ce9e1db5a50076604d0ad13881`.
- Inherited dirty overlay preserved without editing its source as commit
  `b3ee32c0f6` on `codex/bwm-796-c0-revalidation`.
- Review candidate: branch `codex/bwm-796-c0-final`, worktree
  `/Users/abdulrahman/hermes-worktrees/astra-bwm-796-c0`.
- Original dirty source: `/Users/abdulrahman/hermes-worktrees/bwm-796-canonical-merge`;
  preserved unchanged.
- Current upstream main observed independently: `71f8c60f6a6ab8f2fab1e4d5c22d6dd9c856b63e`.
- VPS remains its separate dirty deployment identity. No whole-tree replacement,
  fork merge, or deployment was performed by this node.
- All 17 C0 product-file preimages matched the inherited overlay/base; the
  repair changes nine product files. Exact hashes and deploy flags are recorded
  in the program's `work/c0-evidence/deployment-manifest.json`.

## Architecture decision: REPAIR

Keep AgentComputer, BrowserIdentity, exclusive ControlLease, private loopback CDP,
and authenticated same-origin HTTPS/WebSocket transport. Retain the pinned
1440×900 browser viewport, with one display-to-browser coordinate transform.

An isolated empty-profile probe on the actual VPS Chromium exercised a page
changing every 100ms. `Page.startScreencast` emitted **zero frames in four seconds**.
Twenty `Page.captureScreenshot` calls over one persistent socket had median
**50.5ms** capture latency and mean **22,664 base64 bytes** per simple fixture frame.
The isolated browser was terminated and its temporary profile removed.

The previous code ran a screenshot pump and native screencast concurrently, then
waited for an ACK inside the only CDP reader. That reader could not receive its
own awaited reply. It also retained unused duplicate input dispatch code. The
candidate uses one persistent CDP screenshot connection with bounded client ACKs;
no native screencast, no competing frame pump, and no duplicated input queue.

Native supported CDP commands are documented at
[Page](https://chromedevtools.github.io/devtools-protocol/tot/Page/) and
[Input](https://chromedevtools.github.io/devtools-protocol/tot/Input/).
The VPS also directly rejected the inherited `Page.goBack` command; history now
uses `Page.getNavigationHistory` and `Page.navigateToHistoryEntry`.

The current evidence supports this narrow repair. WebRTC, VNC, or a desktop OS
would add a media/runtime layer without resolving the proven authority, history,
URL, key, and layout bugs. User-perceived stream quality and latency still require
the public deployed journey; these transport measurements alone do not accept UX.

## Reproduced defects and repairs

- History buttons used nonexistent CDP methods. Real Chromium Back/Forward/Reload
  now run and are covered against an actual browser.
- Link/form navigation left the trust chrome on an old origin. Each frame now
  carries browser-derived location/title; full URL is available in fullscreen.
  URL userinfo is removed and origin uses the actual hostname.
- Client discarded Escape, lost agent name on partial takeover responses, and
  treated PointerEvent.detail as a double-click counter. Escape, Majed identity,
  explicit multi-click counting, direct paste, and editing shortcuts are repaired.
- ASGI input called blocking private CDP work on its event loop. Input runs in a
  worker while retaining serialized, immediate lease/fencing checks.
- Disconnect errors were in the hidden chooser. Live errors remain visible,
  input pauses until a frame has decoded, stale socket callbacks cannot draw,
  ACKs follow successful rendering, and retry resets require a displayed frame.
- Narrow header switched to a wrapping column with a 100% vertical basis. At
  406px the toolbar overflowed to 576px. The repaired header uses a nonwrapping
  column, and actual browser layout assertions now pass.
- Process-local RLocks did not serialize input/takeover or runtime admission
  across agents and serve. Reuse the existing Hermes bounded native file-lock
  helper for the full control operation, failing closed if locking is unavailable.
- There was no active-computer admission limit, and recovery bypassed wake.
  `agent_computer.max_active_computers` defaults to 2; wake and recovery count
  actual reachable managed runtimes, reconcile stale ready rows, and serialize
  check plus launch. Existing awake instances can resume at the limit.
- Agent suspend could interrupt Owner control. It now rejects; the Owner has a
  chooser Suspend action to release an unused workspace deliberately.
- Identity replacement/detach/revoke left the old runtime usable. They now stop
  the previous mounted runtime, expire tokens/revoke leases, advance fencing,
  clear the old workspace projection, and release its identity lock before reuse.
- A persisted PID could refer to an unrelated process after restart. Termination
  now verifies the managed user-data-dir argument before using a recovered PID.
- Input and frame sockets could independently select different page targets.
  A runtime handle pins its selected tab so visible page and input stay aligned.
  Multi-tab browser UI is not introduced by this repair.

Configuration uses the existing non-secret `config.yaml` path:

```yaml
agent_computer:
  runtime: chromium
  max_active_computers: 2
```

## Test evidence

Executed using `scripts/run_tests.sh`; no direct pytest invocation.

The inherited four suites passed **77/77**, despite history/origin/layout defects.
The initial repaired candidate passed **88/88 across five files**, including six real
Chromium tests and nine added control/resource regressions. Final local run:
**13.9 seconds, two workers, zero failures, no flaky retry pass**.

```sh
HERMES_TEST_WORKERS=2 scripts/run_tests.sh \
  tests/gateway/test_agent_computer.py \
  tests/gateway/test_agent_computer_stream.py \
  tests/gateway/test_agent_computer_chromium.py \
  tests/gateway/test_agent_computer_main_integration.py \
  tests/gateway/test_agent_computer_regressions.py
```

Real browser regressions cover private stream frames plus direct input,
Ctrl/Command-style Select All, Backspace, link-driven URL changes, Back/Forward/
Reload, bounded unacknowledged frames, suspend/resume URL persistence, client
Escape and double-click dispatch, fullscreen URL and Give Back, correct agent
name, and 406px layout. Client DOM tests use a deterministic test transport;
they are not claimed as public production journey evidence.

The multiprocess test starts two independently spawned service instances behind
a barrier with a one-runtime limit: exactly one starts and one receives admission
rejection. Further tests cover stale ready reconciliation, service reattach,
recovery admission, identity fencing, and a real unrelated subprocess surviving
a stale-PID termination attempt.

External review found that the inherited module-level `macos_only` marker would
skip all six real Chromium tests on the deployed Linux host. A test-only repair
now selects the actual native host marker: `macos_only` on macOS, `linux_only` on
Linux, and an explicit skip elsewhere. The Mac CI selector is retained without
spoofing `sys.platform`. On Linux the tests reuse Hermes's browser cache roots to
find an installed native Chromium executable, then set the existing runtime
binary override within the isolated test fixture. No browser is downloaded, no
live identity is used, and product files remain byte-identical to `90f4285`.

The VPS has an executable at
`/home/hermes/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`.
After this test-only repair, the native Mac CI selection
`scripts/run_tests.sh tests/gateway/test_agent_computer_chromium.py -m macos_only`
passes all **six tests**, with zero failures, in 13.1 seconds.
Native Linux execution is a separate required check against deployed product
modules in the protected disposable test tree; the earlier Mac pass is not
represented as a Linux pass.

### Native Linux findings and narrow follow-up

The first protected run against the deployed `90f4285` source executed all six
real Chromium cases and exposed two failures: an Owner REST pixel click missed
its button, and one cold-wake history case reopened a stale page. The test unit
mounted the production Hermes home read-only, used a private network and private
temporary directories, and verified actual imported module hashes.

A separate protected comparison using the preserved inherited `b3ee32c0f6`
package reproduced the click defect. Linux reported a 1440×757 screenshot and
CSS viewport, while the handle retained a configured 1440×900 stream viewport.
The REST input path scaled y=184 below the intended button. The follow-up stores
the observed CSS dimensions alongside each REST screenshot and uses that pair
for REST pointer mapping. It leaves the configured stream viewport unchanged.

The inherited package also consistently lost a native-link navigation during
suspend. `90f4285` saved the current URL correctly, but its cold-wake history
failure was timing/session-dependent: isolated repeats passed. The follow-up
forces the saved URL only when launching a new Chromium process, whose disk
session can contain an older page. It waits for the requested navigation's loader
to become the active top-frame loader. Reattaching an existing live process keeps
its current page. An actual second-service reattach test verifies that behavior;
a deterministic delayed-loader regression verifies cold-restore completion.

The protected Linux follow-up variant passes **91/91** in **14.3 seconds**: all
**90 C0 cases across five files** (six real Chromium, eleven regressions) plus one
supplemental native-navigation restore probe. This variant imports an explicitly
copied candidate C0 package inside the disposable tree; it is not represented as
an already deployed fix. Root must independently review and deploy the single
changed product file, `gateway/agent_computer/adapter.py`, then rerun against the
actual deployed module paths/hashes. No production runtime or identity was
changed by these diagnostic/test runs.

The exact same final five-file suite also passes **90/90 on native macOS**, with
zero failures in **16.5 seconds**, through the canonical runner. Both hosts retain
their real platform identity; no `sys.platform` substitution was used.

### Native Enter default-action follow-up

Root's public Wikipedia journey reopened a keyboard defect: typing and Backspace
worked, but two Return attempts did not submit the search form; clicking Search
with the same text navigated immediately. The earlier fixture's custom keydown
handler had hidden the distinction between a raw key event and a native default
action.

An independent protected Linux comparison against unchanged deployed `5936efec`
reproduced the failure with an ordinary GET form containing no JavaScript handler.
The exact same scenario passed when Enter alone used CDP `keyDown` with carriage
return `text`/`unmodifiedText`, followed by a text-free `keyUp`. This also produced
native textarea newlines for Enter and Shift+Enter. The
[CDP Input contract](https://chromedevtools.github.io/devtools-protocol/tot/Input/#method-dispatchKeyEvent)
documents the event types and character fields; native Chromium execution proves
the submission/editing behavior here.

The product follow-up adds four lines in `keys.py`. Plain/Shift Enter uses the
character/default-action path; Ctrl/Alt/Meta combinations retain raw keydown and
keyup remains text-free. The new real-browser regression types `zeusx`, removes
the final character with Backspace, submits a native GET form with Enter, verifies
its query value, then verifies plain/Shift Enter textarea newlines. Unit coverage
also checks the release and Ctrl+Enter payloads.

The final focused suite now passes **91/91 on native Mac (18.9 seconds)** and
**91/91 in the protected native Linux candidate tree (15.8 seconds)**, including
**seven real Chromium tests**. The deployed baseline failure and supported-event
comparison are preserved in `work/c0-evidence/enter-native-baseline-comparison.log`.
The candidate remains subject to external delta review, selective deployment and
a repeated public journey. The frame pump/runtime/resource architecture is
unchanged; the earlier resource measurements retain their exact measured identity.

## Deployment and remaining acceptance

Use the exact nine-file delta manifest, verify every live preimage before writes,
and preserve per-file rollback copies. Eight existing files plus new `locking.py`
are changed. No `tui_gateway`, `web_server`, `hosted_rooms`, unread, or BWM-797 files
are replaced. Confirm both serve and gateway use the repaired control module;
a serve-only restart is insufficient if gateway has imported its old copy.

For a deployment already at `90f4285`, apply only the reviewed native-Linux
follow-up adapter delta using `work/c0-evidence/native-repair-deployment-manifest.json`.
The earlier nine-file manifest describes the initial transition from the
inherited overlay, not a second whole-delta replay.

For a deployment already at `5936efec`, the Enter follow-up changes only
`gateway/agent_computer/keys.py`; use the 17-file final identity and one-file deploy
flag in `work/c0-evidence/enter-repair-deployment-manifest.json`.

After integration: check service health/restarts and re-run the focused suites,
then execute the full authenticated public `/computer` fixture and Wikipedia
journeys. Obtain an independent browser verdict and frozen deployed digest before
asking for one Owner UAT. GitHub credentials/canary remain prohibited until
`OWNER_UX_ACCEPTED`.

Re-measure one and two active workspaces on the final VPS architecture. Record
baseline global RAM/CPU/swap, each managed Chromium process tree's RSS/PSS/CPU,
frame rate, payload bandwidth, and input-to-visible-frame latency, both idle and
with a live stream. Use separate temporary profiles and deterministic public
fixtures; do not disturb an Owner lease. Terminate only probe-owned processes and
remove only probe-owned temporary profiles. The two-workspace default is an
admission bound pending final measurements, not a recommendation to upgrade RAM.
