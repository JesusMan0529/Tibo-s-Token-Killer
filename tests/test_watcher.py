import copy
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "plugins" / "codex-quota-watcher" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import watcher


def quota(used=20, weekly=30, allowed=True):
    return {"ordinaryUsageAllowed": allowed, "rateLimitsByLimitId": {"codex": {
        "limitId": "codex", "primary": {"usedPercent": used, "windowDurationMins": 300, "resetsAt": 1},
        "secondary": {"usedPercent": weekly, "windowDurationMins": 10080}}}}


class FakeClient:
    def __init__(self, limits=None, status="completed", answer=None, error=None, callback=None):
        self.limits = limits if limits is not None else quota()
        self.status = status
        self.answer = answer if answer is not None else json.dumps({"status": "completed", "summary": "已完成并验证"})
        self.error = error
        self.callback = callback
        self.calls = []
        self.events = []
        self.closed = False

    def request(self, method, params, timeout=30):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "account/rateLimits/read":
            if isinstance(self.limits, Exception):
                raise self.limits
            return self.limits
        if method in ("thread/start", "thread/resume"):
            return {"thread": {"id": params.get("threadId", "thread-1")}}
        if method == "turn/start":
            self.events = [
                {"method": "item/completed", "params": {"threadId": "thread-1", "item": {
                    "type": "agentMessage", "phase": "final_answer", "text": self.answer}}},
                {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
                    "id": "turn-1", "status": self.status, "error": self.error, "items": []}}},
            ]
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            return {}
        if method == "turn/steer":
            return {"turnId": "turn-1"}
        raise AssertionError(method)

    def next_event(self, timeout=1):
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
            return {}
        if self.events:
            return self.events.pop(0)
        raise queue.Empty

    def send(self, message):
        self.calls.append(("send", message))

    def close(self):
        self.closed = True


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = watcher.Store(Path(self.temp.name) / "state")
        self.workspace = Path(self.temp.name) / "中文项目"
        self.workspace.mkdir()

    def configured(self):
        with patch.object(watcher, "kick"):
            watcher.set_task(self.store, "完成指定任务，验证结果", self.workspace)
        watcher.set_switch(self.store, True, start=False)

    def run_tick(self, client):
        return watcher.tick(self.store, lambda *_: client)


