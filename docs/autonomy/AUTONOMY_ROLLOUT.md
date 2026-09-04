# AUTONOMY_ROLLOUT

Live roster on `srv1945447` after `hermes -p <slug> autonomy enable` (2026-09-04).

| Profile | Display | Domains | Observe job | Initiative |
|---|---|---|---|---|
| abu-saud | Abu Saud | coordination | d86892863c69 | enabled |
| badr | Badr | engineering | 8dfc69b92351 | enabled |
| sami | Sami | operations | 39e1a05703e9 | enabled |
| nasser | Nasser | erp | 9b4ba7cd6995 | enabled |
| abu-saleh | Abu Saleh | coordination | e8b4c45f9390 | enabled |
| fahad | Fahad | research | 568de2fe954f | enabled |
| faisal | Faisal | finance | 05d9e1b274f8 | enabled |
| fares | Fares | sales | 018f45fe5037 | enabled |
| hamad | Hamad | personal | 2c68e858dcb3 | enabled (isolated A2A) |
| joud | Joud | growth | ad44c1cc1fd5 | enabled |
| majed | Majed | coordination | c0356750d4e8 | enabled |
| mishari | Mishari | endpoint_it | 41f85f7c1fc9 | enabled |
| nawaf | Nawaf | ir | 94225e2686ee | enabled |
| rashid | Rashid | knowledge | 543d56fd92e7 | enabled |
| turki | Turki | finance | 3fdd2db55da5 | enabled |

`default` is not owner-facing and was not enabled.

Observation jobs that would inherit Codex are pinned to `provider=anthropic`. Badr uses the profile default (`anthropic` / `claude-opus-5`).

Event domains: existing Badr GitHub desk + Sami Sentry routes now run `autonomy_webhook_filter.py` (payload pass-through; duplicates `[SILENT]`).
