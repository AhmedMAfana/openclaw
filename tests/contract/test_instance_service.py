"""T030: contract tests for InstanceService.

Covers every public method in specs/001-per-chat-instances/contracts/instance-service.md:

  * provision idempotency (N calls = 1 row)
  * touch is a no-op in terminal states
  * terminate is idempotent
  * state-transition invariants (can't skip 'terminating')
  * get_or_resume re-entrance on idle (re-awakens)
  * get_or_resume on destroyed rows provisions anew
  * distinct errors: PerUserCapExceeded vs PlatformAtCapacity
  * validation of ``reason`` arg in terminate()

These tests must NOT require Postgres: they use a hand-rolled in-memory
fake session whose query surface is scoped to exactly the access
patterns InstanceService uses. Integration-tier coverage (real DB +
compose + tunnel) lives in tests/integration/ (T031, T034, T034a).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.sql.elements import BooleanClauseList

from openclow.models.instance import Instance, InstanceStatus
from openclow.models.web_chat import WebChatSession
from openclow.services.instance_service import (
    ACTIVE_STATUSES,
    ChatNotFound,
    HeartbeatSignals,
    InstanceNotFound,
    InstanceService,
    PerUserCapExceeded,
    PlatformAtCapacity,
    ProjectNotContainerMode,
)


# ---------------------------------------------------------------------------
# Minimal in-memory store + fake AsyncSession.
#
# We do NOT try to be a general SQLAlchemy mock. We intercept just the two
# kinds of `session.execute(stmt)` calls the service issues, identified by
# the statement's entity list. If InstanceService grows new queries, this
# fake needs a matching branch — failing loudly on unknown shapes is the
# deliberate design here (no silent test coverage holes).
# ---------------------------------------------------------------------------


@dataclass
class _Store:
    chats: dict[int, WebChatSession] = field(default_factory=dict)
    instances: dict[uuid.UUID, Instance] = field(default_factory=dict)
    commits: int = 0


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """In-memory async session that handles exactly InstanceService's queries."""

    def __init__(self, store: _Store) -> None:
        self._store = store
        self._pending: list[Instance] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, model: type, pk: Any) -> Any:
        if model is WebChatSession:
            return self._store.chats.get(int(pk))
        if model is Instance:
            return self._store.instances.get(
                pk if isinstance(pk, uuid.UUID) else uuid.UUID(str(pk))
            )
        raise AssertionError(f"unexpected get({model}, {pk!r})")

    async def execute(self, stmt: Any) -> _FakeResult:
        # Introspect the select's whereclause & columns.
        cols = list(stmt.selected_columns)
        where = stmt.whereclause
        preds = _flatten_and(where) if where is not None else []

        col_tables = {getattr(c.table, "name", None) for c in cols}
        col_names = [c.name for c in cols]

        # A) Full-entity query: `select(Instance).where(...)` — selected_columns
        #    spans every `instances` column. If the query also joined to
        #    web_chat_sessions (list_active(user_id=...)), apply that filter.
        if col_tables == {"instances"} and len(cols) > 1:
            rows = self._filter_instances(preds)
            user_pred = _find_pred(preds, table="web_chat_sessions", col="user_id")
            if user_pred is not None:
                user_id = user_pred.right.value
                chat_ids = {
                    cid
                    for cid, chat in self._store.chats.items()
                    if chat.user_id == user_id
                }
                rows = [r for r in rows if r.chat_session_id in chat_ids]
            return _FakeResult(rows)

        # B) Scalar-column query for the per-user cap — `select(Instance.chat_session_id).join(...)`.
        if col_tables == {"instances"} and col_names == ["chat_session_id"]:
            rows = self._filter_instances(preds)
            user_pred = _find_pred(preds, table="web_chat_sessions", col="user_id")
            if user_pred is not None:
                user_id = user_pred.right.value
                chat_ids = {
                    cid
                    for cid, chat in self._store.chats.items()
                    if chat.user_id == user_id
                }
                rows = [r for r in rows if r.chat_session_id in chat_ids]
            return _FakeResult([(r.chat_session_id,) for r in rows])

        raise AssertionError(
            f"_FakeSession does not implement execute({stmt!r}); "
            f"cols={col_names} tables={col_tables}"
        )

    def _filter_instances(self, preds: list[Any]) -> list[Instance]:
        rows = list(self._store.instances.values())
        for p in preds:
            left = getattr(p, "left", None)
            # sqlalchemy represents `col.in_([...])` as a BinaryExpression whose
            # operator is `in_op`; value list is p.right.value.
            if left is None:
                continue
            col = getattr(left, "name", None)
            tbl = getattr(getattr(left, "table", None), "name", None)
            if tbl != "instances":
                continue
            if col == "chat_session_id":
                target = p.right.value
                rows = [r for r in rows if r.chat_session_id == target]
            elif col == "slug":
                target = p.right.value
                rows = [r for r in rows if r.slug == target]
            elif col == "status":
                # expression like `status.in_([...])`
                values = _extract_in_values(p)
                if values is not None:
                    rows = [r for r in rows if r.status in values]
        return rows

    def add(self, obj: Any) -> None:
        if isinstance(obj, Instance):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self._pending.append(obj)
        else:
            raise AssertionError(f"_FakeSession does not support add({type(obj)})")

    async def commit(self) -> None:
        for inst in self._pending:
            self._store.instances[inst.id] = inst
        self._pending.clear()
        self._store.commits += 1

    async def refresh(self, obj: Any) -> None:
        # In-memory — the row reference IS the stored one.
        return None