class ContinuousTests(StoreCase):
    def continuous_task(self):
        watcher.set_task(self.store, "持续贡献，每轮一个 PR", self.workspace, continuous=True)
        watcher.set_switch(self.store, True, start=False)

    def client_with_limits(self, limits):
        client = FakeClient()
        original = client.request
        values = iter(limits)

        def request(method, params, timeout=30):
            if method == "account/rateLimits/read":
                client.limits = next(values)
            return original(method, params, timeout)

        client.request = request
        return client

    def test_two_project_cycles_continue_in_same_thread_until_quota_exhausted(self):
        self.continuous_task()
        client = self.client_with_limits([quota(), quota(), quota(used=100)])
        result = self.run_tick(client)
        self.assertEqual(result["status"], "waiting_quota")
        methods = [method for method, _ in client.calls]
        self.assertEqual(methods.count("turn/start"), 2)
        self.assertEqual(methods.count("account/rateLimits/read"), 3)
        self.assertEqual(methods.count("thread/start"), 1)
        self.assertEqual(methods.count("thread/resume"), 1)
        self.assertEqual(self.store.state()["completed_cycles"], 2)
        self.assertTrue(self.store.config()["enabled"])

    def test_query_failure_after_a_completed_cycle_waits_and_resumes(self):
        self.continuous_task()
        first = self.client_with_limits([quota(), TimeoutError("offline")])
        self.assertEqual(self.run_tick(first)["status"], "waiting_quota")
        second = self.client_with_limits([quota(), quota(used=100)])
        self.assertEqual(self.run_tick(second)["status"], "waiting_quota")
        self.assertIn("thread/resume", dict(second.calls))
        self.assertNotIn("thread/start", dict(second.calls))
        self.assertEqual(self.store.state()["completed_cycles"], 2)

    def test_explicit_stop_finishes_current_cycle_without_interrupt_or_next_cycle(self):
        self.continuous_task()
        client = FakeClient(callback=lambda: watcher.set_switch(self.store, False, start=False))
        result = self.run_tick(client)
        methods = [method for method, _ in client.calls]
        self.assertEqual(result["status"], "paused")
        self.assertEqual(methods.count("turn/start"), 1)
        self.assertEqual(methods.count("turn/steer"), 1)
        self.assertNotIn("turn/interrupt", methods)
        self.assertFalse(self.store.config()["enabled"])
        self.assertEqual(self.run_tick(FakeClient())["status"], "off")

    def test_stop_without_an_active_cycle_starts_no_model(self):
        self.continuous_task()
        watcher.set_switch(self.store, False, start=False)
        client = FakeClient()
        self.assertEqual(self.run_tick(client)["status"], "off")
        self.assertEqual(client.calls, [])
        self.assertFalse(self.store.config()["stop_requested"])

    def test_one_shot_task_still_completes_once(self):
        self.configured()
        client = FakeClient()
        self.assertEqual(self.run_tick(client)["status"], "completed")
        self.assertEqual(self.run_tick(FakeClient())["status"], "completed")
        self.assertEqual(self.store.state().get("completed_cycles", 0), 0)

    def test_blocked_continuous_cycle_stops_automatic_retries(self):
        self.continuous_task()
        client = FakeClient(answer=json.dumps({"status": "blocked", "summary": "login unavailable"}))
        self.assertEqual(self.run_tick(client)["status"], "blocked")
        self.assertEqual([method for method, _ in client.calls].count("turn/start"), 1)
        next_client = FakeClient()
        self.assertEqual(self.run_tick(next_client)["status"], "blocked")
        self.assertEqual(next_client.calls, [])

    def test_progress_is_visible_during_the_turn_and_does_not_complete_it(self):
        self.configured()
        client = FakeClient()
        original = client.request

        def request(method, params, timeout=30):
            result = original(method, params, timeout)
            if method == "turn/start":
                client.events.insert(0, {"method": "item/completed", "params": {
                    "threadId": "thread-1", "turnId": "turn-1", "item": {
                        "type": "agentMessage", "phase": "commentary", "text": "正在验证第二个项目"}}})
            return result

        client.request = request
        original_next = client.next_event
        observations = []

        def next_event(timeout=1):
            if self.store.state().get("message") == "正在验证第二个项目":
                observations.append(self.store.state()["status"])
            return original_next(timeout)

        client.next_event = next_event
        self.assertEqual(self.run_tick(client)["status"], "completed")
        self.assertIn("running", observations)
        self.assertIsNotNone(self.store.state()["last_activity_at"])

    def test_mode_changes_preserve_task_and_thread(self):
        self.configured()
        original_id = self.store.config()["task_id"]
        self.store.update_state(thread_id="existing-thread")
        watcher.set_mode(self.store, True)
        self.assertTrue(self.store.config()["continuous"])
        self.assertEqual(self.store.config()["task_id"], original_id)
        self.assertEqual(self.store.state()["thread_id"], "existing-thread")


class QuotaTests(unittest.TestCase):
    def test_positive_five_hour_and_weekly_quota(self):
        result = watcher.quota_snapshot(quota())
        self.assertTrue(result["can_run"])
        self.assertEqual(result["remaining_percent"], 80)
        self.assertEqual(result["other_remaining_percent"], [70])

    def test_no_recovery_inferred_from_expired_reset_timestamp(self):
        self.assertFalse(watcher.quota_snapshot(quota(used=100))["can_run"])

    def test_weekly_exhausted(self):
        self.assertFalse(watcher.quota_snapshot(quota(weekly=100))["can_run"])

    def test_official_permission_required_even_with_remaining_quota(self):
        for permission in (False, None):
            self.assertFalse(watcher.quota_snapshot(quota(allowed=permission))["can_run"])

    def test_unknown_or_wrong_window_fails_closed(self):
        data = quota()
        data["rateLimitsByLimitId"]["codex"]["primary"]["windowDurationMins"] = 15
        self.assertFalse(watcher.quota_snapshot(data)["can_run"])
        self.assertFalse(watcher.quota_snapshot({})["can_run"])

    def test_five_hour_window_can_be_secondary(self):
        data = quota()
        bucket = data["rateLimitsByLimitId"]["codex"]
        bucket["primary"], bucket["secondary"] = bucket["secondary"], bucket["primary"]
        self.assertEqual(watcher.quota_snapshot(data)["remaining_percent"], 80)

    def test_backend_block_overrides_percentages(self):
        for key, value in (("spendControlReached", True), ("rateLimitReachedType", "rate_limit_reached")):
            data = quota()
            data["rateLimitsByLimitId"]["codex"][key] = value
            self.assertFalse(watcher.quota_snapshot(data)["can_run"])

    def test_invalid_percentage_fails_closed(self):
        for used in (None, "0", True, -1, float("nan"), float("inf")):
            self.assertFalse(watcher.quota_snapshot(quota(used=used))["can_run"])

    def test_does_not_pick_an_unrelated_available_bucket(self):
        data = quota(used=100)
        data["rateLimitsByLimitId"]["codex_other"] = quota()["rateLimitsByLimitId"]["codex"]
        self.assertFalse(watcher.quota_snapshot(data)["can_run"])

    def test_legacy_bucket_supported_with_explicit_permission(self):
        self.assertTrue(watcher.quota_snapshot({"ordinaryUsageAllowed": True,
                                              "rateLimits": quota()["rateLimitsByLimitId"]["codex"]})["can_run"])


