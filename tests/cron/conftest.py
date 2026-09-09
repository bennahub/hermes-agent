"""Cron-test fixtures.

Provides a default ``HERMES_MODEL`` for cron run_job tests so each one
doesn't have to spell out a model. The global conftest blanks
HERMES_MODEL hermetically; without this autouse fixture every cron test
that exercises ``run_job`` would hit the fail-fast guard added in
``cron/scheduler.py`` (see issue #23979) and have to be rewritten.

Tests that specifically need ``HERMES_MODEL`` unset — model-resolution
edge cases — call ``monkeypatch.delenv("HERMES_MODEL", raising=False)``
inside the test, which overrides this fixture's value for that scope.
"""

import pytest


@pytest.fixture()
def make_cron_provider():
    """Factory for minimal CronScheduler test doubles.

    ``make_cron_provider(register_job=...)`` returns a real ``CronScheduler``
    subclass instance whose ``register_job`` is the given callable — so tests
    exercising the creation-registration contract share one stub instead of
    redefining inline spy/failing classes, and an ABC rename breaks them
    loudly instead of silently passing a duck-type.
    """
    from cron.scheduler_provider import CronScheduler

    def _make(register_job=None, name="stub"):
        class _StubProvider(CronScheduler):
            @property
            def name(self):  # pragma: no cover - trivial
                return name

            def start(self, stop_event, **kw):  # pragma: no cover - unused
                pass

            def register_job(self, job):
                if register_job is not None:
                    return register_job(job)
                return None

        return _StubProvider()

    return _make


@pytest.fixture(autouse=True)
def _default_cron_test_model(monkeypatch):
    """Pin a default HERMES_MODEL so cron run_job tests have a resolvable model."""
    monkeypatch.setenv("HERMES_MODEL", "test-cron-default-model")
    yield


@pytest.fixture(autouse=True)
def _reset_session_context_vars():
    """Restore session ContextVars around cron tests that call run_job directly.

    Production confines each cron run to a copied context, but direct unit tests
    share the pytest context. ``run_job`` intentionally clears ordinary session
    variables to explicit empty values, which would otherwise shadow legacy env
    fallbacks used by later approval tests in the same process.
    """
    from gateway.session_context import _UNSET, _VAR_MAP

    def _reset_all():
        for var in _VAR_MAP.values():
            var.set(_UNSET)

    _reset_all()
    yield
    _reset_all()


@pytest.fixture()
def migrate_configured_cron_job(monkeypatch, tmp_path):
    """Adopt explicit fixture schedules through the real migration and action gate."""
    import json
    from types import SimpleNamespace
    from scripts import migrate_execution_scopes as migration

    def policy(**kwargs):
        assert kwargs["task"] == "execution_scope"
        payload = json.loads(kwargs["messages"][-1]["content"])
        subject = payload["original_instruction"]
        if "invocation" not in payload:
            decision = {"objective": "Execute the explicit configured fixture schedule",
                        "permitted": ["Only the configured script bytes"], "excluded": ["Unrelated actions"]}
        else:
            call = payload["invocation"]
            from pathlib import Path
            allowed_arguments = [
                {**snapshot, "cwd": subject["configured_assignment"].get("workdir")
                 or str(Path(snapshot["path"]).parent)}
                for snapshot in subject["configured_scripts"].values()
            ]
            decision = {"allowed": call["tool"] == "cron_script"
                        and call["arguments"] in allowed_arguments,
                        "reason": "Exact explicitly configured script snapshot"}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(decision)))])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", policy)

    def adopt(home, job):
        from cron.jobs import get_job
        (home / "profiles").mkdir(exist_ok=True)
        manifest = migration.prepare(home)
        migration.apply(manifest, tmp_path / ("scope-backup-" + job["id"]))
        return get_job(job["id"])

    return adopt


@pytest.fixture()
def run_scoped_cron_job():
    """Supply the same persisted native execution identity used by scheduler dispatch."""
    def run(job):
        from cron.executions import create_execution, finish_execution
        from cron.scheduler import run_job

        execution = create_execution(job["id"], source="test-native-dispatch")
        result = run_job(job, execution_id=execution["id"])
        finish_execution(execution["id"], success=result[0])
        return result
    return run
