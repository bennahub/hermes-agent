"""Which model actually runs a turn, and why.

BWM-797 Scope 16. The owner's goal in one sentence: keep agents working when a
provider's included allowance runs out, without quietly spending money while the
other approved provider still has included capacity left.

**Almost none of this is new machinery, and that is deliberate.** Hermes already
has a structured failure classifier (`agent/error_classifier.py`), a
cross-provider fallback chain that is already *turn-scoped* and never rewrites
the profile's configured model (`try_activate_fallback` /
`restore_primary_runtime`), persisted per-credential cooldowns carrying the
provider's own `reset_at`, and live subscription-quota endpoints for both
families (`agent/account_usage.py`). Rebuilding any of that would have been the
expensive mistake.

Three things genuinely did not exist, and they are all this module is:

1. **Included-vs-paid classification.** `usage_pricing.resolve_billing_route`
   hardcodes `anthropic` as paid regardless of how the credential authenticates,
   so a Claude *subscription* token was indistinguishable from a pay-as-you-go
   key. It is also display-only and reaches no routing code.

2. **Preference ordering.** `_fallback_index` walks a fixed list. "Paid is a
   last resort" means ordering candidates by whether they spend included
   capacity — which is a property of the *credential*, not the provider.

3. A place to answer "what is actually running, and why", so the owner can see
   it without reading a log.

The configured/effective split the scope asks for already exists structurally:
the profile's `config.yaml` holds what the owner chose, the live agent holds
what this turn is using, and fallback is restored at the start of the next turn.
Nothing here writes the configured model — and `agent.switch_model` is
deliberately not used for automatic routing, because it updates
`_primary_runtime` *and prunes both providers out of the fallback chain*, which
would silently disable failover for the rest of that agent's life.

Approved families are OpenAI and Anthropic/Claude only, per the owner's
decision. A candidate from anywhere else is not ordered, not preferred, and not
introduced — it is simply left where the operator put it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional


#: The only families automatic routing may move an agent between.
APPROVED_FAMILIES: frozenset[str] = frozenset({"openai", "anthropic"})

#: `paid_usage_policy`. Not a boolean: refusing to spend at all would stop the
#: agent entirely once both included routes are gone, which is worse for the
#: owner than a small bill they can see.
PAID_LAST_RESORT = "last_resort"
PAID_ALLOWED = "allowed"

INCLUDED = "included"
PAID = "paid"
UNKNOWN = "unknown"


def provider_family(provider: Any) -> str:
    """The approved family a provider id belongs to, or ``""``.

    `openai-codex` and `openai-api` are one family with two billing shapes —
    which is exactly why the family cannot decide included-vs-paid on its own.
    """
    name = str(provider or "").strip().lower()
    if not name:
        return ""
    if name.startswith("openai"):
        return "openai"
    if name.startswith("anthropic") or name.startswith("claude"):
        return "anthropic"
    return ""


def classify_credential(provider: Any, entry: Optional[Mapping[str, Any]] = None) -> str:
    """Whether using this credential spends included capacity or money.

    Decided by **how the credential authenticates**, not by the provider id.
    That is the correction this module exists to make:

    * `openai-codex` is ChatGPT-subscription OAuth and has no API-key mode at
      all, so it is always included capacity.
    * `anthropic` is both, per pool entry: an OAuth token (`auth_type == "oauth"`,
      or the `sk-ant-oat…` shape `_normalize_pool_auth_type` already recognises,
      or a `hermes_pkce` / `claude_code` source) is a Claude subscription; an
      `sk-ant-api…` key is pay-as-you-go.
    * `openai-api` is an API key: paid.

    Anything unrecognised is `UNKNOWN` and is ordered *after* included routes
    but *before* known-paid ones — a guess in either direction would be worse
    than admitting the uncertainty.
    """
    name = str(provider or "").strip().lower()
    if name == "openai-codex":
        return INCLUDED
    if name == "openai-api":
        return PAID

    # Read by attribute *or* by key. The pool hands out `PooledCredential`
    # dataclasses, not dicts, and its `__getattr__` raises for any unknown name
    # — so a bare `record.get(...)` did not merely miss the fields, it raised
    # `AttributeError` and took the whole RPC down with it. Reading both shapes
    # keeps the one caller that passes a dict working too.
    def _field(name: str) -> str:
        if entry is None:
            return ""
        if isinstance(entry, Mapping):
            return str(entry.get(name) or "")
        return str(getattr(entry, name, "") or "")

    auth_type = _field("auth_type").strip().lower()
    source = _field("source").strip().lower()
    # `access_token` is the pool's own field name; `token` is what a plain dict
    # uses. Only `token` was read, so the `sk-ant-…` shape test could never fire
    # against a real pool entry — the branch existed and was unreachable.
    token = _field("access_token") or _field("token")

    if auth_type == "oauth":
        return INCLUDED
    if source in {"hermes_pkce", "claude_code"}:
        return INCLUDED
    if token.startswith("sk-ant-oat"):
        return INCLUDED
    if token.startswith("sk-ant-api"):
        return PAID
    if auth_type == "api_key":
        return PAID
    return UNKNOWN


@dataclass(frozen=True)
class Candidate:
    """One place a turn could run, with what it would cost."""

    provider: str
    model: str
    route_class: str = UNKNOWN
    #: True for the model the owner configured. Never reordered below a
    #: fallback of the same class — the owner's choice wins ties.
    is_primary: bool = False
    #: False while the credential is in cooldown, from the pool's own
    #: `next_available_at`. An unavailable candidate is not a preference
    #: question; it simply cannot run.
    available: bool = True

    @property
    def family(self) -> str:
        return provider_family(self.provider)


def _rank(candidate: Candidate) -> tuple[int, int, int]:
    """Sort key. Lower is preferred.

    The order the owner asked for, stated once:

      1. the configured primary, on included capacity
      2. any other included capacity — the cross-provider fallback
      3. anything we cannot classify
      4. paid, last

    Availability dominates all of it: a route in cooldown loses to one that can
    actually answer, whatever it would have cost.
    """
    cost = {INCLUDED: 0, UNKNOWN: 1, PAID: 2}.get(candidate.route_class, 1)
    return (0 if candidate.available else 1, cost, 0 if candidate.is_primary else 1)


def order_candidates(
    candidates: Iterable[Candidate], *, paid_policy: str = PAID_LAST_RESORT
) -> list[Candidate]:
    """Order routes by the policy. Pure — the whole point is that it is testable.

    Stable within a rank, so an operator's own chain order survives wherever the
    policy does not have an opinion.

    With ``paid_policy == PAID_ALLOWED`` the cost dimension is dropped and only
    availability and the owner's primary matter, which is the escape hatch for
    someone who would rather pay than wait.
    """
    ordered = list(candidates)
    if paid_policy == PAID_ALLOWED:
        return sorted(
            ordered, key=lambda c: (0 if c.available else 1, 0 if c.is_primary else 1)
        )
    return sorted(ordered, key=_rank)


def is_automatic_failover_allowed(primary: Candidate, candidate: Candidate) -> bool:
    """Whether routing may move a turn from *primary* to *candidate*.

    Only between the two approved families, and only to somewhere different —
    a "fallback" onto the same provider is the credential pool's job, not this
    module's.
    """
    if candidate.family not in APPROVED_FAMILIES:
        return False
    if primary.family and primary.family not in APPROVED_FAMILIES:
        return False
    return candidate.provider != primary.provider


def order_fallback_chain(
    chain: Iterable[Mapping[str, Any]],
    *,
    primary_provider: str = "",
    paid_policy: str = PAID_LAST_RESORT,
    classify: Any = None,
) -> list[dict[str, Any]]:
    """Reorder an agent's fallback chain so included capacity is tried first.

    `try_activate_fallback` walks `_fallback_chain` by index, so ordering the
    list *is* the routing policy — no new traversal, no second mechanism, and
    the operator's own entries are preserved rather than replaced.

    Entries outside the approved families keep their relative position at the
    end: the owner's decision limits *automatic* routing to OpenAI and Claude,
    and silently discarding an operator's third-party fallback would be a
    different and worse behaviour than declining to prefer it.

    `classify` is injectable so the policy can be tested without a credential
    pool on disk.
    """
    classifier = classify or (lambda provider: classify_credential(provider))
    approved: list[tuple[Candidate, dict[str, Any]]] = []
    others: list[dict[str, Any]] = []

    for raw in chain:
        entry = dict(raw)
        provider = str(entry.get("provider") or "")
        if provider_family(provider) not in APPROVED_FAMILIES:
            others.append(entry)
            continue
        approved.append((
            Candidate(
                provider=provider,
                model=str(entry.get("model") or ""),
                route_class=classifier(provider),
                is_primary=bool(primary_provider) and provider == primary_provider,
            ),
            entry,
        ))

    ordered = order_candidates((c for c, _ in approved), paid_policy=paid_policy)
    # Match each ordered candidate back to its entry by identity, so an entry
    # carrying extra keys (base_url, api_mode, key_env) survives intact.
    remaining = list(approved)
    out: list[dict[str, Any]] = []
    for candidate in ordered:
        for index, (other, entry) in enumerate(remaining):
            if other is candidate:
                out.append(entry)
                remaining.pop(index)
                break
    return out + [entry for _, entry in remaining] + others


@dataclass(frozen=True)
class RoutingState:
    """What the owner is shown: what they chose, what is running, and why.

    Deliberately a projection rather than a store. The configured model is the
    profile's `config.yaml`; the effective model is the live agent. Persisting a
    third copy would be the "second model configuration system" the scope
    forbids, and it would be the copy that goes stale.
    """

    configured_provider: str
    configured_model: str
    effective_provider: str
    effective_model: str
    route_class: str = UNKNOWN
    reason: str = ""

    @property
    def is_fallback_active(self) -> bool:
        return (
            self.effective_provider != self.configured_provider
            or self.effective_model != self.configured_model
        )

    @property
    def is_paid(self) -> bool:
        return self.route_class == PAID

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": {
                "provider": self.configured_provider,
                "model": self.configured_model,
            },
            "effective": {
                "provider": self.effective_provider,
                "model": self.effective_model,
            },
            "route_class": self.route_class,
            "fallback_active": self.is_fallback_active,
            "paid": self.is_paid,
            "reason": self.reason,
        }


#: Why a turn is not on its configured model. Owner-facing sentences, not error
#: codes: "OpenAI included quota unavailable" is something the owner can act on,
#: `usage_limit_reached` is not.
REASONS = {
    "quota": "Included quota unavailable",
    "rate_limit": "Provider was rate limiting",
    "auth": "Credential was refused",
    "model_not_found": "Model unavailable",
    "unavailable": "Provider unavailable",
}


def reason_for(failover_reason: Any) -> str:
    """Turn a `FailoverReason` into something worth showing the owner."""
    name = getattr(failover_reason, "value", None) or str(failover_reason or "")
    name = name.strip().lower()
    if name in {"billing", "usage_limit_reached"}:
        return REASONS["quota"]
    if name in {"rate_limit", "upstream_rate_limit"}:
        return REASONS["rate_limit"]
    if name.startswith("auth"):
        return REASONS["auth"]
    if name == "model_not_found":
        return REASONS["model_not_found"]
    if name in {"overloaded", "server_error", "timeout"}:
        return REASONS["unavailable"]
    return ""
