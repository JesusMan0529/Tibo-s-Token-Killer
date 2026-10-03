"""Read-only live desktop dashboard; never starts a model or changes a task."""

from datetime import datetime
import json
from pathlib import Path
import tkinter as tk

import watcher


class ActivityReader:
    def __init__(self):
        self.path = None
        self.offset = 0
        self.message = ""
        self.updated_at = None
        self.started_at = None

    def read(self, path, started_at):
        if not path:
            return
        if path != self.path:
            self.path, self.offset = path, 0
            self.message, self.updated_at = "", None
        if started_at != self.started_at:
            self.started_at = started_at
            self.message, self.updated_at = "", None
        file = Path(path)
        if not file.is_file():
            return
        if file.stat().st_size < self.offset:
            self.offset = 0
            self.message, self.updated_at = "", None
        with file.open(encoding="utf-8") as handle:
            handle.seek(self.offset)
            while True:
                line = handle.readline()
                if not line.endswith("\n"):
                    break
                self.offset = handle.tell()
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                timestamp = record.get("timestamp", "")
                if started_at and datetime.fromisoformat(timestamp) < datetime.fromisoformat(started_at):
                    continue
                payload = record.get("payload", {})
                if record.get("type") != "response_item":
                    continue
                self.updated_at = timestamp
                if payload.get("type") == "message" and payload.get("phase") == "commentary":
                    text = "\n".join(item.get("text", "") for item in payload.get("content", [])
                                     if item.get("type") == "output_text").strip()
                    if text:
                        self.message = text


def display_state(state, runner_active):
    status = state.get("status")
    if status == "stopping" and runner_active:
        return "正在收尾", "完成当前项目后停止，不再开始新项目", "#B45309"
    if status == "running" and runner_active:
        return "工作中", "后台执行进程正在运行", "#15803D"
    if status in ("running", "stopping"):
        return "等待检查", "执行记录仍在，但运行进程已退出", "#B45309"
    if not state["enabled"]:
        return "已关闭", "监控关闭，当前没有执行任务", "#64748B"
    if status in ("blocked", "failed", "needs_review"):
        return "等待处理", "遇到阻塞，需要检查具体原因", "#B91C1C"
    if status == "completed":
        return "任务完成", "本次目标已结束", "#64748B"
    if status == "awaiting_task":
        return "等待设置", "尚未配置目标任务", "#64748B"
    return "休眠中", "等待额度恢复或下一次定时检查", "#B45309"


def local_time(value):
    if not value:
        return "尚无记录"
    return datetime.fromisoformat(value).astimezone().strftime("%m-%d %H:%M:%S")


class Dashboard:
    def __init__(self, root, store):
        self.root, self.store = root, store
        self.activity = ActivityReader()
        self.closed = False
        root.title("Codex 额度任务助手 · 实时工作状态")
        root.geometry("860x690")
        root.minsize(740, 600)
        root.configure(bg="#F1F5F9")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.title = tk.Label(root, text="现在正在做什么", bg="#F1F5F9", fg="#0F172A",
                              font=("Microsoft YaHei UI", 23, "bold"), anchor="w")
        self.title.pack(fill="x", padx=28, pady=(24, 4))
        tk.Label(root, text="本地状态自动更新 · 不消耗模型额度 · 关闭此窗口不停止后台工作",
                 bg="#F1F5F9", fg="#64748B", font=("Microsoft YaHei UI", 10), anchor="w").pack(fill="x", padx=28)
        card = tk.Frame(root, bg="white", padx=22, pady=18)
        card.pack(fill="x", padx=28, pady=(20, 14))
        self.state = tk.StringVar()
        self.state_label = tk.Label(card, textvariable=self.state, bg="white", anchor="w",
                                    font=("Microsoft YaHei UI", 27, "bold"))
        self.state_label.pack(fill="x")
        self.detail = tk.StringVar()
        tk.Label(card, textvariable=self.detail, bg="white", fg="#475569", anchor="w",
                 font=("Microsoft YaHei UI", 11)).pack(fill="x", pady=(6, 0))
        self.project = tk.StringVar()
        self.meta = tk.StringVar()
        tk.Label(root, textvariable=self.project, bg="#F1F5F9", fg="#0F172A", anchor="w",
                 font=("Microsoft YaHei UI", 14, "bold")).pack(fill="x", padx=28, pady=(0, 6))
        tk.Label(root, textvariable=self.meta, bg="#F1F5F9", fg="#64748B", anchor="w",
                 font=("Microsoft YaHei UI", 10), justify="left").pack(fill="x", padx=28)
        self.progress = tk.StringVar()
        self.progress_label = tk.Label(root, textvariable=self.progress, bg="white", fg="#0F172A", anchor="nw",
                 justify="left", wraplength=760, padx=20, pady=18,
                 font=("Microsoft YaHei UI", 12))
        self.progress_label.pack(fill="both", expand=True, padx=28, pady=18)
        root.bind("<Configure>", lambda event: self.progress_label.configure(
            wraplength=max(200, root.winfo_width() - 110)))
        self.footer = tk.StringVar()
        tk.Label(root, textvariable=self.footer, bg="#F1F5F9", fg="#64748B", anchor="w",
                 font=("Microsoft YaHei UI", 10), justify="left").pack(fill="x", padx=28, pady=(0, 22))
        self.refresh()

    def refresh(self):
        if self.closed:
            return
        try:
            state = watcher.get_status(self.store)
            self.activity.read(state.get("thread_path"), state.get("started_at"))
            runner_active = False
            if state.get("status") in ("running", "stopping"):
                try:
                    with watcher.file_lock(self.store.home / "runner.lock"):
                        pass
                except watcher.LockBusy:
                    runner_active = True
            title, detail, color = display_state(state, runner_active)
            self.state.set(title)
            self.state_label.configure(fg=color)
            self.detail.set(detail)
            checkpoint = {}
            if state["cwd"]:
                checkpoint = watcher.read_json(Path(state["cwd"]) / "work/open-source-prs/state.json", {})
            self.project.set("当前项目：" + (checkpoint.get("current_repository") or "查看下方任务进度"))
            activity_at = self.activity.updated_at or state.get("last_activity_at")
            self.meta.set(f"最近活动：{local_time(activity_at)}    本轮开始：{local_time(state.get('started_at'))}")
            message = self.activity.message if runner_active and self.activity.message else state.get("message", "")
            self.progress.set(message or "等待首次检查或设置目标任务。")
            quota = state.get("quota") or {}
            remaining = quota.get("remaining_percent")
            quota_text = "尚未读取" if remaining is None else f"{remaining}%"
            prs = checkpoint.get("new_prs", [])
            latest_pr = prs[-1].get("url", "") if prs else "暂无新 PR"
            self.footer.set(f"上次额度样本：5 小时剩余 {quota_text} · {local_time(state.get('last_check'))}\n"
                            f"已记录的新 PR：{len(prs)}    最近一个：{latest_pr}")
        except Exception as error:
            self.state.set("读取状态失败")
            self.state_label.configure(fg="#B91C1C")
            self.progress.set(str(error))
        self.root.after(1500, self.refresh)

    def close(self):
        self.closed = True
        self.root.destroy()


def show(store):
    root = tk.Tk()
    Dashboard(root, store)
    root.mainloop()
