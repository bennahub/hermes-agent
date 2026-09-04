# Astra C2 / C3 / C4 / C6 independent revalidation

Observed 2026-09-05 Asia/Riyadh (2026-09-04 UTC). This replaces inherited
capability verdicts; prior reports remain historical evidence. No account was
created, number bought, call placed, device toggled, reservation submitted or
credential disclosed. Code and preparation are executable; service acceptance
remains bounded by the evidence below.

## Ownership and provenance

Isolated candidate: `astra/bwm-802-capabilities-20260905` in
`/Users/abdulrahman/hermes-worktrees/astra-capabilities`.
BASE_SHA / initial CURRENT_HEAD: `dbcd325f5cb9bb8f48dc97a0266bc204713dc34e`.
Original dirty telephony and personal-operations paths were copied from
`bwm-802-autonomy` without altering that worktree. Their input digests are in
[the inherited manifest](astra-capabilities-evidence/inherited-digests.json).
The final candidate commit is recorded by N0's integration/review receipt;
this report does not label an unreviewed worktree as a reviewed commit.

Live VPS repository HEAD: `b2bc82cdb44a685eb1ec3c7850e6b09256347bcc`.
No live source/config was replaced for this investigation. Per-file identities:

| File | Local base SHA-256 | Live SHA-256 / relation |
|---|---|---|
| `agent/secret_scope.py` | `51d91d280ef05d59c0c450dbfaecd6f50b314a4ff75614cb23b5288a308c645d` | `9650a82a2e78df8875043abc49588eaadfc322ddf3ed475986d89ab712eb6fd2`; contains additional BWM-797 protection |
| `agent/file_safety.py` | `f2cb782cda3f091acba507e3af6a31e7326770dfc6b75657cd5adb1006521db9` | `5282dfa9b9b3c112c7d2028c6052aa01182c4d855dc78033ba063db20018b23e`; adds BrowserIdentity-store blocking |
| `tools/homeassistant_tool.py` | `84d5a4eb69bc88357c45fbc80cf072feaf8716d3b2d96ab5dd8b2f36c80a380b` | identical |
| `hermes_cli/auth.py` | `c834bec3e744a9da8be0dac08ed218be605a96f03b7639d1c13d73ede8b46f05` | identical |
| `hermes_cli/env_loader.py` | `26712ba3e020b5c306cd456e8d2cc96084fd2295f7ec4104466fffd0adeab87d` | identical |
| Telephony helper | inherited dirty input `66fcbc9d5d65b7027188f3af2c783f20b26c182c94cfceb1fc24ca9d2f326c66` | live repo helper `0c8d5b326a7c0ef7854388a71dc1e905fa819b5481f88f4bbf10c61e84eedf60`; Wave dirty work was not deployed |

Preserve the live secret-scope/file-safety overlays. They are not equivalent to
the 802 branch and do not belong to this capability patch.

## Independent node matrix

| Node | Inherited claim | Finding / decision | Truthful state |
|---|---|---|---|
| C2 | native credential path sufficient | KEEP native paths; no demonstrated vault need. Correct profile-isolation and passkey claims. Browser login acceptance depends on C0. | Architecture verified with nonblocking limitations; full credential journey remains dependent on C0 UAT/canary |
| C3 | Keep Wave, KYC only | REPAIR helper; Wave provisional. Public provider docs conflict about production, AI flow and webhooks. Commercial/eligibility facts unavailable publicly. | EXTERNAL_GATE after executable preparation; no live telephone E2E |
| C4 | no HA, native needs config | KEEP native HA. No instance detected within bounded current environment; actual device model unknown. | EXTERNAL_GATE |
| C6 | partial research E2E | REPAIR guidance and stale shortlist. Real public availability reached without reserving; Calendar absent. | EXTERNAL_GATE at Owner selection/authorization; REAL_BOOKING_E2E_ACCEPTED not earned |

N0 must carry the C0 dependency explicitly in C2's program-level terminal
classification. A native architecture review cannot certify Owner authentication.

## C2: five credential classes

