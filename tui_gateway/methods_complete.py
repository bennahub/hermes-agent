"""Completion / model-key / paste JSON-RPC handlers.

Rebound onto server.py's globals at install time (``method_ctx.bind_module``), so
bodies reference server globals bare (``_ok``, ``_err``, ``_sessions``, ...).
"""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

_BUILTIN_AT_PREFIXES = frozenset({"file", "folder", "url", "git", "diff", "staged"})
_AT_DIRECTIVE_HINTS = [
    ("@diff", "git diff"), ("@staged", "staged diff"), ("@file:", "attach file"),
    ("@folder:", "attach folder"), ("@url:", "fetch url"), ("@git:", "git log")]
_SLASH_EXTRAS = [
    ("/density", "Toggle compact display mode"), ("/details", "Control agent detail visibility"),
    ("/logs", "Show recent gateway log lines"),
    ("/mouse", "Set mouse tracking preset [on|off|toggle|wheel|buttons|all]")]


def _item(text: str, meta: str, display: str | None = None) -> dict:
    return {"text": text, "display": display if display is not None else text, "meta": meta}


def _catch(fail_code: int):
    """Handler body exceptions → ``_err(rid, fail_code, str(e))``."""

    def deco(body):
        def handler(rid, params: dict) -> dict:
            try:
                return body(rid, params)
            except Exception as e:
                return _err(rid, fail_code, str(e))
        handler.__doc__ = body.__doc__
        return handler
    return deco


@method("paste.collapse")
def _(rid, params: dict) -> dict:
    global _paste_counter
    text = params.get("text", "")
    if not text:
        return _err(rid, 4004, "empty paste")
    _paste_counter += 1
    line_count = text.count("\n") + 1
    paste_dir = _hermes_home / "pastes"
    paste_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    paste_file = paste_dir / f"paste_{_paste_counter}_{datetime.now().strftime('%H%M%S')}.txt"
    paste_file.write_text(text, encoding="utf-8")
    placeholder = f"[Pasted text #{_paste_counter}: {line_count} lines \u2192 {paste_file}]"
    return _ok(rid, {"placeholder": placeholder, "path": str(paste_file), "lines": line_count})


def _profile_mention_items(prefix: str) -> list[dict]:
    """`@<profile>` completions (multi-agent UIs route `@<profile>` text to another
    profile). Bare-word matches only, never `@kind:` directives; the primary profile
    is also offered as 'hermes' when no real profile claims that name."""
    out: list[dict] = []
    try:
        from hermes_cli.profiles import list_profiles
        seen: set[str] = set()
        for p in list_profiles():
            if not (name := (p.name or "").strip()):
                continue
            seen.add(name.lower())
            if name.lower().startswith(prefix.lower()):
                out.append(_item(f"@{name}", (getattr(p, "description", "") or "").strip() or "agent profile"))
        if "hermes".startswith(prefix.lower()) and "hermes" not in seen:
            out.append(_item("@hermes", "agent profile (primary)"))
    except Exception:
        return []
    return out


def _plugin_reference_items(pfx: str, qval: str) -> list[dict] | None:
    """`@<prefix>:<query>` autocomplete for a plugin ContextReferenceProvider; None when
    no provider owns ``pfx`` or it fails."""
    try:
        from agent.context_references import get_context_reference_providers
        import asyncio
        if (prov := get_context_reference_providers().get(pfx)) is None:
            return None
        coro = prov.autocomplete(qval, limit=20)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            ac = asyncio.run(coro)
        else:  # already inside a running loop: run the coroutine on a side thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                ac = pool.submit(asyncio.run, coro).result()
        return [{"text": f"@{pfx}:{it.text}", "display": it.display, "meta": it.meta} for it in ac]
    except Exception:
        return None


