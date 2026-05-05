"""Shared test fixtures + in-memory fakes.

Two surfaces:

* **In-memory** (``inmemory_store``, ``inmemory_service``) — lift of
  the ``_FakeSession`` / ``_FakeResult`` helpers that
  ``tests/contract/test_instance_service.py`` built during T030. Used
  by unit/contract tests that need an ``InstanceService`` but no real
  Postgres. The fakes cover exactly the queries InstanceService
  actually runs — new queries trigger an ``AssertionError`` so
  test-only code paths can't hide behind a too-clever dummy.

* **Fixture factory** (``tests/integration/fixtures/instance_factory.py``)
  — full DB-backed fixtures for integration tests. Those live in the
  integration subtree and pull in real Postgres via
  ``async_session``. They do not appear here.

Kept as a plain module so both pytest and individual tests can import
the fakes without pulling the whole integration path.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# In-memory store — one dict per table InstanceService reads/writes.
# ---------------------------------------------------------------------------


@dataclass
class InMemoryStore:
    """In-memory store used by the fake session. Lifted from T030.

    ``chats`` and ``instances`` are keyed by their primary keys.
    ``commits`` is a counter tests can inspect to verify the service
    did / did not commit.
    """
    chats: dict[int, Any] = field(default_factory=dict)
    instances: dict[uuid.UUID, Any] = field(default_factory=dict)
    commits: int = 0


class InMemoryResult:
    """Tiny stand-in for a SQLAlchemy ``Result`` object."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> "InMemoryResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class InMemorySession:
    """Async-session fake. See T030 for the original (copy-of-the-copy)."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self._pending: list[Any] = []

    async def __aenter__(self) -> "InMemorySession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, model: type, pk: Any) -> Any:
        from taghdev.models.instance import Instance
        from taghdev.models.web_chat import WebChatSession
        if model is WebChatSession:
            return self._store.chats.get(int(pk))
        if model is Instance:
            return self._store.instances.get(
                pk if isinstance(pk, uuid.UUID) else uuid.UUID(str(pk))
            )
        raise AssertionError(f"InMemorySession.get({model}, {pk!r}) not implemented")

    async def execute(self, stmt: Any) -> InMemoryResult:
        """Cover the exact queries InstanceService runs.

        New query shapes should be added here deliberately so dead
        code can't quietly pass through.
        """
        from sqlalchemy.sql.elements import BooleanClauseList  # noqa: F401  (used by _flatten_and)

        cols = list(stmt.selected_columns)
        where = stmt.whereclause
        preds = _flatten_and(where) if where is not None else []

        col_tables = {getattr(c.table, "name", None) for c in cols}
        col_names = [c.name for c in cols]

        # Full-entity query — ``select(Instance).where(...)``.
        if col_tables == {"instances"} and len(cols) > 1:
            rows = self._filter_instances(preds)
            user_pred = _find_pred(preds, table="web_chat_sessions", col="user_id")
            if user_pred is not None:
                user_id = user_pred.right.value
                chat_ids = {
                    cid for cid, chat in self._store.chats.items()
                    if chat.user_id == user_id
                }
                rows = [r for r in rows if r.chat_session_id in chat_ids]
            return InMemoryResult(rows)

        # Scalar query — ``select(Instance.chat_session_id).join(...)``.
        if col_tables == {"instances"} and col_names == ["chat_session_id"]:
            rows = self._filter_instances(preds)
            user_pred = _find_pred(preds, table="web_chat_sessions", col="user_id")
            if user_pred is not None:
                user_id = user_pred.right.value
                chat_ids = {
                    cid for cid, chat in self._store.chats.items()
                    if chat.user_id == user_id
                }
                rows = [r for r in rows if r.chat_session_id in chat_ids]
            return InMemoryResult([(r.chat_session_id,) for r in rows])

        raise AssertionError(
            f"InMemorySession.execute does not implement {stmt!r}; "
            f"cols={col_names} tables={col_tables}"
        )

    def _filter_instances(self, preds: list[Any]) -> list[Any]:
        rows = list(self._store.instances.values())
        for p in preds:
            left = getattr(p, "left", None)
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
                values = _extract_in_values(p)
                if values is not None:
                    rows = [r for r in rows if r.status in values]
        return rows

    def add(self, obj: Any) -> None:
        from taghdev.models.instance import Instance
        if isinstance(obj, Instance):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self._pending.append(obj)
        else:
            raise AssertionError(f"InMemorySession.add({type(obj)}) not supported")

    async def commit(self) -> None:
        for inst in self._pending:
            self._store.instances[inst.id] = inst
        self._pending.clear()
        self._store.commits += 1

    async def refresh(self, obj: Any) -> None:
        return None

    async def delete(self, obj: Any) -> None:
        from taghdev.models.instance import Instance
        if isinstance(obj, Instance) and obj.id in self._store.instances:
            del self._store.instances[obj.id]


def _flatten_and(where: Any) -> list[Any]:
    from sqlalchemy.sql.elements import BooleanClauseList
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
    right = getattr(pred, "right", None)
    if right is None:
        return None
    for attr in ("element", "value"):
        val = getattr(right, attr, None)
        if isinstance(val, (list, tuple, set, frozenset)):
            return set(val)
    clauses = getattr(right, "clauses", None)
    if clauses:
        return {c.value for c in clauses if hasattr(c, "value")}
    return None


# ---------------------------------------------------------------------------
# Pytest fixtures — one for the store, one for a bound InstanceService.
# ---------------------------------------------------------------------------


@pytest.fixture
def inmemory_store() -> InMemoryStore:
    """A clean in-memory store per test."""
    return InMemoryStore()


@pytest.fixture
def inmemory_service(inmemory_store: InMemoryStore):
    """Build an ``InstanceService`` + enqueuer-call-recorder pair.

    Returns ``(service, recorded_calls)``. Tests that need to assert
    on enqueued jobs inspect ``recorded_calls`` — a list of
    ``(job_name, args)`` tuples.
    """
    from taghdev.services.instance_service import InstanceService

    calls: list[tuple[str, tuple[Any, ...]]] = []

    def session_factory() -> InMemorySession:
        return InMemorySession(inmemory_store)

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
    )
    return svc, calls


@pytest.fixture
def make_chat():
    """Factory for a ``WebChatSession``-shaped object.

    The real ``WebChatSession`` ORM class is fine to instantiate
    without a session — SQLAlchemy only binds on commit. We plant a
    simple ``project`` attribute holding ``.mode`` since InstanceService
    reads it during provision.
    """
    def _factory(
        chat_id: int,
        user_id: int,
        *,
        project_id: int = 1,
        mode: str = "container",
        session_branch: str = "session-x",
    ):
        from taghdev.models.web_chat import WebChatSession

        @dataclass
        class _FakeProject:
            id: int
            mode: str = "container"
            name: str = "test-project"
            github_repo: str = "org/test-project"
            default_branch: str = "main"

        chat = WebChatSession(
            id=chat_id,
            user_id=user_id,
            project_id=project_id,
            session_branch_name=session_branch,
        )
        chat.project = _FakeProject(id=project_id, mode=mode)  # type: ignore[assignment]
        return chat
    return _factory
