# Cross-client parity: Hermes shared contract

Candidate base: `30dacc1c4154f26aae20a754f3471c8ed396d054`

Branch: `bwm808-parity-shared-core`

The canonical corpus is `tests/fixtures/cross_client_parity_v1.json`. It covers the
Owner message, direct Agent-to-Owner reply, A2A acknowledgement, collaboration run,
runtime-shaped user row, recovery continuation, optimistic/durable reconciliation,
image, PDF, multiple attachments, voice with transcript, and Needs You.

This report establishes the Hermes server/shared contract only. It does not claim
that a mobile or macOS adapter consumes every field or passes these fixtures.

## Contract

For a natively attributed durable row, `session.history` now includes:

- `semantic_kind`: `owner_message`, `agent_message`, `collaboration`, or `hidden`.
- `owner_authored`: derived only from native provenance.
- `owner_notification_eligible` and `owner_unread_eligible`: the same Hermes
  `owner_attention` predicate used by canonical read state and push eligibility.
- `message_identity`: durable row/session identity, client correlation IDs, and a
  reply target reduced to the producer's `message_id`. Reply excerpts do not
  establish identity.
- `attachments`: ordered artifact records with no filesystem paths.
- `transcript`: optional versioned STT metadata bound to an audio artifact.

Owner-facing history and REST projections recursively remove path and backend
fields and path-like values from `display_metadata`. The durable row remains intact
for internal use and export. Collaboration rows sharing one native process/event
identity project as one compact, body-free summary card, so the internal A2A memo is
not returned as owner-facing card content.

Untyped legacy rows retain their existing wire shape. `owner_semantics()` reports
their authorship as unresolved (`None`) rather than inferring it from role or text.

Attachment-only messages remain present in history even when the caption is empty.
An attachment batch may carry an explicit version-1 transcript. Hermes accepts it
only when its `audio_item_id` names an audio item in that batch and its text exactly
matches the caption delivered to the agent. After staging, Hermes replaces that
client-known identity with the canonical `audio_artifact_id` and persists both.
If both identities are supplied they must resolve to the same staged audio record,
independent of attachment order. Hermes does not infer a transcript from a caption.
A rejected association leaves a durable refusal tombstone and removes the
unaccepted artifact rows and blobs. Partial execution-copy preparation removes each
copy created by that attempt before the refusal is persisted; no dispatch occurs.
When a previous process dies between copies, a later pre-admission refusal also
removes the batch's complete private execution namespace. Post-dispatch states are
ineligible for this cleanup, so accepted/shared data remains untouched.

An image is admitted when its bytes decode, or carry an image signature this
build recognises. The type kept on the durable record — and served back by the
file route — is read off the bytes, never trusted from the client's label: iOS
hands PNG bytes back for some screenshots requested as `image/jpeg`, and the
send goes through with the record corrected to `image/png`. Wire-type spelling
still decides by drawn type, not by the reader's name: a camera still carrying
an MPF multi-picture segment — what a current iPhone writes for an HDR gain map
— is a JPEG, and `image/jpg`, `image/mpo` and the other honest synonyms name the
same bytes. Formats this build cannot decode in full are accepted on signature
alone (an iPhone HEIC original is admitted as `image/heic`); a reader with no
registered wire type stores the reader's own name (`image/dds`), never a label
it cannot corroborate. Only bytes that neither decode nor match a signature are
refused.

A refusal that one named item caused carries that item in the error:
`attachment_batch.refusal` is `{reason, code, item_id, filename}`, and the RPC
error message is that same `reason`, so a client renders one sentence naming the
file to change instead of a generic batch failure. Chat and room uploads compose
the same refusal. The reason is stored with the tombstone, so a resend of the
same refused identity with the same files is answered with the same sentence
rather than decaying into the generic refusal.

A refusal never names a file that is not part of the submission being answered.
Once the Owner has changed the files behind an already-refused identity, the
stored reason no longer describes what was sent, and the reply becomes the one
fact still true — the identity is spent, so send a new message (`identity_refused`,
with `item_id` and `filename` null). A resend whose *new* files are themselves
inadmissible is answered for those files, not for the previous attempt's. A
refusal with no single responsible item omits `refusal` and keeps the generic
wording. Codes in use: `image_unreadable` (no reader opens these bytes and no
signature claims them), `mime_invalid`, `item_too_large`, `item_empty`,
`data_corrupt`, `identity_refused`. The former `image_format_unsupported` and
`image_type_mismatch` are retired with label-trust: a recognised image is
admitted under its true type instead of being refused.

`attachments.capabilities` additionally reports `image_mime_types`: the image
wire types this build decodes in full, derived from the installed decoders. A
client should still convert at the picker when its file's type is absent — a
build with no HEIF decoder does not list `image/heic` — though no upload is
refused on that ground any more; the stored record simply says what the bytes
are.

Push eligibility and unread eligibility both evaluate the same `owner_attention`
predicate with the actual message role and content. Blank, silent, and peer-only
events therefore remain consistent across both paths.

## Verification

- Hermes parity corpus, attachment batches/refusal, native projection, native
  message identity, canonical profile/read state: 121 passed.
- History/attachment/message-projection/read-state regression selection from the
  large TUI gateway suite: 15 passed, 635 deselected.
- Python byte compilation and `git diff --check`: passed.

The test runtime used the installed Hermes dependency environment plus an isolated
pytest package path because this worktree has no development virtual environment.

## Remaining integration gaps

- Mobile and macOS adapter consumption, presentation tests, and real-device journeys
  were not executed in this Hermes-only worktree.
- Existing clients that omit `attachment_batch.transcript` continue to work, but a
  voice transcript becomes cross-device canonical only after clients send the
  explicit metadata.
- Hermes returns a canonical compact collaboration projection; client card layout
  and navigation into the underlying exchange remain adapter responsibilities.
- Historical untyped rows remain unresolved rather than being guessed into Owner,
  peer, or runtime provenance.
- Real cross-device sends, read propagation, previews, and notifications require a
  frozen integrated candidate. No live VPS, profile, database, or runtime was
  modified here.