def _fuzzy_basename_items(root: str, path_part: str, prefix_tag: str) -> list[dict]:
    """Cmd-P style fuzzy basename search for a bare `@name`; path-ish queries take the listing path."""
    ranked: list[tuple[tuple[int, int], str, str, bool]] = []
    walked_dirs: set[str] = set()
    seen: set[str] = set()
    want_hidden = path_part.startswith(".")

    def _consider(rel: str, name: str, is_dir: bool) -> None:
        if rel in seen or (name.startswith(".") and not want_hidden):
            return
        if (rank := _fuzzy_basename_rank(name, path_part)) is not None:
            seen.add(rel)
            ranked.append((rank, rel, name, is_dir))

    # Seed with root's immediate children: `_list_repo_files` is capped at _FUZZY_CACHE_MAX_FILES
    # and the non-git fallback walk can burn the whole budget on one deep subtree.
    with contextlib.suppress(OSError):
        for entry in os.listdir(root):
            if entry not in _FUZZY_FALLBACK_EXCLUDES:
                _consider(entry, entry, os.path.isdir(os.path.join(root, entry)))
    for rel in _list_repo_files(root):
        _consider(rel, os.path.basename(rel), False)
        # Rank each ancestor dir too — a folder with no name-matching file inside is otherwise invisible.
        parent = os.path.dirname(rel)
        while parent and parent not in walked_dirs:
            walked_dirs.add(parent)
            _consider(parent, os.path.basename(parent), True)
            parent = os.path.dirname(parent)

    # Same rank tier: folders first, so `@Desktop` leads with the folder.
    ranked.sort(key=lambda r: (r[0], not r[3], len(r[1]), r[1]))
    tag = prefix_tag or "file"
    return [
        _item(
            f"@{'folder' if is_dir else tag}:{rel}{'/' if is_dir else ''}",
            "dir" if is_dir else os.path.dirname(rel), basename + ("/" if is_dir else ""))
        for _, rel, basename, is_dir in ranked[:30]]


def _at_root_items() -> list[dict]:
    """Completions for a bare ``@``: directive hints, agent profiles, plugin ``@<prefix>:`` providers."""
    items = [_item(t, m) for t, m in _AT_DIRECTIVE_HINTS] + _profile_mention_items("")
    with contextlib.suppress(Exception):
        from agent.context_references import get_context_reference_providers
        for pfx, prov in sorted(get_context_reference_providers().items()):
            items.append(_item(f"@{pfx}:", prov.description or f"plugin: {pfx}"))
    return items


def _dir_listing_items(root: str, word: str, path_part: str, prefix_tag: str, is_context: bool) -> list[dict]:
    """Prefix-match entries of the directory ``path_part`` points at (max 30)."""
    expanded = _normalize_completion_path(path_part) if path_part else "."
    if expanded == "." or not expanded or expanded.endswith("/"):
        search_dir, match = (expanded or "."), ""
    else:
        search_dir, match = os.path.dirname(expanded) or ".", os.path.basename(expanded)
    search_dir = search_dir if os.path.isabs(search_dir) else os.path.join(root, search_dir)
    items: list[dict] = []
    if not os.path.isdir(search_dir):
        return items
    for entry in sorted(os.listdir(search_dir)):
        if match and not entry.lower().startswith(match.lower()):
            continue
        if is_context and (entry in _FUZZY_FALLBACK_EXCLUDES or (not prefix_tag and entry.startswith("."))):
            continue
        full = os.path.join(search_dir, entry)
        is_dir = os.path.isdir(full)
        if prefix_tag and (prefix_tag == "folder") != is_dir:  # explicit `@folder:`/`@file:` skip the other kind
            continue
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        suffix = "/" if is_dir else ""
        if is_context:
            text = f"@{prefix_tag or ('folder' if is_dir else 'file')}:{rel}{suffix}"
        elif word.startswith("~"):
            text = "~/" + os.path.relpath(full, os.path.expanduser("~")) + suffix
        else:
            text = ("./" if word.startswith("./") else "") + rel + suffix
        items.append(_item(text, "dir" if is_dir else "", entry + suffix))
        if len(items) >= 30:
            break
    return items


