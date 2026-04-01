"""
Regression tests for per-user job_id ownership in trns.bot.routes.

Keeps tests offline: no Pyrogram, no real pipeline threads.
"""

import asyncio
import threading

import pytest

from trns.bot import routes as routes_mod


@pytest.fixture(autouse=True)
def clean_registry():
    """Isolate tests from each other — user_processing_tasks is process-global."""
    routes_mod.user_processing_tasks.clear()
    yield
    routes_mod.user_processing_tasks.clear()


class TestRemoveTaskIfOwner:
    def test_matching_job_id_removes_entry(self):
        uid = 1001
        jid = "aaa111"
        routes_mod.user_processing_tasks[uid] = {"job_id": jid, "type": "youtube"}

        assert routes_mod._remove_task_if_owner(uid, jid) is True
        assert uid not in routes_mod.user_processing_tasks

    def test_mismatch_does_not_remove_newer_job(self):
        uid = 1002
        routes_mod.user_processing_tasks[uid] = {"job_id": "newer", "type": "youtube"}

        assert routes_mod._remove_task_if_owner(uid, "older") is False
        assert routes_mod.user_processing_tasks[uid]["job_id"] == "newer"


class TestCancelUserProcessing:
    """asyncio.run wraps async cancel for pytest without pytest-asyncio."""

    def test_removes_registry_when_job_unchanged(self):
        uid = 2001
        jid = "samejob01"

        async def run():
            loop = asyncio.get_running_loop()
            ex = loop.create_future()
            ex.set_result(None)
            routes_mod.user_processing_tasks[uid] = {
                "job_id": jid,
                "shutdown_flag": threading.Event(),
                "output_task": asyncio.create_task(asyncio.sleep(0)),
                "executor_task": ex,
                "task": asyncio.create_task(asyncio.sleep(0)),
            }
            await asyncio.sleep(0)
            await routes_mod.cancel_user_processing(uid)

        asyncio.run(run())
        assert uid not in routes_mod.user_processing_tasks

    def test_stale_cancel_does_not_wipe_replaced_job(self):
        """Simulates: cancel copied old job_id; registry was replaced by a new run before cleanup."""
        uid = 2002
        old_jid = "oldjob02"

        async def run():
            loop = asyncio.get_running_loop()
            exec_fut = loop.create_future()

            routes_mod.user_processing_tasks[uid] = {
                "job_id": old_jid,
                "shutdown_flag": threading.Event(),
                "output_task": asyncio.create_task(asyncio.sleep(0)),
                "executor_task": exec_fut,
                "task": asyncio.create_task(asyncio.sleep(0)),
            }
            await asyncio.sleep(0)

            cancel_task = asyncio.create_task(routes_mod.cancel_user_processing(uid))
            await asyncio.sleep(0)

            routes_mod.user_processing_tasks[uid] = {
                "job_id": "newjob02",
                "shutdown_flag": threading.Event(),
            }
            exec_fut.set_result(None)
            await cancel_task

        asyncio.run(run())
        assert uid in routes_mod.user_processing_tasks
        assert routes_mod.user_processing_tasks[uid]["job_id"] == "newjob02"

    def test_noop_when_nothing_registered(self):
        async def run():
            await routes_mod.cancel_user_processing(99999)

        asyncio.run(run())
        assert 99999 not in routes_mod.user_processing_tasks