| Class | Actual primitive and proof | Boundary |
|---|---|---|
| A. machine/API credentials | Profile `.env`, native `secret_scope`, provider `auth.json`; private files on both hosts; native tests green | Running process necessarily consumes credentials. Same-OS-user terminal access is not a vault boundary. |
| B. browser sessions | C0 managed BrowserIdentity, distinct from the Owner's daily browser; live file tools deny the store | N0 owns exact current C0 storage/lease tests and canary. No authenticated session was manufactured here. |
| C. passwords | Owner enters at genuine C0/C1 authentication checkpoint; use session afterward | Never route password values through model arguments. C0 UX has to work first. |
| D. passkeys / hardware auth | Authenticator remains Owner/device-controlled | Hosted Linux browser does not inherit the Mac authenticator. Remote/hybrid support depends on provider and available authenticators; not proven by a canvas. |
| E. human-only login / 2FA / CAPTCHA | Human Takeover on the same browser identity; native Mac checkpoint when applicable | Owner action/provider restrictions; no bypass, profile import or vault can prove this journey. |

Read-only inventory covered 15 active profile homes including default on each
host. Every `.env` and `auth.json` observed was mode 0600. Wave/HA variables were
absent across those profiles; Google credential files were absent in Hamad on
both hosts. No profile enabled 1Password, Bitwarden or
real-browser-profile import. `op`, `bws`, `bw` absent; Apple Passwords app present
on the Mac. Presence does not imply that a usable website password exists.
[Local inventory](astra-capabilities-evidence/local-inventory.json),
[VPS inventory](astra-capabilities-evidence/vps-inventory.json).

`auth.json` is not identical to strict `.env` isolation: its supported provider
resolution can read global-root auth when a profile has none, and refresh writes
can target the credential source. Tests explicitly cover that behavior. Live
BWM-797 adds opt-in `SealedScope` for `credentials.inherit_process_env: false`
and denies deployment-only dashboard/Sentry secrets. Do not flatten those into
one generic per-profile isolation claim or remove the overlay.

Native Bitwarden support is Secrets Manager (`bws`) credential hydration;
1Password support resolves `op://` references. Neither is a hosted-browser
password/autofill/passkey implementation. Native env-loader/cache handling is
already present. Installing a vault adds no proven missing capability here.
`read_file` protection and redaction are defense in depth: an authorized terminal
running as the same account can read files. That trust model must be described
truthfully, not called cryptographic isolation.

Validation: baseline 78 tests across six files included secret scope 24,
HA tools 37, auth provider scope 3 and file-mode 4. Extended local native suites
added 192 passes, with one Windows-only test skipped. Existing live native suites
passed 68 (secret scope 24, HA37, auth fallback7). These totals include overlap
and are not a unique-test count. Logs below. Real login/canary is N0/C0-owned.

## C3: current provider comparison and limits

The selected implementation remains the native optional telephony skill, with
Wave retained as a **provisional first account to evaluate**. The prior claims
that Wave is definitely the smallest, CST-licensed and fully self-service are
not independently established. No provider is operational in Hamad today.

| Requirement | Wave | Unifonic | CEQUENS | Twilio for Saudi Arabia |
|---|---|---|---|---|
| +966 inventory | API documents Saudi virtual allocations; real inventory/account enablement unverified | Number/caller-ID support through provider account; exact Saudi offer needs confirmation | DID capability marketed; exact +966 inventory and eligibility need quote | Current SIP pricing page says no voice-enabled numbers in locale; foreign-number termination does not supply +966 identity |
| Inbound/outbound | Published virtual-number instructions/forwarding and `/v1/calls` | Voice applications / outbound API | DID, forwarding, TTS/audio outbound API | Outbound Saudi landline/mobile termination supported |
| SMS / replies | SMS not live per channel guide; no supported SMS helper | SMS channel with registration; exact reply-capable inventory needs confirmation | SMS/MO products; Saudi routing and number type need confirmation | SA guidelines: no two-way SMS; no numeric long-code support |
| SIP | WaveML can dial SIP user; no proven customer trunk provisioning API | BYOC requires existing eligible SIP connectivity, configured through provider | Exact account SIP/BYOC offer unverified | Global SIP/BYOC; cannot infer local number |
| API/webhooks | Exact OpenAPI checked; call-initiation only is conservative emitted-event contract | Existing REST Voice/SMS and callbacks | Voice REST + DTMF/status callbacks | Mature REST/voice/messaging surfaces in native skill |
| Arabic / quality | Arabic/English TTS and Saudi dialect marketing; no call audio tested | Arabic market offering; no live quality/latency test | Multilingual/TTS voice including Zeina; no live quality test | Voice TTS options; no live route/quality test |
| Number/month | Unpublished | Exact rental unpublished | Exact rental unpublished | No comparable local number offer proven |
| Call/minute | Unpublished | Quote/consumption | Quote/usage | Published USD 0.1738 landline; 0.3122 mobile |
| SMS price | Not live/proven | Quote by routing/registration | Quote by route | Account/routing-specific; not a same-number two-way substitute |
| Channels/concurrency | WaveML concurrency concept; actual account channel price/limit unpublished | Quote | Quote | SIP page includes concurrency; CPS and number costs separate |
| Platform floor | Unpublished | Connect USD 499/month excluding tax, usage additional | Public usage pricing says no monthly minimum/subscription | Pay as you go |
| KYC / compliance | Nafath/national-ID error codes and Go-Live; actual personal/business eligibility unverified | Business/provider onboarding; exact case requirements need provider | Exact Saudi entity/KYC requirements need provider | Country/sender-specific requirements; no suitable shared local identity proven |