@method("complete.path")
@_catch(5021)
def _(rid, params: dict) -> dict:
    word = params.get("word", "")
    if not word:
        return _ok(rid, {"items": []})
    root = _completion_cwd(params)
    is_context = word.startswith("@")
    query = word[1:] if is_context else word
    if is_context and not query:
        return _ok(rid, {"items": _at_root_items()})
    # Plugin `@<prefix>:<query>` runs before the built-in file/folder branching.
    if is_context and ":" in query:
        pfx, _, qval = query.partition(":")
        if pfx not in _BUILTIN_AT_PREFIXES and (plugin_items := _plugin_reference_items(pfx, qval)) is not None:
            return _ok(rid, {"items": plugin_items})
    # Bare `@folder` lists as soon as the keyword is typed (the static `@folder:` hint is not accepted).
    if is_context and (query in {"file", "folder"} or query.startswith(("file:", "folder:"))):
        prefix_tag, _, path_part = query.partition(":")
    else:
        prefix_tag, path_part = "", query
    # `@/foo` usually means "foo, from here": absolute only when that prefix exists,
    # else resolve relative to cwd (`@/Desktop` must not dead-end; `@/usr/local` still resolves).
    if (
        is_context and path_part.startswith("/") and not path_part.startswith("//")
        and not _abs_completion_prefix_exists(path_part)):
        path_part = path_part.lstrip("/")
    bare_word = is_context and path_part and "/" not in path_part
    if bare_word and len(path_part.strip()) >= 2 and prefix_tag != "folder":
        items = _fuzzy_basename_items(root, path_part, prefix_tag)
    else:
        items = _dir_listing_items(root, word, path_part, prefix_tag, is_context)
    # Bare-word `@name` may be an agent mention: profiles rank ABOVE file hits.
    if bare_word and not prefix_tag:
        with contextlib.suppress(Exception):
            items = _profile_mention_items(path_part) + items
    return _ok(rid, {"items": items})


@method("complete.slash")
@_catch(5020)
def _(rid, params: dict) -> dict:
    text = params.get("text", "")
    if not text.startswith("/"):
        return _ok(rid, {"items": []})
    from hermes_cli.commands_completion import SlashCommandCompleter
    from prompt_toolkit.document import Document
    from prompt_toolkit.formatted_text import to_plain_text
    from agent.skill_commands import get_skill_commands
    from agent.skill_bundles import get_skill_bundles
    completer = SlashCommandCompleter(
        skill_commands_provider=lambda: get_skill_commands(), skill_bundles_provider=lambda: get_skill_bundles())
    # `kind` reaches the TUI as data (from the providers, not sniffed from ⚡/▣ glyphs):
    # skills/bundles are the only completions for an inline `/skill` typed mid-message.
    skill_names = {key.lstrip("/").lower() for key in (*get_skill_commands(), *get_skill_bundles())}

    def to_items(doc: Document) -> list[dict]:
        # display/display_meta are FormattedText; the TUI contract is a plain string
        # (the raw list trips Ink's row layout into 1-char truncation).
        return [
            {
                "text": c.text, "display": to_plain_text(c.display) if c.display else c.text,
                "meta": to_plain_text(c.display_meta) if c.display_meta else "",
                "kind": "skill" if c.text.strip().lstrip("/").lower() in skill_names else "command"}
            for c in completer.get_completions(doc, None)]
    items = to_items(Document(text, len(text)))
    # Rank + bound while a `/token` is under the cursor (the one stage skills are
    # offered at); an argument stage (`/personality `) keeps its command's order.
    if text.rsplit(" ", 1)[-1].startswith("/"):
        score_of = None
        # Command-token stage: the completer only emits name-prefix matches, so merge in
        # catalog entries whose name SUBSTRING or DESCRIPTION words match (name outranks description).
        if " " not in text and len(text) > 1:
            from tui_gateway.slash_fuzzy import fuzzy_rank_slash_items, normalize_slash_search_query
            items, score_of = fuzzy_rank_slash_items(
                items, to_items(Document("/", 1)), normalize_slash_search_query(text))
        usage, origin_of = _skill_usage_lookup()
        items = _rank_slash_completions(items, usage, origin_of, browsing=text == "/", score_of=score_of)
    else:
        items = items[:_SLASH_COMPLETION_LIMIT]
    text_lower = text.lower()
    for extra_text, extra_meta in _SLASH_EXTRAS:
        if extra_text.startswith(text_lower) and not any(item["text"] == extra_text for item in items):
            items.append({**_item(extra_text, extra_meta), "kind": "command"})
    if (details_items := _details_completions(text)) is not None:
        return _ok(rid, {"items": details_items, "replace_from": text.rfind(" ") + 1 if " " in text else len(text)})
    return _ok(rid, {"items": items, "replace_from": text.rfind(" ") + 1 if " " in text else 1})


