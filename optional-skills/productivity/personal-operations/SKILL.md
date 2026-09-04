---
name: personal-operations
description: Book personal appointments using hosted or native surfaces.
version: 1.0.0
author: Abdulrahman Almuzaini + Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [personal, booking, calendar, hamad, operations]
    related_skills: [telephony, computer-use, google-workspace, maps, product-price-monitor]
    category: productivity
    config:
      isolated_profile: hamad
---

# Personal Operations Skill

Routes Hamad personal bookings and errands through existing Hermes
surfaces. It does not implement a booking backend, wallet, or login
system.

Use this skill only on the isolated **Hamad** profile. Company agents
must not run personal bookings.

## When to Use

- Restaurant, clinic, salon, or similar personal appointment
- The task needs a website, native Mac app, calendar, maps, or a phone call
- Owner asked Hamad to research options, then hold a reversible slot

## When NOT to Use

- Company work, or any profile other than Hamad
- Recurring price/availability alerts (use `product-price-monitor`)
- Autonomous payment, contract acceptance, or irreversible submission
- Pure geocoding / nearby search with no booking (use `maps`)
- Home Assistant / Orvibo (C4, not a booking path)

## Prerequisites

- Hamad profile (`hermes -p hamad`). Kernel A2A to/from Hamad is blocked.
- Calendar via `google-workspace` when the event is on Owner calendar;
  use `--services calendar` for this workflow, without requesting email access.
- Hosted browser: BWM-796 AgentComputer + dedicated BrowserIdentity (C0).
- Native Mac apps: `computer_use` / cua-driver on the Owner Mac (C1).
- Phone: `telephony` skill. Saudi +966 needs a verified production account,
  an owned number, and a configured conversational flow if talking is required.
  A Wave API key or an initiated call does not prove a booking conversation.
- Home Assistant is independent and is not a booking path.

## How to Run

```bash
hermes -p hamad skills install official/productivity/personal-operations
```

There is no helper backend. Drive the chosen surface with native tools.

## Quick Reference

| Need | Surface |
|---|---|
| Nearby options / travel time | `maps` skill, then shortlist |
| Recurring price / availability watch | `product-price-monitor` |
| One-off flight/hotel research | Search and the provider's current booking surface |
| API or calendar tool exists | Use that tool. Do not open a GUI. |
| Website that can run hosted | C0 hosted Computer + BrowserIdentity |
| Finder, Mail, Xcode, System Settings | C1 native `computer_use` |
| Human login / 2FA / CAPTCHA | C0 Human Takeover, then Give Back |
| Phone call or SMS | `telephony` (`wave-call` dry-run until KYC) |
| Payment / legal submit | Stop. Consequence boundary. |

## Procedure

### 1. Confirm isolation

Stay on Hamad. Do not delegate to company agents. Do not copy company
data in or personal data out. Done when the session profile is Hamad.

### 2. Research, then shortlist

If the venue is unknown, use `maps` / `web_search` first. Confirm map results
against current official venue pages; a stale map listing is not a valid
shortlist. Return 2–3 options with hours, location and a constraint (distance,
cuisine, time). You may inspect booking pages and availability without a
selection. Ask with `clarify` only for missing details that prevent a booking:
venue, date, time, party size, preferences or budget. Use details already given
and state any exploratory assumptions. Do not treat an assumed date or party
size as authorization to reserve.

### 3. Choose the simplest surface

Prefer API/calendar, then C0 hosted browser, then C1 Mac, then phone.
Do not invent a second browser or password store. Done when one surface
is selected and the others are left unused.

### 4. Prepare, then pause only for a human checkpoint

Navigate to the booking page or native app yourself. If the site asks
for login, 2FA, passkey, or CAPTCHA, pause for Owner Take Control on
authenticated `/computer` (C0) or stop and ask (C1). Do not type stored
Mac passwords. Done when the page is ready or Owner has the exact gate.

### 5. Complete only the reversible booking step

Fill the form using authorized details. Inspect cancellation, deposit and
no-show terms before submitting. A free reversible reservation may be completed
when the Owner's booking instruction covers this venue and slot; research-only
instructions do not authorize one. Honor existing authorization without asking
again. Stop at any uncovered payment, contract or irreversible commitment.
Write to the Owner calendar only after a real confirmed slot, using Hamad's
authorized calendar with an explicit timezone. Calendar OAuth blocks the calendar
write, not research or inspecting availability.

### 6. Stop at consequence

If the next click spends money, signs a contract, or cannot be undone,
stop and return control to the Owner. Done when no unauthorized
external action has been taken.

## Pitfalls

- C0 and C1 are different capabilities. Do not rebuild either.
- Do not import the Owner daily Mac Chrome profile into a hosted
  BrowserIdentity.
- A Linux hosted browser may be refused by Apple/Google. Classify that
  honestly; do not weaken BrowserIdentity.
- `wave-call` is dry-run by default. It starts the account's configured flow;
  it does not accept a booking task or prove conversation completion.
- Do not label research, availability, request submitted, waitlist, provisional
  hold, confirmed booking and Calendar write as the same outcome. Record the
  actual result and provider confirmation reference; never invent a booking.
- Home Assistant / Orvibo is not a booking path.

## Verification

1. Session is Hamad.
2. One existing surface was used (API, C0, C1, or telephony).
3. No new booking backend or wallet was created.
4. Owner handled any login/2FA/CAPTCHA.
5. Payment/legal submit did not happen unless Owner did it.