class WorkflowTests(StoreCase):
    def test_default_permissions_still_use_workspace_sandbox(self):
        self.configured()
        client = FakeClient()
        self.run_tick(client)
        calls = dict(client.calls)
        self.assertEqual(calls["thread/start"]["sandbox"], "workspace-write")
        self.assertEqual(calls["turn/start"]["sandboxPolicy"], {
            "type": "workspaceWrite", "writableRoots": [str(self.workspace)], "networkAccess": True})

    def test_explicit_host_permissions_apply_to_thread_and_turn(self):
        self.configured()
        watcher.set_sandbox(self.store, "danger-full-access")
        client = FakeClient()
        self.run_tick(client)
        calls = dict(client.calls)
        self.assertEqual(calls["thread/start"]["sandbox"], "danger-full-access")
        self.assertEqual(calls["turn/start"]["sandboxPolicy"], {"type": "dangerFullAccess"})

    def test_explicit_on_resumes_blocked_task_with_new_permissions_and_same_thread(self):
        self.configured()
        first = FakeClient(answer=json.dumps({"status": "blocked", "summary": "authentication unavailable"}))
        self.run_tick(first)
        task_id = self.store.config()["task_id"]
        watcher.set_sandbox(self.store, "danger-full-access")
        watcher.set_switch(self.store, True, start=False)
        client = FakeClient()
        self.assertEqual(self.run_tick(client)["status"], "completed")
        calls = dict(client.calls)
        self.assertNotIn("thread/start", calls)
        self.assertEqual(calls["thread/resume"]["threadId"], "thread-1")
        self.assertEqual(calls["thread/resume"]["sandbox"], "danger-full-access")
        self.assertEqual(self.store.config()["task_id"], task_id)

    def test_explicit_on_can_resume_failed_and_review_states(self):
        for status in ("failed", "needs_review"):
            with self.subTest(status=status):
                self.configured()
                self.store.update_state(task_id=self.store.config()["task_id"], status=status, thread_id="thread-1")
                watcher.set_switch(self.store, True, start=False)
                self.assertEqual(self.store.state()["status"], "waiting_quota")
                self.assertEqual(self.run_tick(FakeClient())["status"], "completed")

    def test_on_does_not_repeat_completed_task(self):
        self.configured()
        self.run_tick(FakeClient())
        watcher.set_switch(self.store, True, start=False)
        client = FakeClient()
        self.assertEqual(self.run_tick(client)["status"], "completed")
        self.assertEqual(client.calls, [])

    def test_invalid_permissions_never_change_configuration(self):
        self.configured()
        original = self.store.config()
        with self.assertRaises(ValueError):
            watcher.set_sandbox(self.store, "invalid")
        self.assertEqual(self.store.config(), original)

    def test_default_off_performs_no_quota_or_model_calls(self):
        client = FakeClient()
        self.assertEqual(self.run_tick(client)["status"], "off")
        self.assertEqual(client.calls, [])

    def test_exhausted_quota_never_starts_a_turn(self):
        self.configured()
        client = FakeClient(limits=quota(used=100))
        self.assertEqual(self.run_tick(client)["status"], "waiting_quota")
        self.assertEqual([method for method, _ in client.calls], ["account/rateLimits/read"])
        self.assertTrue(client.closed)

    def test_quota_recovery_automatically_starts_the_pending_task(self):
        self.configured()
        self.assertEqual(self.run_tick(FakeClient(limits=quota(used=100)))["status"], "waiting_quota")
        available = FakeClient(limits=quota(used=0))
        self.assertEqual(self.run_tick(available)["status"], "completed")
        self.assertEqual([method for method, _ in available.calls].count("turn/start"), 1)

    def test_query_failure_waits_without_execution(self):
        self.configured()
        client = FakeClient(limits=TimeoutError("offline"))
        self.assertEqual(self.run_tick(client)["status"], "waiting_quota")
        self.assertNotIn("turn/start", [method for method, _ in client.calls])

    def test_no_task_waits_for_human_configuration(self):
        watcher.set_switch(self.store, True, start=False)
        client = FakeClient()
        self.assertEqual(self.run_tick(client)["status"], "awaiting_task")
        self.assertNotIn("turn/start", [method for method, _ in client.calls])

    def test_completed_task_is_not_repeated_even_if_quota_query_would_fail(self):
        self.configured()
        self.assertEqual(self.run_tick(FakeClient())["status"], "completed")
        second = FakeClient(limits=TimeoutError("offline"))
        self.assertEqual(self.run_tick(second)["status"], "completed")
        self.assertEqual(second.calls, [])

    def test_usage_limit_failure_resumes_same_thread(self):
        self.configured()
        first = FakeClient(status="failed", error={"message": "quota gone", "codexErrorInfo": "usageLimitExceeded"})
        self.assertEqual(self.run_tick(first)["status"], "waiting_quota")
        second = FakeClient()
        self.assertEqual(self.run_tick(second)["status"], "completed")
        calls = dict(second.calls)
        self.assertNotIn("thread/start", calls)
        self.assertEqual(calls["thread/resume"]["threadId"], "thread-1")

    def test_nonquota_failure_stops_retries(self):
        self.configured()
        client = FakeClient(status="failed", error={"message": "bad request", "codexErrorInfo": "badRequest"})
        self.assertEqual(self.run_tick(client)["status"], "failed")
        second = FakeClient()
        self.assertEqual(self.run_tick(second)["status"], "failed")
        self.assertEqual(second.calls, [])

    def test_self_reported_blocked_is_not_marked_complete(self):
        self.configured()
        client = FakeClient(answer=json.dumps({"status": "blocked", "summary": "需要额外信息"}))
        self.assertEqual(self.run_tick(client)["status"], "blocked")

    def test_missing_completion_result_stops_instead_of_repeating(self):
        self.configured()
        client = FakeClient(answer="没有明确的 JSON 结果")
        self.assertEqual(self.run_tick(client)["status"], "needs_review")
        self.assertEqual(self.run_tick(FakeClient())["status"], "needs_review")

    def test_switch_off_interrupts_running_turn(self):
        self.configured()
        client = FakeClient(callback=lambda: watcher.set_switch(self.store, False, start=False))
        self.assertEqual(self.run_tick(client)["status"], "paused")
        self.assertIn("turn/interrupt", dict(client.calls))
        self.assertFalse(self.store.config()["enabled"])

    def test_setting_new_task_during_run_interrupts_old_turn(self):
        self.configured()
        with patch.object(watcher, "kick"):
            client = FakeClient(callback=lambda: watcher.set_task(self.store, "新目标", self.workspace))
            self.assertEqual(self.run_tick(client)["status"], "paused")
        self.assertIn("turn/interrupt", dict(client.calls))
        self.assertEqual(watcher.get_status(self.store)["status"], "pending")
        self.assertEqual(self.run_tick(FakeClient())["status"], "completed")

    def test_repeated_on_does_not_interrupt_running_task(self):
        self.configured()
        revision = self.store.config()["revision"]
        watcher.set_switch(self.store, True, start=False)
        self.assertEqual(self.store.config()["revision"], revision)

    def test_unknown_worker_exit_requires_review(self):
        self.configured()
        self.store.update_state(task_id=self.store.config()["task_id"], status="running", thread_id="thread-1")
        client = FakeClient()
        self.assertEqual(self.run_tick(client)["status"], "needs_review")
        self.assertEqual(client.calls, [])

    def test_new_task_resets_completed_progress(self):
        self.configured()
        self.run_tick(FakeClient())
        with patch.object(watcher, "kick"):
            watcher.set_task(self.store, "第二个目标", self.workspace)
        self.assertEqual(self.run_tick(FakeClient())["status"], "completed")

    def test_os_lock_prevents_concurrent_launches(self):
        self.configured()
        with watcher.file_lock(self.store.home / "runner.lock"):
            client = FakeClient()
            self.assertEqual(self.run_tick(client)["status"], "already_running")
            self.assertEqual(client.calls, [])

    def test_invalid_task_does_not_change_configuration(self):
        original = self.store.config()
        for prompt, directory in (("", self.workspace), ("目标", ""), ("目标", self.workspace / "missing")):
            with self.assertRaises(ValueError):
                watcher.set_task(self.store, prompt, directory)
        self.assertEqual(self.store.config(), original)

    @unittest.skipUnless(os.name == "nt", "Windows Task Scheduler integration")
    def test_immediate_check_uses_installed_scheduler(self):
        with patch.dict(os.environ):
            os.environ.pop("CODEX_QUOTA_WATCHER_HOME", None)
            with patch.object(watcher, "default_home", return_value=self.store.home), \
                    patch.object(watcher.subprocess, "run") as run, \
                    patch.object(watcher.subprocess, "Popen") as popen:
                run.return_value.returncode = 0
                watcher.kick(self.store)
                self.assertEqual(run.call_args.args[0], ["schtasks.exe", "/Run", "/TN", watcher.SCHEDULED_TASK_NAME])
                popen.assert_not_called()

    def test_fallback_worker_uses_the_requested_state_directory(self):
        with patch.object(watcher.subprocess, "Popen") as popen:
            watcher.kick(self.store)
        self.assertEqual(popen.call_args.kwargs["env"]["CODEX_QUOTA_WATCHER_HOME"], str(self.store.home))