def _session_agent(params: dict):
    session = _sessions.get(params.get("session_id", ""))
    return session.get("agent") if session else None


@method("a2a.threads")
def _(rid, params: dict) -> dict:
    """Every agent-to-agent conversation one agent is part of.

    The owner is inspecting collaboration that happened in the background, so
    this reads the deployment-level ledger rather than any one profile's
    transcript: a conversation has two ends, and they live in two different
    profiles' databases.

    Each summary names its ``run_id`` — the collaboration episode it belongs
    to. Two agents who worked together twice on different tasks return two
    rows, not one, and a client groups the rows of one run into one card by
    that id rather than by how close together they arrived.

    **Paged.** Run scoping made this list grow with runs × pairs rather than
    with the roster, so it hands back ``next_cursor`` whenever more rows exist;
    pass it back as ``cursor`` for the next page. ``next_cursor: null`` — and
    only that — means the caller has seen every conversation.

    A client must never conclude that a conversation is empty because it is not
    on a page. ``a2a.thread`` addresses one thread directly, by counterpart and
    run, and that is the only answer that carries that authority.
    """
    try:
        from gateway import a2a_threads
        from hermes_constants import get_hermes_home

        profile = str(params.get("profile") or "").strip()
        if not profile:
            return _err(rid, 5040, "profile is required")
        page = a2a_threads.threads_for(
            get_hermes_home(), profile=profile,
            limit=int(params.get("limit") or 100),
            cursor=params.get("cursor"),
        )
        return _ok(rid, {
            "threads": page["threads"],
            # Present and null on the last page rather than absent, so a client
            # can tell "no more" from "this server does not page".
            "next_cursor": page["next_cursor"],
        })
    except Exception as e:
        return _err(rid, 5041, str(e))


@method("a2a.runs")
def _(rid, params: dict) -> dict:
    """Every collaboration **run** one agent took part in, newest first.

    One entry is one row in the owner's transcript. The server groups, because
    the grouping key — ``run_id`` — is a server identity: the work item, the
    owner request, or the turn that issued the sends. A client that grouped
    these itself could only reach for clock proximity, which splits a fan-out
    the moment one agent replies late and merges two topics raised in the same
    minute.

    Each run carries ``has_reply`` and ``reply_count``, and each thread inside
    it carries ``has_reply``. That is here for the same reason the grouping is:
    the server holds the direction of every event, and a client inferring
    "someone answered" from a count got it wrong the moment one teammate was
    messaged twice. Semantics belong in Hermes; the native side renders them.

    Paged on the same contract as ``a2a.threads``: ``next_cursor`` when more
    remain, ``null`` when the caller has seen every run.
    """
    try:
        from gateway import a2a_threads
        from hermes_constants import get_hermes_home

        profile = str(params.get("profile") or "").strip()
        if not profile:
            return _err(rid, 5044, "profile is required")
        page = a2a_threads.runs_for(
            get_hermes_home(), profile=profile,
            limit=int(params.get("limit") or 50),
            cursor=params.get("cursor"),
        )
        return _ok(rid, {"runs": page["runs"], "next_cursor": page["next_cursor"]})
    except Exception as e:
        return _err(rid, 5045, str(e))