def _flatten_and(where: Any) -> list[Any]:
    """Unwrap `.and_()` / `.where(a, b)` into a flat list of predicates."""
    if isinstance(where, BooleanClauseList):
        out: list[Any] = []
        for c in where.clauses:
            out.extend(_flatten_and(c))
        return out
    return [where]


def _find_pred(preds: list[Any], *, table: str, col: str) -> Any | None:
    for p in preds:
        left = getattr(p, "left", None)
        if left is None:
            continue
        if getattr(left, "name", None) != col:
            continue
        if getattr(getattr(left, "table", None), "name", None) != table:
            continue
        return p
    return None


def _extract_in_values(pred: Any) -> set[str] | None:
    """Pull the `values` out of a `col.in_([...])` BinaryExpression."""
    right = getattr(pred, "right", None)
    if right is None:
        return None
    # SA wraps the list in a BindParameter-grouped expression; iterate
    # through clauses if present, else try .value directly.
    for attr in ("element", "value"):
        val = getattr(right, attr, None)
        if isinstance(val, (list, tuple, set, frozenset)):
            return set(val)
    # Fallback: walk grouped clauses.
    clauses = getattr(right, "clauses", None)
    if clauses:
        return {c.value for c in clauses if hasattr(c, "value")}
    return None


@dataclass
class _FakeProject:
    """Stand-in for models.Project — InstanceService only reads .mode."""
    id: int
    mode: str = "container"


def _make_chat(
    chat_id: int,
    user_id: int,
    *,
    project_id: int = 1,
    mode: str = "container",
    session_branch: str = "session-x",
) -> WebChatSession:
    chat = WebChatSession(
        id=chat_id,
        user_id=user_id,
        project_id=project_id,
        session_branch_name=session_branch,
    )
    # InstanceService reads chat.project.mode — plant a simple fake.
    chat.project = _FakeProject(id=project_id, mode=mode)  # type: ignore[assignment]
    return chat


def _new_svc(store: _Store, **kw: Any) -> tuple[InstanceService, list[tuple]]:
    """Build an InstanceService wired to the fake store + a call-recorder."""
    calls: list[tuple] = []

    def session_factory() -> _FakeSession:
        return _FakeSession(store)

    async def fake_enqueue(job_name: str, *args: Any) -> str:
        calls.append((job_name, args))
        return f"job-{job_name}"

    @asynccontextmanager
    async def noop_lock(_key: str, _ttl: int):
        yield

    svc = InstanceService(
        session_factory=session_factory,
        lock_factory=noop_lock,
        job_enqueuer=fake_enqueue,
        **kw,
    )
    return svc, calls


