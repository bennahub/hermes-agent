# BWM-796 — Hosted resource capacity

Host: `srv1945447` — 2 CPU, 7.8 GiB RAM, 4 GiB swap, 96G disk (~29G used at
activation). Measurements from `docs/bwm-796/hosted_acceptance.py`
`resource_snapshot()` on 2026-09-04.

`chrome_rss_kb` sums `ps` RSS for every chrome/chromium thread. That **overcounts**
shared mappings. `free` `mem_used` / `mem_avail` is the better host-level signal.

## Baseline

| Signal | Value |
|---|---|
| Host RAM | 7.8 GiB |
| After sleep (no AgentComputer Chrome) | mem used 1.50 GiB, avail 6.82 GiB, chrome RSS 0 |
| Idle Hermes | `hermes-serve` + `hermes-gateway` only |
| Disk | 96G, ~68G free |
| Profile store after UAT | ~76 MiB under `agent-computers` (many synthetic identities) |

A “1_active” snapshot taken while leftover Chrome from a prior run still existed
is not a clean idle baseline. Use the post-sleep row above.

## Per active Chromium workspace

| Active | Wake ms | chrome_rss_kb (sum) | mem_used | mem_avail |
|---|---:|---:|---:|---:|
| 1 | 694 | 1 060 408 (~1.01 GiB) | 1.70 GiB | 6.63 GiB |
| 2 | 577 | 2 527 140 | 1.89 GiB | 6.44 GiB |
| 3 | 590 | 3 759 548 | 2.07 GiB | 6.26 GiB |
| 4 | 560 | 4 900 004 | 2.23 GiB | 6.09 GiB |

Incremental host `mem_used` ≈ 0.18–0.20 GiB per extra workspace after the first
(shared mappings). RSS-sum ≈ 1.2 GiB per extra workspace and should not be
treated as unique private memory.

Startup / wake latency stayed 0.45–0.70 s on this host.

## Concurrency

Tested 1 / 2 / 4 simultaneous active Chromiums. VPS remained healthy (avail RAM
≥ 6 GiB). Did **not** launch 14 Chromiums.

## Safe recommendation

Architecture: **14 durable identities, bounded active compute.**

| Recommendation | Rationale |
|---|---|
| **2 active** | Comfortable default on this 8 GiB host next to serve + gateway |
| **4 active** | Proven; leave headroom for gateway / chat / other profiles |
| **14 always-on** | Rejected. Would exhaust the VPS if RSS-sum were unique |

## Idle / sleep

| Check | Result |
|---|---|
| `POST .../sleep` | Chromium processes gone (`chrome_rss_kb=0`) |
| Durable computer / identity | Unchanged |
| Wake after sleep + `hermes-serve` restart | 459 ms; same `ac_*` + `bi_*`; `ready cookie persist` |

Chromium may stop. Identity and workspace files remain.