class HookTests(StoreCase):
    def test_exact_phrases_toggle_and_do_not_call_model(self):
        with patch.object(watcher, "kick") as kick:
            output = watcher.handle_hook(self.store, {"hook_event_name": "UserPromptSubmit", "prompt": "  不要让Tibo发现\n"})
            self.assertTrue(self.store.config()["enabled"])
            self.assertEqual(output["decision"], "block")
            kick.assert_called_once()
        watcher.handle_hook(self.store, {"hook_event_name": "UserPromptSubmit", "prompt": "Tibo要按按钮了"})
        self.assertFalse(self.store.config()["enabled"])

    def test_mentions_quotes_and_different_case_do_not_trigger(self):
        for prompt in ("当我说 不要让Tibo发现 时开启", '"不要让Tibo发现"', "不要让tibo发现", "LiHaoDong521", "bobo5210", "Tibo要按按钮了 后继续", "`不要让Tibo发现`"):
            self.assertIsNone(watcher.handle_hook(self.store, {"hook_event_name": "UserPromptSubmit", "prompt": prompt}))
        self.assertFalse(self.store.config()["enabled"])

    def test_wrong_event_does_not_trigger(self):
        self.assertIsNone(watcher.handle_hook(self.store, {"hook_event_name": "Stop", "prompt": "不要让Tibo发现"}))


