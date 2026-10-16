import asyncio

import pytest

from mm_bot_manager import runtime
from mm_bot_manager.db import connect, transaction


async def test_event_queue_bounds_concurrency_and_applies_backpressure(monkeypatch):
    started, saturated, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    running = peak = produced = completed = 0

    async def handler(*args):
        nonlocal running, peak, completed
        running += 1
        peak = max(peak, running)
        if running == 2:
            started.set()
        await release.wait()
        running -= 1
        completed += 1

    async def source():
        nonlocal produced
        for _ in range(20):
            produced += 1
            if produced == 6:  # 2 workers + 3 queued + 1 producer waiting to enqueue.
                saturated.set()
            yield {}

    monkeypatch.setattr(runtime, "handle_event", handler)
    task = asyncio.create_task(
        runtime.process_events(source(), None, None, worker_count=2, queue_size=3)
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(saturated.wait(), 1)
        assert peak == 2
        assert produced == 6
    finally:
        release.set()
        await asyncio.wait_for(task, 1)
    assert completed == 20


async def test_cancellation_drains_started_work_before_returning(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    completed = []

    async def handler(*args):
        started.set()
        await release.wait()
        completed.append(True)

    async def source():
        yield {}
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "handle_event", handler)
    task = asyncio.create_task(runtime.process_events(source(), None, None, worker_count=1))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert completed == [True]


async def test_shutdown_timeout_cancels_and_awaits_workers(monkeypatch):
    cancelled = asyncio.Event()

    async def handler(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def source():
        yield {}

    monkeypatch.setattr(runtime, "handle_event", handler)
    await runtime.process_events(source(), None, None, worker_count=1, drain_timeout=0.001)
    assert cancelled.is_set()


async def test_one_bad_event_does_not_kill_worker(monkeypatch):
    calls = []

    async def handler(event, *args):
        calls.append(event)
        if len(calls) == 1:
            raise ValueError("malformed event")

    async def source():
        yield {"event": "bad"}
        yield {"event": "good"}

    monkeypatch.setattr(runtime, "handle_event", handler)
    await runtime.process_events(source(), None, None, worker_count=1)
    assert len(calls) == 2


def test_sqlite_instance_lock_excludes_second_process_and_is_released(tmp_path):
    path = str(tmp_path / "manager.sqlite3")
    conn = connect(path)
    try:
        with (
            runtime.instance_lock(conn, path),
            pytest.raises(RuntimeError, match="Another"),
            runtime.instance_lock(conn, path),
        ):
            pytest.fail("second lock acquired")
        with runtime.instance_lock(conn, path):
            pass
    finally:
        conn.close()


def test_failed_commit_rolls_back_and_preserves_original_error():
    class FailingCommit:
        def __init__(self):
            self.calls = []

        def execute(self, sql):
            self.calls.append(sql)
            if sql == "COMMIT":
                raise RuntimeError("commit failed")

    conn = FailingCommit()
    with pytest.raises(RuntimeError, match="commit failed"), transaction(conn):
        pass
    assert conn.calls == ["BEGIN", "COMMIT", "ROLLBACK"]


def test_cancelled_transaction_rolls_back(tmp_path):
    conn = connect(str(tmp_path / "db"))
    try:
        conn.execute("CREATE TABLE example (id TEXT)")
        with pytest.raises(asyncio.CancelledError), transaction(conn):
            conn.execute("INSERT INTO example VALUES ('temporary')")
            raise asyncio.CancelledError
        assert conn.execute("SELECT * FROM example").fetchall() == []
    finally:
        conn.close()


def test_sqlite_lock_cannot_be_bypassed_with_symlink(tmp_path):
    path = tmp_path / "manager.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    conn = connect(str(path))
    alias.symlink_to(path)
    try:
        with (
            runtime.instance_lock(conn, str(path)),
            pytest.raises(RuntimeError, match="Another"),
            runtime.instance_lock(conn, str(alias)),
        ):
            pytest.fail("symlink bypassed the lock")
    finally:
        conn.close()