@method("a2a.thread")
def _(rid, params: dict) -> dict:
    """One conversation, both directions, in the order it happened.

    Every event carries its own sender, recipient, event id, timestamp and any
    artifact references — the same references a 1:1 message and a room event
    carry, because it is the same registry. Nothing here is recovered from
    prose.

    Addressed either by ``thread_id``, or by ``profile`` + ``counterpart`` +
    ``run_id`` — which the server resolves against the ledger. The second form
    is what a run card opens, and it exists so that a listing is never the only
    route to a thread: a client that could only reach a thread by finding it on
    a page of ``a2a.threads`` would, past that page, tell the owner two agents
    had never messaged each other while the exchange sat intact in the ledger.

    ``run_id`` **addresses; it does not filter.** A thread id already names
    exactly one run, so once one is resolved the whole thread is the run and
    there is nothing left to narrow. Passing a ``run_id`` alongside a
    ``thread_id`` that belongs to a different run does not silently return a
    subset — it returns that thread, and the ``run_id`` in the response is the
    thread's own.

    ``resolved`` separates the two empty answers a client must never conflate.
    ``resolved: true`` means the server located this conversation in the ledger
    and what follows describes it. ``resolved: false`` means it did not — an
    unknown pair-and-run, or a stale thread id — and the client must say *that*,
    because it is not a statement about whether the two agents have ever spoken.
    Rendering "no messages between abu-saud and faisal" over a false is the
    defect this field exists to make impossible.
    """
    try:
        from gateway import a2a_threads
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        thread = str(params.get("thread_id") or "").strip()
        # Admitted, not echoed. A ``run_id`` arriving here is a caller's string,
        # and both answers below put it back on the wire under a field the
        # client contract makes a view identity and an accessibility identifier.
        # A run that reaches a database column goes through this one door
        # (`record_send` does); a run that reaches a client's view has no
        # business skipping it — two hundred characters of a caller's own
        # choosing round-tripped verbatim. Canonical in, unchanged out;
        # anything else is hashed into shape here exactly as the ledger hashed
        # it on the way in, so the value that addresses a thread and the value
        # echoed for one that resolved to nothing are spelled the same way.
        run = a2a_threads.collaboration_run("inherited", params.get("run_id"))
        # Read once, for both forms. In the addressed form it names which end is
        # asking, and the ledger owes that end only what is its own: a slug the
        # owner deleted and minted again shares one legacy thread id with the
        # counterpart, so the new agent's chat would otherwise open on the
        # deleted agent's side of it the moment a new message landed there.
        # Optional in the ``thread_id`` form as it always was — a thread id and
        # nothing else is the owner addressing their own audit trail directly,
        # and that answer is the whole thread.
        profile = str(params.get("profile") or "").strip()
        if not thread:
            counterpart = str(params.get("counterpart") or "").strip()
            if not profile or not counterpart:
                return _err(rid, 5042, "thread_id, or profile and counterpart, is required")
            thread = a2a_threads.resolve_thread(
                home, profile=profile, counterpart=counterpart, run=run
            )
            if not thread:
                # Not an error: the owner asked to open a collaboration that
                # this ledger has no record of. Saying so plainly is what lets
                # the client avoid claiming the two agents never spoke.
                return _ok(rid, {
                    "thread_id": "", "run_id": run, "resolved": False,
                    "events": [], "event_count": 0, "participants": [],
                })
        events = a2a_threads.read_thread(
            home, thread=thread, limit=int(params.get("limit") or 500), profile=profile,
        )
        return _ok(rid, {
            "thread_id": thread,
            # The episode this conversation is, stated once for the whole
            # thread and read from the events rather than echoed back from the
            # request — every event in a thread shares one run by construction,
            # so this is the thread's own answer and not the caller's guess.
            "run_id": next((e.run for e in events), run),
            # Whether the ledger actually holds this conversation. A thread id
            # that names nothing — stale, or every row discarded before it was
            # ever sent — answers false rather than claiming an empty
            # conversation, because the client's copy for false is "could not
            # open this" and its copy for an empty true would be a claim about
            # two agents' history that the server has no basis to make.
            "resolved": bool(events),
            "events": [e.to_dict() for e in events],
            "event_count": len(events),
            # Stated by the server rather than counted by the client, so both
            # ends of the conversation are named even if one of them has not
            # spoken yet.
            "participants": sorted({p for e in events for p in (e.sender, e.recipient)}),
        })
    except Exception as e:
        return _err(rid, 5043, str(e))


