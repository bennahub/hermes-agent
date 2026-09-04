# Astra C1 native macOS revalidation

Base: dbcd325f5cb9bb8f48dc97a0266bc204713dc34e. Independent execution on the Owner's
Mac, macOS 26.5.2 arm64, installed cua-driver 0.23.2, on 2026-09-05 KSA.
The inherited accepted verdict was reopened after a real native interaction
exposed a wrapper compatibility defect. No Mac Worker was introduced.

## Repair

The installed driver returns opaque element tokens and advertises the
`element_token` input property, but does not advertise the legacy
`accessibility.element_tokens` capability name. Hermes captured the tokens but
dropped them before click/set_value. The driver rejected a bare element index.
The wrapper now accepts the per-tool input schema or legacy capability, and
still omits the field when neither is advertised. Two schema-only regressions
(click and set_value) failed before the repair; both unsupported-schema controls
passed. Legacy capability tests explicitly disable schema support.

The inherited overlay-spawn test incorrectly expected a direct binary on macOS.
The same failure was reproduced on pristine source. The test now verifies the
native macOS LaunchServices wrapper and preserves the no-overlay assertion;
other hosts keep their direct-binary expectation. No sys.platform spoofing.

## Direct native evidence

The standard-permission backend started a separate TextEdit instance with a
unique harmless fixture. Target selection required its exact PID and window ID,
then a fresh accessibility capture and opaque element token. `set_value` changed
the fixture to ASTRA_NATIVE_PROOF; another capture read that value back. The
newly launched fixture process was killed and the backend session stopped.

At before-start, after-start, after-launch, after-click, after-set-value and
after-stop samples, the cursor remained [719.85546875, 420.61328125], foreground
bundle remained com.openai.codex, and active Space remained 1. This proves the
observed workflow did not steal cursor/focus/Space; it is not a universal claim
about every third-party app. Accessibility and Screen Recording were granted,
AX trust was confirmed, and no extra permissions or credentials were added.

Supported limitations remain explicit: AXPress on a text area is unsupported by
TextEdit (AX error -25206); exact AXValue editing succeeds. Process-wide typing
was correctly refused when the new app process restored a sibling window,
because delivery could not be proven to the exact fixture. No sibling document
was inspected or changed. An exploratory follow-up found window buttons with
empty labels and stopped without guessing which button to press.

## Validation and release state

Relevant native/CLI/tool exposure, session teardown, no-overlay, input target,
approval isolation and token suites: 198 passed, 5 platform skips, 15 files.
Focused legacy/schema tests were rerun after clarifying the legacy mock.
Evidence is preserved in the supervisor workspace under work/c1-evidence.

This document is execution evidence, not independent acceptance. The supervisor
records exact reviewed commit, review findings/repair and deployed hash after
review. Local installed cua_backend.py before deployment is
a2b20e11183851550b37c9bb0d1a4bc260a52b889eb754e893ed4a2dd7ddb664;
it matches this candidate's base despite unrelated dirty local work. Deploy
only that product file after hash verification and a selective backup.