# ---------------------------------------------------------------------------
# provision()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_creates_one_row_and_enqueues_job():
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, calls = _new_svc(store)

    inst = await svc.provision(chat_session_id=1)

    assert isinstance(inst, Instance)
    assert inst.chat_session_id == 1
    assert inst.project_id == 1
    assert inst.status == InstanceStatus.PROVISIONING.value
    assert inst.slug.startswith("inst-") and len(inst.slug) == 19
    assert inst.compose_project == f"tagh-{inst.slug}"
    assert inst.workspace_path == f"/workspaces/{inst.slug}/"
    assert inst.heartbeat_secret and inst.db_password
    assert len(store.instances) == 1
    assert calls == [("provision_instance", (str(inst.id),))]


@pytest.mark.asyncio
async def test_provision_is_idempotent_returns_existing_active_row():
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, calls = _new_svc(store)

    first = await svc.provision(chat_session_id=1)
    second = await svc.provision(chat_session_id=1)
    third = await svc.provision(chat_session_id=1)

    assert first is second is third
    assert len(store.instances) == 1
    # Enqueue must happen only on the row-creating call.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_provision_raises_on_missing_chat():
    store = _Store()
    svc, _ = _new_svc(store)

    with pytest.raises(ChatNotFound) as ei:
        await svc.provision(chat_session_id=99)
    assert ei.value.chat_session_id == 99


@pytest.mark.asyncio
async def test_provision_raises_when_project_not_container_mode():
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42, mode="docker")
    svc, _ = _new_svc(store)

    with pytest.raises(ProjectNotContainerMode) as ei:
        await svc.provision(chat_session_id=1)
    assert ei.value.mode == "docker"


@pytest.mark.asyncio
async def test_provision_raises_per_user_cap_exceeded_with_active_chat_ids():
    store = _Store()
    user_id = 42
    # Seed 3 already-running instances for this user (default cap = 3).
    for cid in (1, 2, 3):
        store.chats[cid] = _make_chat(cid, user_id=user_id)
        store.instances[uuid.uuid4()] = Instance(
            id=uuid.uuid4(),
            slug=f"inst-{cid:014x}",
            chat_session_id=cid,
            project_id=1,
            status=InstanceStatus.RUNNING.value,
            compose_project=f"tagh-inst-{cid:014x}",
            workspace_path=f"/workspaces/inst-{cid:014x}/",
            session_branch="s",
            heartbeat_secret="x",
            db_password="y",
            per_user_count_at_provision=0,
            last_activity_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
    store.chats[4] = _make_chat(4, user_id=user_id)
    svc, _ = _new_svc(store)

    with pytest.raises(PerUserCapExceeded) as ei:
        await svc.provision(chat_session_id=4)
    err = ei.value
    assert err.user_id == user_id
    assert err.cap == 3
    assert sorted(err.active_chat_ids) == [1, 2, 3]


@pytest.mark.asyncio
async def test_provision_raises_platform_at_capacity_before_per_user_check():
    """PlatformAtCapacity must be distinct from PerUserCapExceeded (FR-030 / FR-030a)."""
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)

    async def capacity_full() -> None:
        raise PlatformAtCapacity("host full")

    svc, _ = _new_svc(store, capacity_guard=capacity_full)

    with pytest.raises(PlatformAtCapacity):
        await svc.provision(chat_session_id=1)


# ---------------------------------------------------------------------------
# get_or_resume()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_resume_returns_running_row_unchanged():
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, calls = _new_svc(store)

    provisioned = await svc.provision(chat_session_id=1)
    # Flip to running to simulate infra completing.
    provisioned.status = InstanceStatus.RUNNING.value

    resumed = await svc.get_or_resume(chat_session_id=1)
    assert resumed is provisioned
    # No new enqueue beyond the original provision.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_get_or_resume_wakes_idle_instance_via_touch():
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, _ = _new_svc(store)

    inst = await svc.provision(chat_session_id=1)
    inst.status = InstanceStatus.IDLE.value
    inst.grace_notification_at = datetime.now(timezone.utc)
    original_expires = inst.expires_at

    resumed = await svc.get_or_resume(chat_session_id=1)

    assert resumed is inst
    assert resumed.status == InstanceStatus.RUNNING.value
    assert resumed.grace_notification_at is None
    assert resumed.expires_at >= original_expires