Sources checked directly on 2026-09-05 KSA:
[Wave introduction](https://docs.wave.sa/introduction),
[virtual numbers](https://docs.wave.sa/voice/virtual-numbers),
[calls](https://docs.wave.sa/voice/calls-and-recordings),
[WaveML](https://docs.wave.sa/voice/waveml),
[webhooks](https://docs.wave.sa/webhooks),
[sandbox limits](https://docs.wave.sa/sandbox-mode),
[authentication](https://docs.wave.sa/authentication),
[WhatsApp/channel status](https://docs.wave.sa/whatsapp),
[Unifonic pricing](https://www.unifonic.com/en/pricing),
[Unifonic BYOC](https://docs.unifonic.com/articles/products-documentation/bring-your-own-connectivity/),
[CEQUENS pricing](https://www.cequens.com/pricing),
[CEQUENS voice](https://www.cequens.com/products/voice-api),
[CEQUENS call API](https://developer.cequens.com/reference/simple-call-voice-sms),
[Twilio Saudi SMS](https://www.twilio.com/en-us/guidelines/sa/sms),
[Twilio Saudi voice prices](https://www.twilio.com/en-us/voice/pricing/sa),
[Twilio Saudi SIP prices](https://www.twilio.com/en-us/sip-trunking/pricing/sa).

Specific contradictory evidence matters more than marketing:

- Wave authentication describes Go-Live, while sandbox page still says arbitrary
  destination upgrade is forthcoming and introduction requires a Wave contact.
- WaveML AI callflows are account-provisioned. A newer public IVR-menu API exists,
  but its documented branch actions are not a generic conversational task API.
- Overview describes a full call event lifecycle; webhook page limits actual
  emission to initiation. Use polling for evidence and do not invent completion.
- Landing page examples include flow/language/webhook fields absent from strict
  `/v1/calls` OpenAPI. The helper uses the exact OpenAPI instead.
- Public CST VVSP material explains the licensing regime but the search did not
  establish Wave's legal entity/license. Ask vendor for the verifiable license
  identity; do not label marketing as regulator proof.
  [CST VVSP](https://www.cst.gov.sa/business/services/Permit-to-provide-virtual-voice-services-VVSP).

Fetched OpenAPI SHA-256:
`24fe9dd2335ab77a36b52db017fed1a90f387893773d70e6597d413f5fc10741`.
`GET /v1/health` returned `{status: ok, service: wave-api}` using the actual
helper. This is HTTP reachability only, not voice latency or quality.

### C3 repaired behavior

Six new regression cases failed on the inherited helper and pass after repair:
private credential-file creation; live key without caller ID remains gated;
caller normalization/rejection; masked accepted-call result; compact masked log
projection excluding metadata/recording URLs; provider-error suppression; and
hidden key prompting (some cases cover multiple invariants). Existing six tests
also pass. Credential writes are atomic mode 0600; multiline injection is rejected.
A key prefix still cannot prove production eligibility, and the helper now says so.
No provisioning, webhook server, AI audio backend or second provider was added.

The helper can initiate an account's configured flow; it cannot convert an
arbitrary Hamad booking prompt into a conversation. This is a vendor account-flow
gate until a supported native provider path is confirmed, not an invitation to
build another voice backend.

**One first Owner action:** obtain Wave's written Go-Live eligibility and quote
for one Saudi number, including the enabled outbound/AI flow and result retrieval.
The ready request is [the provider brief](C3_WAVE_PROVIDER_BRIEF.md). Do not buy or
supply a key yet if Wave cannot confirm these facts. No outreach was sent.
After a positive response, the executor rechecks the provider decision; the Owner
handles KYC/payment and inserts credentials through the hidden prompt. Then run
an expressly authorized bounded real call, verify outcome, and reclassify.

## C4: reachable environment and supported device path

Native tools are `ha_list_entities`, `ha_get_state`, `ha_list_services`,
`ha_call_service`; availability is gated by profile-scoped `HASS_TOKEN`.
Hamad has no token or URL on either host. Native HA tests pass against both
candidate-base and live code, but no physical device journey occurred.

Current Mac: en0 `192.168.68.64`, router `192.168.68.1`. One bounded connect to
port 8123 on 254 LAN hosts detected no open host. `homeassistant.local` and
`hassio.local` did not resolve; localhost and four known Tailscale endpoints had
no successful port-8123 connection. Two peers were offline. Five-second mDNS
observation produced no advertisement. VPS had no HA container/service/listener;
only Traefik and the exited previous Hermes container were listed.
[Reachability evidence](astra-capabilities-evidence/ha-reachability.json).

This proves no HA **detected by those checks**, not that HA cannot exist behind
a different port, disconnected VLAN, unknown hostname or unavailable host. A
fresh URL supplied by Owner should be checked before installing anything.
No Docker/Colima/Lima/Podman exists on the Mac; it is not an identified always-on
appliance. Installing software cannot establish the unknown physical device model
or unattended LAN availability. The cloud VPS is not an automatically suitable
Matter/local-discovery host.

| Actual model class | Supported preparation |
|---|---|
| S20 socket | Official HA `orvibo` supports S20; discovery needs same subnet. Official page also carries LGS-20 recall; identify actual model before commissioning. |
| Matter-capable MixPad D1 | Manufacturer confirms Matter on supported firmware; use native HA Matter commissioning for exposed endpoints. This does not expose every HomeMate device behind a hub. |
| Other Orvibo / Allone Pro / HomeMate hub | No blanket support demonstrated. Obtain exact model and firmware before choosing the HA integration; absence of an official generic integration is not proof all community paths are impossible. |

[Official Orvibo integration](https://www.home-assistant.io/integrations/orvibo/),
[HA Matter](https://www.home-assistant.io/integrations/matter/),
[manufacturer Matter FAQ](https://www.orvibo.com/en/support/supportFaqs.html).

**Smallest Owner input:** identify the actual Orvibo model and an existing HA
URL or designate the always-on LAN host for HA. This avoids buying/configuring
an unsuitable host or unsupported device. Prepared continuation: official HA OS
on that designated host; onboard the actual supported device; create a dedicated
HA access token in the Owner UI; inject `HASS_TOKEN`/existing `HASS_URL` only for
Hamad on the host that can reach HA. Confirm that the VPS has a legitimate private
route if Hamad operates there. [Supported installation](https://www.home-assistant.io/installation/).

First verification is read-only entity listing/state. Only after Owner names one
safe lamp/socket should a control journey run. Do not toggle locks, alarms,
sirens, covers, gates, garages or unnamed devices. Existing blocked execution
service domains remain unchanged. No direct Orvibo integration is justified.

## C6: real bounded availability journey

Used the actual installed Hamad maps script with landmark coordinates
`24.7113804,46.6743526`, 2 km radius, restaurant category, limit 20. It returned
20 records. OSM still contains Quattro/Planet Hollywood and an old Roma location;
current official pages supersede stale map tags. The prior short list was not
reused as authoritative.

| Current option | Confirmed constraint | Booking path |
|---|---|---|
| Café Boulud | Four Seasons Kingdom Centre; current dinner 18:00–23:30 | Official form, powered by OpenTable; real availability returned |
| Obaya Lounge | Same hotel; lounge option rather than identical French dinner | Official venue page; verify meal/time preference before committing |
| Roma Library branch | Official contact page maps it to `24.6862331,46.6864519`, outside the prior 2 km constraint | Official serVme widget currently says online reservations unavailable; not the old Resy assumption |

[Current Café Boulud](https://www.fourseasons.com/riyadh/dining/restaurants/cafe-boulud/),
[Obaya Lounge](https://www.fourseasons.com/riyadh/dining/lounges/obaya-lounge/),
[Roma contact/locations](https://www.romarestaurants.com/contact),
[officially linked Roma widget](https://widget.servmeco.com/?oid=1359).

Actual UI journey via Codex CUA, hidden in-app browser: Roma official page →
official widget → online reservations unavailable. Then Café Boulud official
page → Reserve a Table → Find a Table → availability modal → Find a Table →
visible AVAILABLE TABLES. Site defaults were **5 Sep 2026 / 2 people / 19:00**,
used solely as exploratory inputs. Results shown: 18:30, 18:45, 19:00, 19:15,
19:30; seating type Standard. Screenshot and accessibility output confirmed it.
No time selected, no contact data, no submit/hold/payment, no login.

This is a real vendor availability journey, **not** a Hermes-hosted C0 journey or
a confirmed reservation. We stop at actual Owner selection/commitment because
the program request gives no real desired venue/date/party/budget. Availability
is perishable and must be refreshed when that input arrives.

Hamad Google Workspace `setup.py --check` returned NOT_AUTHENTICATED on both
Mac and VPS. Both client-secret and token files are absent. Minimal OAuth scope
for this workflow is **calendar**, not inherited `email,calendar`. A connected
Codex Calendar or another profile's token would not prove Hamad Calendar.

C6 changes: allow read-only booking-page inspection before asking for selection;
clarify only missing information; distinguish research/availability/hold/confirmed
booking/calendar; require cancellation/deposit checks before commitment; narrow
Calendar scopes; route recurring price watches to `product-price-monitor`, while
one-off flight/hotel research remains a booking workflow. All related skills
resolve. Four skill tests cover format/routing, not booking E2E.

Dependencies are conditional: C0/C2 for hosted login; C1 only for actual native
Mac surfaces; C3 only when a call is needed; Calendar OAuth only for calendar
operations. No dependency should block public research. No booking backend or
second calendar store was added.

**Smallest Owner action:** choose a real venue/date/time/party size and budget
for the booking trial (or authorize a clear range), then provide Calendar OAuth
only if the confirmed booking should be added. The executor refreshes availability,
reviews actual terms, fills the real form and stops only at an uncovered login,
payment or commitment. REAL_BOOKING_E2E_ACCEPTED remains unearned until an actual
confirmation and required Calendar step are demonstrated.

## Validation / review / installation

- [Baseline native and skill tests: 78 pass](astra-capabilities-evidence/capability-baseline-tests.log).
- [Extended C2 native tests: 192 pass, 1 Windows-only skip](astra-capabilities-evidence/c2-native-tests.log).
- [Live native scope/HA/auth tests: 68 pass](astra-capabilities-evidence/c2-c4-live-tests.log).
- [Repaired skill tests: 16 pass](astra-capabilities-evidence/c3-c6-final-tests.log).
- Live health, inventories, LAN probes and vendor-browser journey described above.
- External Claude Code review returned PASS for the exact frozen candidate
  `1a15b6f4a41af5b42210b2334e74945de9c0964b`, with no material findings. It was
  static source/test inspection: Bash was unavailable to that reviewer. N0
  independently checked the frozen commit and retained the actual test runs
  above; the external verdict is not described as an independent test execution.
- After that review and N0 release coordination, three reviewed files were
  installed additively for Hamad on both Mac and VPS on 2026-09-05 KSA. Both
  skill directories were rechecked absent and reserved exclusively. Files were
  published with atomic no-clobber links, helper before SKILL.md; all final
  hashes match [the reviewed manifest](astra-capabilities-evidence/candidate-digests.json).
  Files are mode 0644 and owned by the profile user: Mac UID/GID 501/20,
  VPS 1000/1000. No config, credentials, repository product files or service
  restarts were part of this installation.
- Native discovery and skill_view succeeded for both skills using each host's
  installed Hermes runtime. The installed helper's diagnose succeeded and
  confirmed Wave key/from-number missing, provider_verified false and
  external_gate true; Twilio/Bland/Vapi configuration was also absent. This
  establishes installed preparation, not a live call or booking.
- [Mac deployment](astra-capabilities-evidence/local-deployment.json),
  [VPS deployment](astra-capabilities-evidence/vps-deployment.json),
  [Mac native verification](astra-capabilities-evidence/local-install-verification.json),
  [VPS native verification](astra-capabilities-evidence/vps-install-verification.json).
  Receipts identify the exact installed paths and rollback policy: remove only
  files still matching the reviewed digests, preserve changed paths, and remove
  only empty directories created by this installation. The executable rollback
  helper is retained in the supervisor workspace as work/deploy_capabilities.py.