class TransportTests(StoreCase):
    def test_windows_child_inherits_system_proxy_without_mutating_parent(self):
        with patch.object(watcher.os, "name", "nt"), \
                patch.dict(os.environ, {"NO_PROXY": "localhost"}, clear=True), \
                patch.object(watcher.urllib.request, "getproxies", return_value={
                    "http": "http://localhost:7897", "https": "http://localhost:7897",
                    "ftp": "http://localhost:7897"}):
            env = watcher.app_server_environment()
            self.assertEqual(env["HTTP_PROXY"], "http://localhost:7897")
            self.assertEqual(env["HTTPS_PROXY"], "http://localhost:7897")
            self.assertEqual(env["NO_PROXY"], "localhost")
            self.assertNotIn("FTP_PROXY", env)
            self.assertNotIn("HTTP_PROXY", os.environ)

    def test_explicit_proxy_environment_takes_precedence(self):
        for key in ("HTTP_PROXY", "https_proxy", "ALL_PROXY"):
            with self.subTest(key=key), patch.object(watcher.os, "name", "nt"), \
                    patch.dict(os.environ, {key: "http://localhost:8888"}, clear=True), \
                    patch.object(watcher.urllib.request, "getproxies") as getproxies:
                self.assertEqual(watcher.app_server_environment(), dict(os.environ))
                getproxies.assert_not_called()

    def test_no_system_proxy_leaves_environment_unchanged(self):
        with patch.object(watcher.os, "name", "nt"), \
                patch.dict(os.environ, {"PATH": "test"}, clear=True), \
                patch.object(watcher.urllib.request, "getproxies", return_value={}):
            self.assertEqual(watcher.app_server_environment(), {"PATH": "test"})

    def test_controller_disables_native_goal_continuation_only_in_child_process(self):
        command = [sys.executable, str(Path(__file__).with_name("fake_app_server.py"))]
        real_popen = watcher.subprocess.Popen
        launches = []

        def launch(args, **kwargs):
            launches.append(args)
            return real_popen(command, **kwargs)

        original = self.store.config()
        with patch.object(watcher, "find_codex", return_value="codex.exe"), \
                patch.object(watcher.subprocess, "Popen", side_effect=launch):
            client = watcher.AppServer(self.store, original)
            try:
                self.assertTrue(watcher.quota_snapshot(client.request("account/rateLimits/read", {}))["can_run"])
            finally:
                client.close()
        self.assertEqual(launches, [["codex.exe", "-c", "features.goals=false", "app-server"]])
        self.assertEqual(self.store.config(), original)

    def client(self):
        command = [sys.executable, str(Path(__file__).with_name("fake_app_server.py"))]
        client = watcher.AppServer(self.store, self.store.config(), command=command)
        self.addCleanup(client.close)
        return client

    def test_real_child_process_and_notification_interleaving(self):
        client = self.client()
        response = client.request("account/rateLimits/read", {})
        self.assertEqual(watcher.quota_snapshot(response)["remaining_percent"], 75)
        self.assertEqual(client.next_event()["params"]["text"], "中文")

    def test_rpc_error_is_surfaced(self):
        with self.assertRaises(watcher.RpcError):
            self.client().request("test/error", {})

    def test_request_timeout_is_bounded(self):
        with self.assertRaises(TimeoutError):
            self.client().request("test/timeout", {}, timeout=0.05)