@pytest.mark.asyncio
async def test_get_or_resume_provisions_new_row_when_prior_destroyed():
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, calls = _new_svc(store)

    old = await svc.provision(chat_session_id=1)
    old.status = InstanceStatus.DESTROYED.value
    old.terminated_at = datetime.now(timezone.utc)

    new = await svc.get_or_resume(chat_session_id=1)
    assert new is not old
    assert new.status == InstanceStatus.PROVISIONING.value
    assert len(store.instances) == 2
    assert len(calls) == 2  # two enqueues


# ---------------------------------------------------------------------------
# touch()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_touch_bumps_running_instance_and_clears_grace():
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, _ = _new_svc(store)
    inst = await svc.provision(chat_session_id=1)
    inst.status = InstanceStatus.RUNNING.value
    inst.grace_notification_at = datetime.now(timezone.utc)
    inst.expires_at = datetime.now(timezone.utc)

    before = inst.expires_at
    await svc.touch(inst.id)

    assert inst.grace_notification_at is None
    assert inst.expires_at > before


@pytest.mark.asyncio
async def test_touch_is_noop_in_terminal_states():
    """Contract: touch must not resurrect destroyed/failed/terminating rows."""
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, _ = _new_svc(store)
    inst = await svc.provision(chat_session_id=1)

    for terminal in (
        InstanceStatus.TERMINATING.value,
        InstanceStatus.DESTROYED.value,
        InstanceStatus.FAILED.value,
    ):
        inst.status = terminal
        inst.grace_notification_at = datetime(
            2026, 1, 1, tzinfo=timezone.utc
        )
        snapshot_expires = inst.expires_at
        snapshot_grace = inst.grace_notification_at

        await svc.touch(inst.id)

        assert inst.status == terminal, f"touch mutated status from {terminal}"
        assert inst.expires_at == snapshot_expires
        assert inst.grace_notification_at == snapshot_grace


@pytest.mark.asyncio
async def test_touch_raises_on_missing_instance():
    store = _Store()
    svc, _ = _new_svc(store)

    with pytest.raises(InstanceNotFound):
        await svc.touch(uuid.uuid4())


# ---------------------------------------------------------------------------
# terminate()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_flips_to_terminating_and_enqueues_teardown():
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, calls = _new_svc(store)
    inst = await svc.provision(chat_session_id=1)
    inst.status = InstanceStatus.RUNNING.value

    await svc.terminate(inst.id, reason="user_request")

    assert inst.status == InstanceStatus.TERMINATING.value
    assert inst.terminated_reason == "user_request"
    assert ("teardown_instance", (str(inst.id),)) in calls


@pytest.mark.asyncio
async def test_terminate_is_idempotent_on_terminating_and_destroyed():
    """Contract: repeated terminate() on already-terminal rows is a no-op."""
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, calls = _new_svc(store)
    inst = await svc.provision(chat_session_id=1)
    inst.status = InstanceStatus.TERMINATING.value
    inst.terminated_reason = "user_request"

    # idempotent
    await svc.terminate(inst.id, reason="user_request")
    assert inst.status == InstanceStatus.TERMINATING.value
    assert not any(c[0] == "teardown_instance" for c in calls)

    inst.status = InstanceStatus.DESTROYED.value
    await svc.terminate(inst.id, reason="user_request")
    assert inst.status == InstanceStatus.DESTROYED.value


@pytest.mark.asyncio
async def test_terminate_rejects_unknown_reason():
    """reason is a closed set — ck_instances_terminated_reason at the DB."""
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, _ = _new_svc(store)
    inst = await svc.provision(chat_session_id=1)
    inst.status = InstanceStatus.RUNNING.value

    with pytest.raises(ValueError):
        await svc.terminate(inst.id, reason="not-a-real-reason")


