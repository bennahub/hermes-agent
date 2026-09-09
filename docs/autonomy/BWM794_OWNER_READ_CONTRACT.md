# BWM-794 canonical owner unread projection

`profiles.list(include_sessions=true).profiles[].canonical_session.owner_read_state`
is an additive versioned projection over the existing canonical Bot Chat and
native `sessions.last_read_at`. Version 1 returns `last_read_at` and
`latest_reply_at` as Unix seconds or null. There is no second read database,
profile metadata write, new provider call, or prompt/history mutation.

The latest reply comes from actual active assistant rows in the server-resolved
compression chain. Typed runtime rows, tool carriers, owner/system/tool rows,
incoming internal routing envelopes (user provenance) and bare silent results do not advance it. A real
assistant answer may be JSON, an error quote or prose beginning with “Message from”. Text alone does not erase assistant provenance. A valid published assistant
artifact counts even when prose is empty. Tool and owner artifact metadata does
not grant assistant authorship. Existing live demands remain a separate attention
contract; the persisted message normalizer does not create demands from rows.

The canonical read watermark is the maximum non-null native watermark in that
compression chain: a continuation created after its parent was read starts with
null and must not resurrect already-read parent messages. Ordinary parentage is
not compression lineage. The existing PATCH session `unread` writer continues to
stamp the entire native lineage; explicit mark-unread therefore sets all to zero.

Clients compare actual reply time with the native read watermark. A missing reply
means nothing unread. A null read watermark means no proven human read, so a real
reply counts as unread. This intentionally differs from the legacy general
`session_unread` NULL-means-untracked/read convention: upgrading may surface
historic conversations that have never received a native read watermark. Fetching
must not invent a read to suppress them. The generic session list is unchanged.

New clients replace local badges with this canonical state on every successful
roster refresh, including reconnect and restoration. Old servers without version
1 keep the previous local fallback. Reading uses the existing profile-scoped
native PATCH only, with the server-resolved canonical session identity. Incoming
replies may be marked read only while a human reader remains active. Background
leases retain their presentation identity but cannot consume unread; queued
writes recheck scope lifetime, reader visibility and canonical identity.

Deployment is additive: old clients ignore this field. Both updated clients are
needed for bidirectional consumption. This document does not claim physical UAT
or deployment; N0 owns integration and the sole live mutation queue.

## Observed read cursor followup

The existing native `PATCH /api/sessions/{id}` accepts optional `read_through`
(Unix seconds) with `unread: false`. Both updated clients send only the latest
durable, owner-visible assistant reply actually loaded into the transcript,
bounded by the server's canonical latest reply. Empty history, local optimistic
stream timestamps and roster previews do not produce a bounded read write.
Endpoints without version 1 metadata retain their native read-now behavior;
these legacy endpoints cannot promise a bounded cursor. Version 1 was not
deployed before this followup, and now includes bounded-read support. Initial opening and polling use the same queued publisher, which
rechecks scope disposal, active reader and canonical identity before dispatch.

The native lineage writer advances bounded reads monotonically across its existing
read watermark. A delayed read of reply A therefore leaves a newer unseen reply B
unread; an older request cannot regress another device's later read. A compression
child created after the root read retains the lineage maximum even if its own
column started null. Legacy callers omitting the cursor still get read-now.
Explicit `unread: true` without a cursor still resets the whole lineage to zero.
No second store or new route is introduced.
