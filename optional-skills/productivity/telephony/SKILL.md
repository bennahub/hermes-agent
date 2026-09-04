---
name: telephony
description: Call and text via Twilio, Wave, Bland, or Vapi.
version: 1.1.0
author: Nous Research
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [telephony, phone, sms, mms, voice, twilio, wave, bland.ai, vapi]
    related_skills: [maps, google-workspace, agentmail]
    category: productivity
---

# Telephony Skill

Use existing provider APIs for owned numbers, texts, direct calls and AI calls.
The helper is a CLI, not a new Hermes tool or inbound voice gateway. Wave supports
Saudi call initiation; a conversational flow must also be enabled by the provider.

## When to Use

- Find and manage an agent-owned number where the provider offers inventory.
- Send an authorized SMS/MMS or check incoming Twilio texts.
- Deliver a voice message or run an authorized conversational AI call.
- Inspect Saudi +966 setup and prepare a Wave call without dialing.

## Prerequisites

Use the intended profile's `HERMES_HOME`; personal work belongs to Hamad. Locate
`scripts/telephony.py` in the installed skill using `search_files` and invoke it
through `terminal`. Hermes' Python runtime is sufficient; the HTTP helper uses
stdlib and optionally reads Hermes' existing YAML configuration.

Install with `hermes skills install official/productivity/telephony`. When testing
an unmerged local candidate, install that reviewed candidate; the official hub
may still contain the earlier version.

Provider credentials are read from that process environment, its profile `.env`,
then legacy config entries. New credential files are written atomically with
private permissions. Do not paste secrets into chat, tool arguments or logs.
Use an Owner terminal or an existing approved secret injection path for setup.

| Provider | Required setup |
|---|---|
| Twilio | Account SID, Auth Token, owned number for outbound/inbox |
| Bland | API key; provider coverage suitable for the destination |
| Vapi | API key, imported/owned phone number ID, assistant settings |
| Wave | Production eligibility, key scopes, owned Saudi caller ID and account call flow |

Persistent credentials and owned identifiers use `<HERMES_HOME>/.env`.
`<HERMES_HOME>/telephony_state.json` stores owned-number IDs and inbox checkpoints.
New behavioral settings belong in `config.yaml`, not new environment variables.

## How to Run

Replace `SCRIPT` below with the located installed helper path. Begin with
`python "$SCRIPT" diagnose`. This is a configuration report, not proof of a
working account, caller ID ownership, voice quality or permission to make calls.

For Wave, the Owner can run this in their terminal; the API key is prompted
without echo and is absent from the command line:

```bash
python "$SCRIPT" save-wave --from-number '<owned +966 number>'
```

An optional positional API key remains for compatibility with existing private
setup automation. Do not put a real key in an agent-generated command. Existing
`save-twilio`, `save-bland` and `save-vapi` commands also remain compatible; use
Owner-side setup or secure injection for their credential arguments.

## Quick Reference

| Need | Command / provider |
|---|---|
| Saudi +966 call preparation | `wave-health`, `diagnose`, `wave-call` dry-run |
| Saudi production call initiation | `wave-call --confirm`, with verified account flow |
| Saudi call status | `wave-logs` (read-only, compact masked records) |
| Owned number where inventory exists | Twilio `twilio-search`, `twilio-buy`, `twilio-owned` |
| Set owned default | `twilio-set-default <E.164-or-PN-SID> --save-env` |
| Send text or media | `twilio-send-sms <to> <body> [--media-url <url>]` |
| Poll inbound texts | `twilio-inbox --since-last --mark-seen` |
| One-way TTS/audio | `twilio-call <to> --message <text>` or `--audio-url <url>` |
| Telephone IVR | Twilio direct call `--send-digits` (w = wait) |
| AI call | `ai-call <to> <task> --provider bland` or `--provider vapi` |
| Check AI outcome | `ai-status <call-id> --provider bland|vapi` |
| Vapi owned-number import | `vapi-import-twilio --save-env` |

Choose a provider from current destination and inventory evidence. Twilio's
Saudi pricing supports outbound termination but does not establish a local
+966 number for this program; its SA SMS guidelines say two-way SMS is unavailable.
Bland and Vapi need separate coverage/account checks, and cannot be assumed to
supply a Saudi caller identity.

Wave is the provisional Saudi option because its published API directly models
Saudi numbers and call initiation. Unifonic and CEQUENS are credible alternatives
if Wave cannot enable the required account; neither has a client in this helper.
Do not create another telephony backend to work around an account gate.

## Procedure

### 1. Resolve authorization and purpose