@pytest.mark.asyncio
async def test_terminate_raises_on_missing_instance():
    store = _Store()
    svc, _ = _new_svc(store)
    with pytest.raises(InstanceNotFound):
        await svc.terminate(uuid.uuid4(), reason="user_request")


@pytest.mark.asyncio
async def test_state_transition_cannot_skip_terminating():
    """A running instance does not jump directly to destroyed.

    terminate() is the ONLY path to destroyed for non-failed rows; it
    always transitions through 'terminating' first (the ARQ teardown job
    then flips to 'destroyed' on completion).
    """
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, _ = _new_svc(store)
    inst = await svc.provision(chat_session_id=1)
    inst.status = InstanceStatus.RUNNING.value

    await svc.terminate(inst.id, reason="user_request")

    assert inst.status == InstanceStatus.TERMINATING.value
    # 'destroyed' is owned by the teardown job, not the service.
    assert inst.status != InstanceStatus.DESTROYED.value
    assert inst.terminated_at is None


# ---------------------------------------------------------------------------
# list_active()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_active_scopes_to_active_statuses_only():
    store = _Store()
    for cid, status in (
        (1, InstanceStatus.RUNNING.value),
        (2, InstanceStatus.IDLE.value),
        (3, InstanceStatus.PROVISIONING.value),
        (4, InstanceStatus.DESTROYED.value),  # excluded
        (5, InstanceStatus.FAILED.value),     # excluded
    ):
        store.chats[cid] = _make_chat(cid, user_id=42)
        inst_id = uuid.uuid4()
        store.instances[inst_id] = Instance(
            id=inst_id,
            slug=f"inst-{cid:014x}",
            chat_session_id=cid,
            project_id=1,
            status=status,
            compose_project="tagh",
            workspace_path="/workspaces/x/",
            session_branch="s",
            heartbeat_secret="x",
            db_password="y",
            per_user_count_at_provision=0,
            last_activity_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
    svc, _ = _new_svc(store)

    active = await svc.list_active()
    statuses = {i.status for i in active}
    assert statuses.issubset(ACTIVE_STATUSES)
    assert not ({"destroyed", "failed"} & statuses)


@pytest.mark.asyncio
async def test_list_active_filters_by_user_id():
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    store.chats[2] = _make_chat(2, user_id=99)
    svc, _ = _new_svc(store)
    await svc.provision(chat_session_id=1)
    await svc.provision(chat_session_id=2)

    u42 = await svc.list_active(user_id=42)
    u99 = await svc.list_active(user_id=99)

    assert [i.chat_session_id for i in u42] == [1]
    assert [i.chat_session_id for i in u99] == [2]


# ---------------------------------------------------------------------------
# record_heartbeat()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_heartbeat_bumps_expiry_on_running_instance():
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, _ = _new_svc(store)
    inst = await svc.provision(chat_session_id=1)
    inst.status = InstanceStatus.RUNNING.value
    inst.expires_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    ack = await svc.record_heartbeat(
        inst.slug,
        HeartbeatSignals(dev_server_running=True),
    )

    assert ack.status == InstanceStatus.RUNNING.value
    assert ack.expires_at == inst.expires_at
    assert ack.expires_at > datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_record_heartbeat_returns_current_status_for_terminal_row():
    """Router uses this to return 409 per heartbeat-api.md."""
    store = _Store()
    store.chats[1] = _make_chat(1, user_id=42)
    svc, _ = _new_svc(store)
    inst = await svc.provision(chat_session_id=1)
    inst.status = InstanceStatus.TERMINATING.value
    snapshot = inst.expires_at

    ack = await svc.record_heartbeat(inst.slug, HeartbeatSignals())

    assert ack.status == InstanceStatus.TERMINATING.value
    assert ack.expires_at == snapshot  # not bumped


@pytest.mark.asyncio
async def test_record_heartbeat_raises_on_unknown_slug():
    store = _Store()
    svc, _ = _new_svc(store)

    with pytest.raises(InstanceNotFound):
        await svc.record_heartbeat("inst-ffffffffffffff", HeartbeatSignals())