@method("push.register")
def _(rid, params: dict) -> dict:
    """Record this device so Hermes can reach the owner when it matters.

    Asera has called this on every connect since the previous wave; until now
    the gateway answered "method not found", and the client recorded that
    honestly rather than pretending it had registered.

    Authenticated by the connection itself — this is a gateway method, so it is
    already behind the same gate as everything else. Idempotent and keyed by
    install id, so calling it on every connect does not grow the registry.

    The response deliberately does not echo the token back: the caller already
    has it, and a credential in a response is a credential in a log.
    """
    try:
        from gateway import push_registry
        from hermes_constants import get_hermes_home

        device = push_registry.register(
            get_hermes_home(),
            token=params.get("token"),
            install_id=params.get("install_id"),
            platform=str(params.get("platform") or "ios"),
            bundle_id=str(params.get("bundle_id") or ""),
        )
        # Said plainly so the client can show the truth rather than a hopeful
        # "registered": the device is recorded, and Hermes still cannot send an
        # APNs push until it holds a signing key from a Developer Program team.
        return _ok(rid, {
            "device": device.to_dict(),
            "delivery_available": False,
            "delivery_blocked_reason": "apns_credentials_unavailable",
        })
    except Exception as e:
        reason = getattr(e, "reason", None)
        if reason:
            return _err(rid, 4036, str(e), {"reason": reason})
        return _err(rid, 5036, str(e))


@method("model.routing_state")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """What model this agent is configured for, and what is actually running.

    Scope 12 asks the owner "which model does this agent use"; Scope 16 makes
    the honest answer two values, because a turn can be running somewhere else
    while the configured primary's included allowance is unavailable.

    Both are read, never stored. The configured model is the profile's own
    `config.yaml`; the effective model is the live agent, if one is up. A third
    persisted copy would be the second configuration system the scope forbids,
    and it would be the copy that goes stale.

    With no live agent — nothing has run yet — effective equals configured,
    which is true rather than merely convenient: fallback is turn-scoped and is
    restored at the start of every turn.
    """
    try:
        from gateway import model_routing

        cfg = _load_cfg() or {}
        model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
        configured_provider = str(model_cfg.get("provider") or "")
        configured_model = str(model_cfg.get("default") or model_cfg.get("model") or "")

        session = _sessions.get(params.get("session_id", ""))
        agent = session.get("agent") if session else None
        effective_provider = str(getattr(agent, "provider", "") or "") or configured_provider
        effective_model = str(getattr(agent, "model", "") or "") or configured_model

        # Classified from the credential that would actually be used, not from
        # the provider id: `anthropic` is a subscription or a paid key
        # depending on the pool entry.
        entry = None
        pool = getattr(agent, "_credential_pool", None) if agent is not None else None
        try:
            entries = pool.entries() if pool is not None else []
            entry = entries[0] if entries else None
        except Exception:
            entry = None
        route_class = model_routing.classify_credential(effective_provider, entry)

        reason = ""
        if effective_provider != configured_provider or effective_model != configured_model:
            # No `or REASONS["quota"]` fallback. The attribute it read was never
            # written, so that fallback fired every time and reported quota
            # exhaustion for a manual model switch, a missing model or an auth
            # refusal alike — the one inference Scope 16 forbids. An unrecognised
            # reason now yields no sentence rather than a confident wrong one;
            # the owner still sees that effective differs from configured.
            reason = model_routing.reason_for(
                getattr(agent, "_last_failover_reason", "") if agent is not None else ""
            )

        state = model_routing.RoutingState(
            configured_provider=configured_provider,
            configured_model=configured_model,
            effective_provider=effective_provider,
            effective_model=effective_model,
            route_class=route_class,
            reason=reason,
        )
        payload = state.to_dict()
        payload["reasoning_effort"] = str(
            ((cfg.get("agent") or {}) if isinstance(cfg.get("agent"), dict) else {})
            .get("reasoning_effort") or ""
        )
        payload["service_tier"] = str(
            ((cfg.get("agent") or {}) if isinstance(cfg.get("agent"), dict) else {})
            .get("service_tier") or ""
        )
        return _ok(rid, payload)
    except Exception as e:
        return _err(rid, 5035, str(e))


