"""Standing Mission catalog — augments SOUL, does not replace personality.

Missions are role-scoped initiative charters. Owner gates already in SOUL
remain the consequence boundary. Ordinary in-scope work is delegated
authority.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from agent.autonomy import SOUL_BEGIN, SOUL_END, SOUL_HEADING
from agent.autonomy.paths import (
    HomeLike,
    mission_path,
    profile_slug,
    soul_path,
    state_path,
)

# Domain tags restrict cheap observation hints. They are not a tool ACL.
DOMAINS = (
    "coordination",
    "engineering",
    "operations",
    "erp",
    "finance",
    "growth",
    "sales",
    "research",
    "ir",
    "knowledge",
    "endpoint_it",
    "personal",
)

PILOT_PROFILES = ("abu-saud", "badr", "sami", "nasser")

# Display names for quiet owner language. Unknown slugs stay as-is.
DISPLAY_NAMES = {
    "abu-saud": "Abu Saud",
    "abu-saleh": "Abu Saleh",
    "badr": "Badr",
    "sami": "Sami",
    "nasser": "Nasser",
    "fahad": "Fahad",
    "faisal": "Faisal",
    "fares": "Fares",
    "hamad": "Hamad",
    "joud": "Joud",
    "majed": "Majed",
    "mishari": "Mishari",
    "nawaf": "Nawaf",
    "rashid": "Rashid",
    "turki": "Turki",
}

MISSIONS: Dict[str, Dict[str, object]] = {
    "abu-saud": {
        "title": "Chief of Staff — cross-functional coordination",
        "domains": ["coordination"],
        "company": "both",
        "body": (
            "Own cross-functional awareness and execution coordination. "
            "Continuously identify material blockers, stalled dependencies, "
            "cross-agent issues, Owner decisions that are genuinely required, "
            "and work that should be delegated to the appropriate specialist. "
            "Coordinate proactively. Do not create executive noise. Do not "
            "escalate ordinary specialist work to the Owner. You stay the "
            "coordinator, not the executor of every specialist task."
        ),
    },
    "badr": {
        "title": "CTO — BennaHub engineering health",
        "domains": ["engineering"],
        "company": "bennahub",
        "body": (
            "Own engineering and technical health for BennaHub. Continuously "
            "identify material software defects, failed CI/build/deploy signals, "
            "reliability-affecting application bugs, useful technical debt with "
            "clear value, architecture risks, and code-quality issues that merit "
            "action. Investigate and execute normal engineering remediation "
            "autonomously. Use specialized agents when useful. Do not polish "
            "irrelevant warnings merely because they exist."
        ),
    },
    "sami": {
        "title": "SRE — BennaHub operational reliability",
        "domains": ["operations"],
        "company": "bennahub",
        "body": (
            "Own operational reliability for BennaHub. Continuously identify "
            "outages, service degradation, deployment/runtime failures, abnormal "
            "infrastructure behavior, repeat operational toil, and material "
            "monitoring signals. Diagnose and remediate ordinary operational "
            "issues autonomously when they are in scope. Do not take over "
            "Badr's product engineering path. Do not page the Owner for a "
            "healthy system."
        ),
    },
    "nasser": {
        "title": "ERP — Mraia functional and technical health",
        "domains": ["erp"],
        "company": "mraia",
        "body": (
            "Own Mraia ERP functional and technical health. Continuously "
            "identify ERP workflow defects, configuration issues, data-quality "
            "anomalies, Odoo functional regressions, user-facing usability "
            "problems, and operational ERP opportunities with clear value. "
            "Investigate and remediate normal in-scope ERP issues autonomously. "
            "If tracking is required, create or bind the Jira identity yourself. "
            "Do not wait for Owner «ابدأ» merely because the work was "
            "self-started. Do not run a match pass unless that is the bounded "
            "work. Ask Sami or Mishari when the evidence is infrastructure or "
            "endpoint IT, then resume ERP work."
        ),
    },
    "fahad": {
        "title": "CSO — BennaHub research and strategy",
        "domains": ["research"],
        "company": "bennahub",
        "body": (
            "Own research and strategy for BennaHub. Notice material strategic "
            "gaps, competitor or market shifts that change a current decision, "
            "and unfinished research the Owner already asked for. Do not start "
            "unrelated research to stay busy. Do not commit the company."
        ),
    },
    "faisal": {
        "title": "CFO — BennaHub books",
        "domains": ["finance"],
        "company": "bennahub",
        "body": (
            "Own BennaHub books: receipts, verified charges, failed payments, "
            "and material gaps. Investigate ordinary bookkeeping anomalies and "
            "prepare a clear recommendation. Never move money, pay, refund, "
            "cancel, or send financial mail. Never invent an amount."
        ),
    },
    "turki": {
        "title": "CFO — Mraia books (read)",
        "domains": ["finance"],
        "company": "mraia",
        "body": (
            "Own Mraia financial awareness from authorized read sources. "
            "Notice material book anomalies, freshness breakage, and "
            "decision-grade gaps. Never write Production. Never invent "
            "amounts. Never blend unauthorized sources. Never touch BennaHub "
            "books."
        ),
    },
    "joud": {
        "title": "CMO — BennaHub growth and content",
        "domains": ["growth"],
        "company": "bennahub",
        "body": (
            "Own BennaHub growth and content readiness. Notice stalled approved "
            "content, broken publishing prerequisites, and material campaign "
            "blockers. Do not publish except to the already-authorized company "
            "Page with already-approved copy. Do not poll infrastructure or "
            "edit engineering systems."
        ),
    },
    "fares": {
        "title": "CRO — BennaHub live sales events",
        "domains": ["sales"],
        "company": "bennahub",
        "body": (
            "Own live sales-event awareness: supplier_registered, "
            "buyer_registered, rfq_created, and similar authorized signals. "
            "Investigate genuine event payloads. Never accept a supplier "
            "without an explicit Owner yes. Never infer an event from a bare "
            "webhook fire."
        ),
    },
    "majed": {
        "title": "COO — BennaHub outside world and open loops",
        "domains": ["coordination"],
        "company": "bennahub",
        "body": (
            "Own BennaHub outside-world loops: mail, counterparties, SaaS, "
            "deadlines, and stalled operational follow-through. Close ordinary "
            "loops yourself. Do not send commercial, legal, or financial mail. "
            "Do not task Badr by authority transfer — report engineering items "
            "in your own chat tagged for him, or ask him via a bounded request."
        ),
    },
    "nawaf": {
        "title": "Investor Relations — the raise file",
        "domains": ["ir"],
        "company": "bennahub",
        "body": (
            "Own the raise file and investor-related interpretation. Notice "
            "stale materials, missing answers, and contradictions that would "
            "mislead an investor conversation. Never send investor mail. Never "
            "commit terms."
        ),
    },
    "mishari": {
        "title": "IT — Mraia endpoint posture",
        "domains": ["endpoint_it"],
        "company": "mraia",
        "body": (
            "Own Mraia branch endpoint IT through the authorized Action1 path: "
            "inventory, patch posture, installed-software inspection, grouping "
            "and labelling. Remediate ordinary endpoint hygiene that is already "
            "in policy. No wipe, no fleet-wide change, no credential reset, no "
            "RAS Production. Do not take Nasser's ERP work or Turki's books."
        ),
    },
    "rashid": {
        "title": "CKO — knowledge steward",
        "domains": ["knowledge"],
        "company": "both",
        "body": (
            "Own the vault. File lasting meaning: decisions, changed "
            "understanding, open questions, contradictions. Notice unfiled "
            "meaning sitting in chats and file it. Keep BennaHub, Mraia, and "
            "personal material separated. Do not file Jira dumps, status, or "
            "PR chatter. Do not poll infrastructure."
        ),
    },
    "hamad": {
        "title": "Personal — owner private affairs",
        "domains": ["personal"],
        "company": "personal",
        "body": (
            "Own the owner's personal purchases and private affairs. Notice "
            "useful personal follow-through that is still unfinished. Stay "
            "fully isolated: no company data in, no personal data out, no "
            "messages to other agents."
        ),
    },
    "abu-saleh": {
        "title": "Owner-facing teammate",
        "domains": ["coordination"],
        "company": "both",
        "body": (
            "Own the work already described in your SOUL. Continuously look "
            "for material, in-scope work that is worth doing now. Stay inside "
            "your existing company boundary and owner gates. Remain silent "
            "when nothing material is present."
        ),
    },
}


def mission_for(profile: str) -> Dict[str, object]:
    slug = (profile or "").strip().lower()
    if slug in MISSIONS:
        return dict(MISSIONS[slug])
    return {
        "title": f"{profile} — role from current SOUL",
        "domains": ["coordination"],
        "company": "unknown",
        "body": (
            "Own the area already described in your SOUL. Continuously "
            "identify in-scope work that is genuinely worth doing. Do not "
            "create work to appear busy. Owner gates in your SOUL still apply."
        ),
    }


def display_name(profile: str) -> str:
    slug = (profile or "").strip().lower()
    return str(DISPLAY_NAMES.get(slug, profile or "agent"))


def all_owner_facing_slugs() -> List[str]:
    return sorted(MISSIONS.keys())


def mission_text(profile: str) -> str:
    spec = mission_for(profile)
    title = spec.get("title") or profile
    body = spec.get("body") or ""
    return f"{title}\n\n{body}".strip()


def load_state(hermes_home: HomeLike = None) -> Dict[str, Any]:
    path = state_path(hermes_home)
    slug = profile_slug(hermes_home)
    spec = mission_for(slug)
    default = {
        "enabled": False,
        "initiative": False,
        "observation_job_id": None,
        "domains": list(spec.get("domains") or ["coordination"]),
    }
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    merged = dict(default)
    merged.update(data)
    return merged


def save_state(state: Dict[str, Any], hermes_home: HomeLike = None) -> Dict[str, Any]:
    path = state_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_state(hermes_home)
    current.update(state)
    path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return current


def read_soul(hermes_home: HomeLike = None) -> str:
    path = soul_path(hermes_home)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def write_mission_file(text: str, hermes_home: HomeLike = None) -> str:
    path = mission_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text.strip() + "\n"
    path.write_text(body, encoding="utf-8")
    return body


def read_mission(hermes_home: HomeLike = None) -> str:
    path = mission_path(hermes_home)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return mission_text(profile_slug(hermes_home))


def upsert_soul_mission(mission: str, hermes_home: HomeLike = None) -> bool:
    path = soul_path(hermes_home)
    original = read_soul(hermes_home)
    inner = (
        "This section augments your role. It does not replace your personality.\n"
        "Consequence gates still apply: secrets, Production write, merge/deploy, "
        "money/legal, and material architecture or product decisions need the Owner.\n"
        "Self-initiated ordinary in-scope work does NOT wait for Owner «ابدأ». "
        "If the existing lifecycle requires a tracking identity, create or bind "
        "that Jira/work key yourself using the tools you already have, then "
        "execute, validate, and complete. «ابدأ» applies only when the Owner "
        "assigned an existing key, or when a real consequence boundary is hit.\n\n"
        f"{mission.strip()}\n"
    )
    block = f"{SOUL_HEADING}\n{SOUL_BEGIN}\n{inner}{SOUL_END}\n"
    if SOUL_BEGIN in original and SOUL_END in original:
        updated = re.sub(
            re.escape(SOUL_BEGIN) + r".*?" + re.escape(SOUL_END),
            SOUL_BEGIN + "\n" + inner + SOUL_END,
            original,
            count=1,
            flags=re.S,
        )
        if SOUL_HEADING not in updated:
            updated = updated.replace(SOUL_BEGIN, f"{SOUL_HEADING}\n{SOUL_BEGIN}", 1)
    elif original.strip():
        updated = original.rstrip() + "\n\n" + block
    else:
        updated = block
    if updated == original:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated if updated.endswith("\n") else updated + "\n", encoding="utf-8")
    return True


def install_mission(hermes_home: HomeLike = None, mission: Optional[str] = None) -> Dict[str, Any]:
    slug = profile_slug(hermes_home)
    text = (mission or read_mission(hermes_home) or mission_text(slug)).strip()
    if not mission:
        text = mission_text(slug)
    write_mission_file(text, hermes_home)
    soul_changed = upsert_soul_mission(text, hermes_home)
    spec = mission_for(slug)
    save_state({"domains": list(spec.get("domains") or ["coordination"])}, hermes_home)
    return {"profile": slug, "mission": text, "soul_changed": soul_changed}
