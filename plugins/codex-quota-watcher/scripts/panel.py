"""A small local settings panel; no web server or extra dependencies."""

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import watcher


STATUS_NAMES = {
    "pending": "等待首次检查", "off": "已关闭", "awaiting_task": "等待设置任务",
    "waiting_quota": "等待额度", "running": "正在执行", "completed": "任务已完成",
    "paused": "任务已暂停", "blocked": "需要人工处理", "failed": "执行失败",
    "needs_review": "需要人工检查",
    "stopping": "正在完成当前项目后停止",
}


class Panel:
    def __init__(self, root, store):
        self.root, self.store = root, store
        self.closed = False
        self.check_results = queue.Queue()
        root.title("Codex 额度任务助手")
        root.geometry("720x600")
        root.minsize(620, 520)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.frame = ttk.Frame(root, padding=20)
        self.frame.pack(fill="both", expand=True)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(4, weight=1)

        ttk.Label(self.frame, text="Codex 额度任务助手", font=("Microsoft YaHei UI", 17, "bold")).grid(sticky="w")
        ttk.Label(self.frame, text="每 15 分钟检查一次 · 有额度时执行 · 支持单次或持续任务").grid(row=1, sticky="w", pady=(5, 16))
        ttk.Label(self.frame, text="目标任务（由你设定）").grid(row=2, sticky="w")
        ttk.Label(self.frame, text="写清预期结果和限制；处理阻塞原因后点击开启即可续跑。").grid(row=3, sticky="w", pady=(3, 5))
        self.prompt = tk.Text(self.frame, height=8, wrap="word", font=("Microsoft YaHei UI", 11), undo=True)
        self.prompt.grid(row=4, sticky="nsew")
        config = store.config()
        self.prompt.insert("1.0", config["prompt"])
        self.continuous = tk.BooleanVar(value=config["continuous"])

        ttk.Label(self.frame, text="任务工作目录").grid(row=5, sticky="w", pady=(12, 5))
        directory = ttk.Frame(self.frame)
        directory.grid(row=6, sticky="ew")
        directory.columnconfigure(0, weight=1)
        self.cwd = tk.StringVar(value=config["cwd"])
        ttk.Entry(directory, textvariable=self.cwd).grid(sticky="ew")
        ttk.Button(directory, text="选择文件夹", command=self.browse).grid(row=0, column=1, padx=(8, 0))

        controls = ttk.Frame(self.frame)
        controls.grid(row=7, sticky="w", pady=15)
        ttk.Button(controls, text="保存目标任务", command=self.save).pack(side="left")
        ttk.Button(controls, text="开启", command=lambda: self.switch(True)).pack(side="left", padx=8)
        ttk.Button(controls, text="关闭", command=lambda: self.switch(False)).pack(side="left")
        self.check_button = ttk.Button(controls, text="刷新额度", command=self.check)
        self.check_button.pack(side="left", padx=8)
        ttk.Checkbutton(controls, text="持续任务", variable=self.continuous).pack(side="left")
        self.status = tk.StringVar()
        ttk.Label(self.frame, textvariable=self.status, wraplength=650).grid(row=8, sticky="w")
        ttk.Label(self.frame, text="聊天口令：不要让Tibo发现 开启 · Tibo要按按钮了 关闭", foreground="#555555").grid(row=9, sticky="w", pady=(14, 3))
        ttk.Label(self.frame, text="关闭窗口不会关闭监控。额度耗尽时也可通过这里切换开关。", foreground="#555555").grid(row=10, sticky="w")
        self.refresh()

    def browse(self):
        selected = filedialog.askdirectory(parent=self.root, initialdir=self.cwd.get() or None)
        if selected:
            self.cwd.set(selected)

    def save(self):
        try:
            watcher.set_task(self.store, self.prompt.get("1.0", "end"), self.cwd.get(),
                             continuous=self.continuous.get())
            messagebox.showinfo("保存成功", "目标任务已保存。开启监控后，会在额度可用时执行。", parent=self.root)
        except Exception as error:
            messagebox.showerror("无法保存", str(error), parent=self.root)

    def switch(self, enabled):
        try:
            watcher.set_switch(self.store, enabled)
        except Exception as error:
            messagebox.showerror("无法切换", str(error), parent=self.root)

    def check(self):
        self.check_button.configure(state="disabled")

        def background():
            try:
                watcher.check(self.store)
                error = None
            except Exception as caught:
                error = str(caught)
            self.check_results.put(error)

        threading.Thread(target=background, daemon=True).start()

    def refresh(self):
        if self.closed:
            return
        try:
            error = self.check_results.get_nowait()
            self.check_button.configure(state="normal")
            if error:
                messagebox.showerror("读取额度失败", error, parent=self.root)
        except queue.Empty:
            pass
        try:
            state = watcher.get_status(self.store)
            quota = state.get("quota") or {}
            remaining = quota.get("remaining_percent")
            quota_text = "尚未读取" if remaining is None else f"{remaining}%"
            other = quota.get("other_remaining_percent", [])
            other_text = " / ".join("未知" if value is None else f"{value}%" for value in other) or "尚未读取"
            enabled = "开启" if state["enabled"] else "关闭"
            status_name = STATUS_NAMES.get(state.get("status"), "等待检查")
            self.status.set(f"监控：{enabled}    任务：{status_name}\n"
                            f"5 小时剩余额度：{quota_text}    其他窗口剩余：{other_text}\n"
                            f"{state.get('message', '')}")
        except Exception as error:
            self.status.set(f"状态读取失败：{error}")
        self.root.after(1500, self.refresh)

    def close(self):
        self.closed = True
        self.root.destroy()


def show(store):
    root = tk.Tk()
    Panel(root, store)
    root.mainloop()