@method("model.options")
@_profile_scoped
@_catch(5033)
def _(rid, params: dict) -> dict:
    from hermes_cli.inventory import build_model_options_payload
    # A spawned agent owns the live provider/model/base_url; empty attributes must
    # NOT clobber disk config (with_overrides is truthy-only).
    return _ok(rid, build_model_options_payload(
        _model_picker_context(_session_agent(params)), explicit_only=bool(params.get("explicit_only")),
        include_unconfigured=bool(params.get("include_unconfigured")), refresh=bool(params.get("refresh"))))


@method("model.save_key")
@_catch(5034)
def _(rid, params: dict) -> dict:
    """Save an API key for ``slug``; return its refreshed provider row (model.options shape + ``authenticated``)."""
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.config import is_managed
    slug, api_key = (params.get("slug") or "").strip(), (params.get("api_key") or "").strip()
    if not slug or not api_key:
        return _err(rid, 4001, "slug and api_key are required")
    if is_managed():
        return _err(rid, 4006, "managed install — credentials are read-only")
    if not (pconfig := PROVIDER_REGISTRY.get(slug)):
        return _err(rid, 4002, f"unknown provider: {slug}")
    if pconfig.auth_type != "api_key":
        return _err(rid, 4003, f"{pconfig.name} uses {pconfig.auth_type} auth — run `hermes model` to configure")
    if not pconfig.api_key_env_vars:
        return _err(rid, 4004, f"no env var defined for {pconfig.name}")
    # Save the key to ~/.hermes/.env via the unified credential lifecycle so any stale config.yaml mirror of
    # the previous key (model.api_key, custom_providers[*].api_key) is rotated in the same action (#62269).
    env_var = pconfig.api_key_env_vars[0]
    from hermes_cli.credential_lifecycle import save_provider_env_credential  # also rotates stale config.yaml mirrors
    save_provider_env_credential(env_var, api_key)
    os.environ[env_var] = api_key  # so the refreshed inventory sees it
    # Shared inventory builder (lock-step with model.options / dashboard); picker_hints carries `authenticated`.
    from hermes_cli.inventory import build_models_payload
    payload = build_models_payload(_model_picker_context(_session_agent(params)), picker_hints=True, max_models=50)
    provider_data = next((p for p in payload["providers"] if p["slug"] == slug), None)
    if provider_data is None:  # key saved but provider didn't appear — still success
        provider_data = {"slug": slug, "name": pconfig.name, "is_current": False, "models": [], "total_models": 0}
    provider_data["authenticated"] = True  # synthetic fallback bypasses picker_hints
    return _ok(rid, {"provider": provider_data})


@method("model.disconnect")
@_catch(5035)
def _(rid, params: dict) -> dict:
    """Remove all credentials (env keys AND OAuth/pool state) for provider ``slug``."""
    from hermes_cli.auth import PROVIDER_REGISTRY, clear_provider_auth
    from hermes_cli.credential_lifecycle import remove_provider_env_credential
    if not (slug := (params.get("slug") or "").strip()):
        return _err(rid, 4001, "slug is required")
    pconfig = PROVIDER_REGISTRY.get(slug)
    # Remove EVERY env var plus its mirrors or the provider resurrects in the picker after restart.
    env_vars = (pconfig.api_key_env_vars if pconfig else None) or ()
    cleared_env = any([remove_provider_env_credential(ev).get("found") for ev in env_vars])
    cleared_auth = clear_provider_auth(slug)  # full disconnect: OAuth grants go too
    if not cleared_env and not cleared_auth:
        return _err(rid, 4005, f"no credentials found for {slug}")
    return _ok(rid, {"slug": slug, "name": pconfig.name if pconfig else slug, "disconnected": True})


def register(server) -> None:
    """Rebind this module's helpers + handlers onto ``server`` and register the handlers."""
    bind_module(globals(), server, skip=("_",))
