"""Local Codex quota controller. Uses only Python's standard library."""

from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid


VERSION = "1.0.4"
INTERVAL_SECONDS = 15 * 60
ON_PHRASE = "不要让Tibo发现"
OFF_PHRASE = "Tibo要按按钮了"
SCHEDULED_TASK_NAME = "CodexQuotaWatcher-LiHaoDong"
FINISHED_STATUSES = {"completed", "blocked", "failed", "needs_review"}
SANDBOX_MODES = ("workspace-write", "danger-full-access")
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked"]},
        "summary": {"type": "string"},
    },
    "required": ["status", "summary"],
    "additionalProperties": False,
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_home():
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return Path(os.environ.get("CODEX_QUOTA_WATCHER_HOME", str(codex_home / "quota-watcher")))


class LockBusy(Exception):
    pass


@contextmanager
def file_lock(path, wait_seconds=0):
    """OS locks are released on process death, unlike stale PID files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + wait_seconds
    locked = False
    try:
        while not locked:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockBusy(str(path)) from None
                time.sleep(0.05)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def read_json(path, default):
    if not path.exists():
        return dict(default)
    with path.open(encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"配置必须是 JSON 对象：{path}")
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


class Store:
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else default_home()
        self.home.mkdir(parents=True, exist_ok=True)

    def config(self):
        defaults = {"enabled": False, "revision": 0, "task_id": None,
                    "prompt": "", "cwd": "", "codex_path": "",
                    "sandbox_mode": "workspace-write", "continuous": False,
                    "stop_requested": False}
        return defaults | read_json(self.home / "config.json", {})

    def state(self):
        return read_json(self.home / "state.json", {})

    def update_config(self, **changes):
        with file_lock(self.home / "config.lock", 4):
            config = self.config()
            config.update(changes)
            config["revision"] += 1
            write_json(self.home / "config.json", config)
        return config

    def update_state(self, **changes):
        with file_lock(self.home / "state.lock", 4):
            state = self.state()
            state.update(changes)
            write_json(self.home / "state.json", state)
        return state

    def event(self, event, **details):
        with file_lock(self.home / "events.lock", 4):
            with (self.home / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"time": now(), "event": event, **details},
                                        ensure_ascii=False) + "\n")


def find_codex(configured=""):
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    executable = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if executable:
        return executable
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        candidates = sorted(root.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime,
                            reverse=True)
        if candidates:
            return str(candidates[0])
    raise RuntimeError("找不到 Codex CLI；请安装 Codex，或重新运行安装.ps1。")


class RpcError(RuntimeError):
    def __init__(self, error):
        self.error = error
        super().__init__(error.get("message", "App Server 请求失败"))


def app_server_environment():
    env = os.environ.copy()
    proxy_keys = {"http_proxy", "https_proxy", "all_proxy"}
    if os.name == "nt" and not any(key.lower() in proxy_keys for key in env):
        # Scheduled tasks do not inherit shell-local proxy variables. Match
        # the user's Windows proxy for this child without changing settings.
        for scheme, url in urllib.request.getproxies().items():
            if scheme in ("http", "https"):
                env[f"{scheme.upper()}_PROXY"] = url
    return env


class AppServer:
    """Line-delimited JSON-RPC over stdio, with bounded request waits."""
    def __init__(self, store, config, command=None):
        self.messages = queue.Queue()
        self.notifications = deque()
        self.next_id = 0
        self.stderr = (store.home / "app-server.stderr.log").open("a", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                # This controller owns retries and turn results. Native goal
                # continuation can start a second turn during thread/resume.
                command or [find_codex(config["codex_path"]), "-c",
                            "features.goals=false", "app-server"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=app_server_environment(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            self.stderr.close()
            raise
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"clientInfo": {
                "name": "codex_quota_watcher", "title": "Codex 额度任务助手",
                "version": VERSION,
            }})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.messages.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def send_request(self, method, params):
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        return self.next_id

    def request(self, method, params, timeout=30):
        request_id = self.send_request(method, params)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"App Server 请求超时：{method}")
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"App Server 请求超时：{method}") from None
            if message is None:
                raise RuntimeError("App Server 已退出，请查看 app-server.stderr.log。")
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RpcError(message["error"])
                return message["result"]
            if "method" in message:
                self.notifications.append(message)

    def next_event(self, timeout=1):
        message = self.notifications.popleft() if self.notifications else self.messages.get(timeout=timeout)
        if message is None:
            raise RuntimeError("任务执行时 App Server 退出，需人工检查后重新设置任务。")
        return message

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        self.reader.join(timeout=1)
        self.process.stdout.close()
        if not self.process.stdin.closed:
            self.process.stdin.close()
        self.stderr.close()


def quota_snapshot(response):
    """Only fresh server permission and a positively identified 5h window unlock work."""
    buckets = response.get("rateLimitsByLimitId") or {}
    bucket = buckets.get("codex")
    if bucket is None:
        legacy = response.get("rateLimits") or {}
        if legacy.get("limitId") in (None, "codex"):
            bucket = legacy
    bucket = bucket or {}
    windows = [w for w in (bucket.get("primary"), bucket.get("secondary")) if w]

    def remaining(window):
        used = window.get("usedPercent")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
            return None
        return max(0, 100 - used)

    five_hour = next((w for w in windows if w.get("windowDurationMins") == 300), None)
    five_left = remaining(five_hour) if five_hour else None
    other_windows = [w for w in windows if w is not five_hour]
    other_left = [remaining(w) for w in other_windows]
    reason = "额度可用"
    if response.get("ordinaryUsageAllowed") is not True:
        reason = "官方尚未确认可使用套餐额度，继续等待"
    elif five_left is None:
        reason = "未读取到明确的 5 小时额度，继续等待"
    elif five_left == 0:
        reason = "5 小时额度为 0，继续等待"
    elif any(value is None for value in other_left):
        reason = "其他额度窗口的数据不完整，继续等待"
    elif any(value == 0 for value in other_left):
        reason = "其他额度窗口（例如周额度）已耗尽，继续等待"
    elif bucket.get("spendControlReached") is True or bucket.get("rateLimitReachedType"):
        reason = "官方仍报告额度限制，继续等待"
    return {
        "checked_at": now(), "remaining_percent": five_left,
        "other_remaining_percent": other_left,
        "reset_at": five_hour.get("resetsAt") if five_hour else None,
        "can_run": reason == "额度可用", "reason": reason,
    }


def is_quota_error(error):
    tag = error.get("codexErrorInfo")
    if tag in ("usageLimitExceeded", "rateLimitExceeded"):
        return True
    if isinstance(tag, dict) and any(isinstance(v, dict) and v.get("httpStatusCode") == 429
                                     for v in tag.values()):
        return True
    message = error.get("message", "").lower()
    return "usage limit" in message or "rate limit" in message


def active(store, config):
    latest = store.config()
    return latest["enabled"] is True and same_task(latest, config)


def same_task(latest, config):
    return all(latest.get(key) == config.get(key) for key in
               ("task_id", "prompt", "cwd", "sandbox_mode", "continuous"))


def final_text(items, previous=""):
    for item in items:
        if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
            previous = item.get("text", previous)
    return previous


def execute_task(store, config, client):
    state = store.state()
    mode = config.get("sandbox_mode", "workspace-write")
    if mode not in SANDBOX_MODES:
        raise ValueError(f"不支持的执行权限：{mode}")
    policy = ({"type": "dangerFullAccess"} if mode == "danger-full-access" else
              {"type": "workspaceWrite", "writableRoots": [config["cwd"]], "networkAccess": True})
    params = {"cwd": config["cwd"], "approvalPolicy": "never", "sandbox": mode}
    if state.get("thread_id"):
        params["threadId"] = state["thread_id"]
        thread = client.request("thread/resume", params)["thread"]
    else:
        thread = client.request("thread/start", params)["thread"]
    thread_id = thread["id"]
    store.update_state(thread_id=thread_id, thread_path=thread.get("path"))
    if not active(store, config):
        return {"status": "paused", "message": "已关闭或更新任务，停止执行"}
    prompt = (
        "执行以下用户明确设定的目标任务，遵守工作目录的 AGENTS.md 和任务本身的约束。"
        "如存在先前执行记录，先检查已完成的工作并从中断处继续，避免重复产生副作用。"
        "完成必要验证后才报告 completed；需要用户输入或无法完成时报告 blocked，说明原因。"
        "最终按指定 JSON 格式返回 status 和 summary。\n\n用户目标：\n" + config["prompt"]
    )
    if config["continuous"]:
        prompt += (
            "\n\n这是持续任务的一轮。先从断点完成当前阶段，记录断点后返回 completed。"
            "对于持续 PR 贡献，本轮只完成一个项目的正式 PR、独立评论及发布核验。"
            "候选不合适时记录原因并继续筛选，不把跳过候选当作本轮完成。"
            "completed 仅代表本轮完成，控制器会重新检查额度并立即开始下一轮，"
            "不要把整个持续目标报告为完成，不要在本轮开始下一个项目。"
        )
    # Persist intent first: a crash after submission must not launch the task again.
    store.update_state(status="running", started_at=now(), message="正在启动目标任务")
    turn = client.request("turn/start", {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
        "approvalPolicy": "never",
        "sandboxPolicy": policy,
        "outputSchema": RESULT_SCHEMA,
    })["turn"]
    turn_id = turn["id"]
    store.update_state(status="running", turn_id=turn_id, started_at=now(),
                       runner_version=VERSION, last_activity_at=now(), message="正在执行目标任务")
    store.event("task_started", task_id=config["task_id"], thread_id=thread_id)
    answer = ""
    finished = False
    stop_sent = False
    try:
        while True:
            if not active(store, config):
                latest = store.config()
                if (config["continuous"] and latest["stop_requested"]
                        and same_task(latest, config)):
                    if not stop_sent:
                        stop_sent = True
                        store.update_state(status="stopping", message="已请求停止，完成当前项目后结束")
                        try:
                            client.request("turn/steer", {
                                "threadId": thread_id, "expectedTurnId": turn_id,
                                "input": [{"type": "text", "text":
                                    "用户已明确要求停止持续任务。如果已有正在处理的 issue/PR，"
                                    "完成当前项目的修复、必要验证、PR 和独立评论后结束；"
                                    "不要开始新项目。如果还没有进行中的 issue/PR，立即结束。"
                                    "保存断点，按原 JSON 格式返回本轮结果。"}],
                            })
                        except RpcError as error:
                            # A completed turn may race this request. In either
                            # case, the controller will not start another cycle.
                            store.event("stop_steer_error", message=str(error))
                else:
                    return {"status": "paused", "message": "已关闭或更新任务，停止执行"}
            try:
                event = client.next_event(timeout=1)
            except queue.Empty:
                continue
            if "id" in event and "method" in event:
                client.send({"id": event["id"], "error": {
                    "code": -32601, "message": "Unattended task cannot answer interactive requests"}})
                return {"status": "blocked", "message": "任务要求交互输入，请人工处理后重新设置任务"}
            payload = event.get("params", {})
            if payload.get("threadId") not in (None, thread_id):
                continue
            if payload.get("turnId") not in (None, turn_id):
                continue
            if event.get("method") == "item/completed":
                item = payload.get("item", {})
                changes = {"last_activity_at": now()}
                if item.get("type") == "agentMessage" and item.get("phase") == "commentary":
                    text = item.get("text", "").strip()
                    if text:
                        changes["message"] = text
                        store.event("task_progress", thread_id=thread_id, turn_id=turn_id, message=text)
                store.update_state(**changes)
                answer = final_text([item], answer)
            if event.get("method") != "turn/completed" or payload.get("turn", {}).get("id") != turn_id:
                continue
            finished = True
            result = payload["turn"]
            if result["status"] == "failed":
                error = result.get("error") or {}
                return {"status": "waiting_quota" if is_quota_error(error) else "failed",
                        "message": error.get("message", "Codex 执行失败")}
            if result["status"] == "interrupted":
                return {"status": "paused", "message": "任务已中断"}
            answer = final_text(result.get("items", []), answer)
            try:
                output = json.loads(answer)
                if not isinstance(output, dict) or output.get("status") not in ("completed", "blocked") or not isinstance(output.get("summary"), str):
                    raise ValueError("Unexpected task result")
            except (ValueError, TypeError):
                return {"status": "needs_review", "message": "未获得明确的任务完成结果，请人工检查",
                        "last_output": answer}
            return {"status": output["status"], "message": output["summary"], "last_output": answer}
    finally:
        if not finished:
            try:
                client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=5)
            except (RuntimeError, TimeoutError, OSError):
                pass


def tick(store, client_factory=AppServer):
    try:
        with file_lock(store.home / "runner.lock"):
            config = store.config()
            if config["enabled"] is not True:
                return {"status": "off"}
            state = store.state()
            if state.get("task_id") != config["task_id"]:
                store.update_state(task_id=config["task_id"], thread_id=None, turn_id=None,
                                   thread_path=None, completed_cycles=0, last_activity_at=None,
                                   status="waiting_quota", message="等待额度", last_output="")
            state = store.state()
            if state.get("status") in FINISHED_STATUSES and not (
                    config["continuous"] and state["status"] == "completed"):
                return {"status": state["status"]}
            if state.get("status") in ("running", "stopping"):
                result = {"status": "needs_review", "message": "上次执行意外退出，请检查执行记录后重新保存任务"}
                store.update_state(**result)
                return result
            client = None
            executing = False
            try:
                client = client_factory(store, config)
                while True:
                    executing = False
                    quota = quota_snapshot(client.request("account/rateLimits/read", {}))
                    store.update_state(quota=quota, last_check=now())
                    if not active(store, config):
                        result = {"status": "paused", "message": "已关闭或更新任务，停止执行"}
                        break
                    if not config["prompt"]:
                        result = {"status": "awaiting_task", "message": "已开启，请人工设置目标任务"}
                        break
                    if not quota["can_run"]:
                        result = {"status": "waiting_quota", "message": quota["reason"]}
                        break
                    executing = True
                    result = execute_task(store, config, client)
                    if result["status"] != "completed" or not config["continuous"]:
                        break
                    cycles = store.state().get("completed_cycles", 0) + 1
                    store.update_state(status="waiting_quota", completed_cycles=cycles,
                                       last_output=result["last_output"],
                                       message="本轮已完成，检查额度后继续下一项目")
                    store.event("cycle_completed", completed_cycles=cycles, message=result["message"])
                    if not active(store, config):
                        result = {"status": "paused", "message": "当前项目已完成，持续任务已停止",
                                  "last_output": result["last_output"]}
                        break
            except Exception as error:
                rpc_error = error.error if isinstance(error, RpcError) else {"message": str(error)}
                status = "waiting_quota" if not executing or is_quota_error(rpc_error) else "needs_review"
                result = {"status": status, "message": str(error)}
            finally:
                if client is not None:
                    client.close()
            store.update_state(**result, finished_at=now())
            store.event("tick_result", **result)
            return result
    except LockBusy:
        return {"status": "already_running"}


def kick(store):
    """Immediate first check; subsequent checks belong to Windows Task Scheduler."""
    if (os.name == "nt" and not os.environ.get("CODEX_QUOTA_WATCHER_HOME")
            and store.home.resolve() == default_home().resolve()):
        try:
            result = subprocess.run(
                ["schtasks.exe", "/Run", "/TN", SCHEDULED_TASK_NAME],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    with (store.home / "controller.log").open("a", encoding="utf-8") as log:
        env = os.environ.copy()
        env["CODEX_QUOTA_WATCHER_HOME"] = str(store.home)
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "tick"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            env=env,
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )


def set_switch(store, enabled, start=True):
    config = store.config()
    state = store.state()
    finishing = (not enabled and config["continuous"]
                 and state.get("task_id") == config["task_id"]
                 and state.get("status") in ("running", "stopping"))
    if config["enabled"] is not enabled or config["stop_requested"] != finishing:
        config = store.update_config(enabled=enabled, stop_requested=finishing)
    if finishing:
        store.update_state(status="stopping", message="已请求停止，完成当前项目后结束")
    if (enabled and state.get("task_id") == config["task_id"]
            and state.get("status") in {"blocked", "failed", "needs_review"}):
        store.update_state(status="waiting_quota", finished_at=None,
                           message="已请求恢复原任务，等待额度检查")
        store.event("resume_requested", task_id=config["task_id"], thread_id=state.get("thread_id"))
    store.event("enabled" if enabled else "disabled")
    if enabled and start:
        kick(store)
    if finishing:
        message = "已请求停止；完成当前正在处理的项目后结束，不再开始新项目"
    elif not enabled:
        message = "额度任务助手已关闭；正在运行的插件任务将中断"
    elif not config["prompt"]:
        message = "额度任务助手已开启，请先设置目标任务"
    else:
        message = "额度任务助手已开启，已安排立即检查，之后每 15 分钟检查一次"
    return {"enabled": enabled, "message": message}


def set_task(store, prompt, cwd, continuous=False):
    prompt = prompt.strip()
    if not str(cwd).strip():
        raise ValueError("请指定任务工作目录")
    directory = Path(cwd).expanduser().resolve()
    if not prompt:
        raise ValueError("目标任务不能为空")
    if not directory.is_dir():
        raise ValueError("工作目录不存在或不是文件夹")
    config = store.update_config(task_id=str(uuid.uuid4()), prompt=prompt, cwd=str(directory),
                                 continuous=continuous, stop_requested=False)
    store.event("task_configured", task_id=config["task_id"], cwd=str(directory))
    if config["enabled"]:
        kick(store)
    return {"task_id": config["task_id"], "cwd": str(directory), "message": "目标任务已保存"}


def set_sandbox(store, mode):
    if mode not in SANDBOX_MODES:
        raise ValueError(f"不支持的执行权限：{mode}")
    config = store.config()
    if config["sandbox_mode"] != mode:
        config = store.update_config(sandbox_mode=mode)
    store.event("sandbox_configured", sandbox_mode=mode)
    return {"sandbox_mode": mode, "task_id": config["task_id"], "message": "执行权限已保存，任务进度保留"}


def set_mode(store, continuous):
    config = store.config()
    if config["continuous"] != continuous:
        store.update_config(continuous=continuous)
    store.event("mode_configured", continuous=continuous)
    return {"continuous": continuous, "task_id": config["task_id"], "message": "执行模式已保存，任务进度保留"}


def get_status(store):
    config, state = store.config(), store.state()
    if state.get("task_id") != config["task_id"]:
        state = {"quota": state.get("quota"), "status": "awaiting_task" if not config["prompt"] else "pending"}
    return {"enabled": config["enabled"], "interval_minutes": 15,
            "prompt": config["prompt"], "cwd": config["cwd"], "data_dir": str(store.home),
            "sandbox_mode": config["sandbox_mode"],
            "continuous": config["continuous"], "stop_requested": config["stop_requested"],
            **state}


def check(store):
    client = AppServer(store, store.config())
    try:
        quota = quota_snapshot(client.request("account/rateLimits/read", {}))
        store.update_state(quota=quota, last_check=now())
        return quota
    finally:
        client.close()


def handle_hook(store, payload):
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return None
    prompt = payload.get("prompt", "").strip()
    if prompt not in (ON_PHRASE, OFF_PHRASE):
        return None
    result = set_switch(store, prompt == ON_PHRASE)
    return {"decision": "block", "reason": result["message"], "systemMessage": result["message"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Codex 额度任务助手")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("on", "off", "status", "check", "tick", "hook", "gui", "dashboard"):
        commands.add_parser(name)
    task = commands.add_parser("set-task")
    task.add_argument("--cwd", required=True)
    task.add_argument("--prompt-file", required=True, help="UTF-8 文本文件，或 - 从 stdin 读取")
    task.add_argument("--continuous", action="store_true", help="每轮完成后继续，直到明确关闭")
    mode = commands.add_parser("set-mode")
    mode.add_argument("--mode", choices=("once", "continuous"), required=True)
    sandbox = commands.add_parser("set-sandbox")
    sandbox.add_argument("--mode", choices=SANDBOX_MODES, required=True)
    init = commands.add_parser("init")
    init.add_argument("--codex-path", default="")
    args = parser.parse_args(argv)
    store = Store()
    if args.command in ("on", "off"):
        result = set_switch(store, args.command == "on")
    elif args.command == "set-task":
        prompt = sys.stdin.read() if args.prompt_file == "-" else Path(args.prompt_file).read_text(encoding="utf-8-sig")
        result = set_task(store, prompt, args.cwd, continuous=args.continuous)
    elif args.command == "set-mode":
        result = set_mode(store, args.mode == "continuous")
    elif args.command == "set-sandbox":
        result = set_sandbox(store, args.mode)
    elif args.command == "init":
        codex_path = find_codex(args.codex_path)
        store.update_config(codex_path=codex_path)
        result = get_status(store)
    elif args.command == "hook":
        result = handle_hook(store, json.load(sys.stdin))
    elif args.command == "gui":
        from panel import show
        show(store)
        return 0
    elif args.command == "dashboard":
        from dashboard import show
        show(store)
        return 0
    else:
        result = {"tick": tick, "status": get_status, "check": check}[args.command](store)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