Use the user's existing instruction when it clearly authorizes this recipient
and call/message purpose. Ask only for a missing consequential authorization;
do not reconfirm an already authorized ordinary action. Number purchases,
subscriptions, account terms and unapproved charges remain Owner boundaries.
Never dial emergency numbers or use calling for harassment, spam or impersonation.

Record the call goal and stop condition before dialing. A restaurant inquiry and
a reservation commitment are different actions. Do not use a research instruction
as permission to book, pay or accept cancellation charges.

### 2. Verify current configuration

Run `diagnose`. Select the correct profile and inspect only presence/status,
not credential contents. A key prefix is not account validation. Do not infer
that a missing number can be supplied by another profile or an arbitrary caller ID.

For Wave, confirm with the provider before provisioning/payment:

- Whether production is available for this account and its KYC/business requirements.
- The exact +966 number type, inbound/outbound support and caller ID entitlement.
- Monthly number rental, inbound/outbound minutes, AI/recording fees, channel limits,
  taxes, minimum spend and cancellation terms.
- The enabled voice flow and whether a conversational AI outcome is retrievable.

Current Wave docs disagree about production availability and complete webhook
lifecycle support. Do not turn those claims into readiness. Sandbox callbacks
can ring real phones; a sandbox key is not permission to test-call someone.

### 3. Prepare using a dry-run or read-only operation

Wave commands:

```bash
python "$SCRIPT" wave-health
python "$SCRIPT" wave-call '<Saudi destination>'
python "$SCRIPT" wave-logs --limit 20
```

`wave-health` uses no credentials and proves only HTTP service health. Dry-run
validates destination/caller syntax and makes no HTTP request. `wave-logs` needs
`calls:read`. No command here allocates a Wave number or installs webhooks.

The exact `/v1/calls` request accepts `to`, `caller_id_name`, `caller_id_number`
and metadata. This helper does not accept an AI persona, live audio or a booking
task. WaveML AI flows are provisioned separately; do not promise a conversational
booking from successful call initiation.

### 4. Execute the authorized provider operation

For a verified Wave account, owned caller ID and enabled flow, an authorized
live call uses `wave-call '<Saudi destination>' --confirm`. The helper requires
a production key and caller ID. The provider remains responsible for checking
key validity, scopes, number ownership and account eligibility.

For Twilio, search with `twilio-search --country <ISO> --limit 5`; buy only when
purchase is authorized, using `twilio-buy '<number>' --save-env`. Import an owned
Twilio number into Vapi with `vapi-import-twilio --save-env` if needed.

For a direct Twilio voice message, choose `--message` or `--audio-url`. Existing
`text_to_speech` can prepare audio, but publishing private audio to a provider
must be within the task's authorization. This is one-way audio, not a conversation.

For an AI call, use `ai-call '<destination>' '<bounded task>' --provider bland|vapi
--max-duration 3`. For Vapi, first verify its imported phone-number ID.

### 5. Verify the outcome and stop

Poll call status/logs using the returned call ID. An HTTP accepted/initiated
response does not prove ringing, answering, conversation success or a booking.
If a call-creation request times out or fails ambiguously, inspect recent calls
before retrying; the Wave call endpoint has no documented idempotency key.

Twilio inbox polling supports `--since-last --mark-seen` to continue from the
saved checkpoint. Results may contain message content or OTPs: summarize only
what the task needs and keep secrets out of memory and follow-up notes.

## Pitfalls

- Do not persist third-party phone numbers in Hermes memory unless requested.
  Owned-number identifiers belong in configuration; mask destinations in reports.
- Do not assume a VoIP number works for account verification or passkeys.
- Wave SMS is not proven live; WhatsApp documentation is inconsistent and neither
  messaging path is implemented here. Voice is not a two-way SMS substitute.
- Wave's webhook guide says only `call.initiated` currently emits. No webhook
  server, inbound live-answering loop or conversational booking backend ships here.
- A shared hosted account is not permission to borrow personal credentials across profiles.
- No cancellation, payment or legal commitment may be inferred from a call response.

## Verification

Run `scripts/run_tests.sh tests/skills/test_telephony_skill.py -q` in the repo.
The suite checks persistence, dry-run isolation, Saudi validation, private file
creation, credential-free prompting, masked output and safe provider errors.
It does not prove production service, call quality, KYC, number assignment or
conversation E2E. After actual account setup, verify the specifically authorized
call through initiation, answered/ended state and the requested business outcome.

References: [Wave](https://docs.wave.sa/),
[Twilio phone numbers](https://www.twilio.com/docs/phone-numbers/api),
[Twilio voice](https://www.twilio.com/docs/voice/api/call-resource),
[Twilio messaging](https://www.twilio.com/docs/messaging/api/message-resource),
[Vapi](https://docs.vapi.ai/), [Bland](https://docs.bland.ai/).