class PanelTests(StoreCase):
    def test_dashboard_distinguishes_actual_work_sleep_stop_and_stale_state(self):
        from dashboard import display_state
        for status, enabled, alive, label in (
                ("running", True, True, "工作中"),
                ("running", True, False, "等待检查"),
                ("waiting_quota", True, False, "休眠中"),
                ("stopping", False, True, "正在收尾"),
                ("blocked", True, False, "等待处理"),
                ("paused", False, False, "已关闭")):
            with self.subTest(status=status):
                self.assertEqual(display_state({"status": status, "enabled": enabled}, alive)[0], label)

    def test_dashboard_reads_live_progress_incrementally_and_waits_for_complete_lines(self):
        from dashboard import ActivityReader
        path = self.workspace / "activity.jsonl"
        record = {"timestamp": "2026-10-03T04:00:01+00:00", "type": "response_item", "payload": {
            "type": "message", "phase": "commentary", "content": [{"type": "output_text", "text": "正在构建"}]}}
        text = json.dumps(record, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        reader = ActivityReader()
        started = "2026-10-03T04:00:00+00:00"
        reader.read(str(path), started)
        self.assertEqual(reader.message, "")
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        reader.read(str(path), started)
        self.assertEqual(reader.message, "正在构建")
        offset = reader.offset
        reader.read(str(path), started)
        self.assertEqual(reader.offset, offset)
        reader.read(str(path), "2026-10-03T04:01:00+00:00")
        self.assertEqual(reader.message, "")

    def test_actual_dashboard_shows_worker_progress_without_mutating_config(self):
        import tkinter as tk
        from dashboard import Dashboard
        self.configured()
        config = self.store.config()
        self.store.update_state(task_id=config["task_id"], status="running", message="正在验证示例")
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        with watcher.file_lock(self.store.home / "runner.lock"):
            panel = Dashboard(root, self.store)
            root.update_idletasks()
        self.assertEqual(panel.state.get(), "工作中")
        self.assertEqual(panel.progress.get(), "正在验证示例")
        self.assertEqual(self.store.config(), config)

    def test_actual_tk_panel_initialization_and_controls(self):
        import tkinter as tk
        from panel import Panel
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        panel = Panel(root, self.store)
        root.update_idletasks()
        self.assertIn("监控：关闭", panel.status.get())
        with patch.object(watcher, "kick"):
            panel.switch(True)
        self.assertTrue(self.store.config()["enabled"])
        panel.switch(False)
        self.assertFalse(self.store.config()["enabled"])


if __name__ == "__main__":
    unittest.main()
