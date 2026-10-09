"""
托比改品台 — 桌面 UI
认领公共箱 → 套模板改品 → 上架小赵1店（可切店）
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


def app_dir() -> Path:
    """源码目录，或打包后 exe 所在目录（可写 config/data）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """只读资源：开发时=ROOT；打包后优先 exe 旁，其次 _internal / _MEIPASS。"""
    base = app_dir()
    if (base / "config").exists():
        return base
    internal = base / "_internal"
    if (internal / "config").exists():
        return internal
    mei = getattr(sys, "_MEIPASS", None)
    if mei and (Path(mei) / "config").exists():
        return Path(mei)
    return base


ROOT = app_dir()
RES = resource_dir()
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
_MEI = getattr(sys, "_MEIPASS", None)
if _MEI:
    mei_src = Path(_MEI) / "src"
    if mei_src.exists() and str(mei_src) not in sys.path:
        sys.path.insert(0, str(mei_src))

import yaml

from api.miaoshou_client import MiaoshouClient
from services.auto_batch import pending_rows, run_available_batches
from services.daily_quota import DailyQuota
from services.feishu_bitable import FeishuBitable
from services.full_pipeline import list_public_items, run_full_pipeline

# —— 视觉：暖纸底 + 墨青强调（非紫、非纯黑仪表盘）——
C = {
    "bg": "#F3EEE4",
    "panel": "#FFFBF5",
    "ink": "#1C2B24",
    "muted": "#5C6B62",
    "line": "#D9D0C3",
    "accent": "#0F6B5C",
    "accent_hover": "#0B5549",
    "danger": "#9B3D2E",
    "ok": "#2F6B3A",
    "row_alt": "#F7F1E8",
    "select": "#D8EBE6",
}


SHOPS = [
    (18545044, "小赵1店"),
    (18545217, "小韩1店"),
]


class TobeeApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("托比改品台")
        self.geometry("1180x720")
        self.minsize(980, 620)
        self.configure(bg=C["bg"])
        self._log_q: queue.Queue[str] = queue.Queue()
        self._busy = False
        self._items: list[dict] = []
        self._settings = self._load_settings()
        self._scheme = self._load_scheme()
        self._publish_cfg = self._load_publish_cfg()
        self._watcher_on = False
        self._watcher_stop = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        self._job_lock = threading.Lock()
        self._build_style()
        self._build_ui()
        self.after(120, self._drain_log)
        self.after(300, self.refresh_list)
        self.after(400, self._update_watcher_label)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_settings(self) -> dict:
        # 优先程序旁可写 config；没有则从打包资源复制一份
        path = ROOT / "config" / "settings.yaml"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            for cand in (
                RES / "config" / "settings.portable.yaml",
                RES / "config" / "settings.yaml",
            ):
                if cand.exists():
                    path.write_text(cand.read_text(encoding="utf-8"), encoding="utf-8")
                    break
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        paths = data.get("paths") or {}
        for key in ("blank_tee_dir", "preview_dir", "db_path"):
            val = paths.get(key)
            if not val:
                continue
            p = Path(str(val))
            if not p.is_absolute():
                paths[key] = str((ROOT / p).resolve())
        data["paths"] = paths
        return data

    def _load_scheme(self) -> dict:
        name = self._settings.get("default_scheme") or "default"
        for base in (ROOT, RES):
            path = base / "config" / "schemes" / f"{name}.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        raise FileNotFoundError(f"找不到方案 config/schemes/{name}.json")

    def _load_publish_cfg(self) -> dict:
        for base in (ROOT, RES):
            path = base / "config" / "publish_xiaozhao.yaml"
            if path.exists():
                return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {}

    def _client(self) -> MiaoshouClient:
        m = self._settings.get("miaoshou") or {}
        return MiaoshouClient(
            app_key=str(m.get("app_key") or ""),
            app_secret=str(m.get("app_secret") or ""),
            base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
            timeout=float(m.get("timeout_seconds") or 60),
            min_interval=float(m.get("min_request_interval_seconds") or 1.2),
        )

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=C["bg"])
        style.configure("Panel.TFrame", background=C["panel"])
        style.configure("TLabel", background=C["bg"], foreground=C["ink"], font=("Microsoft YaHei UI", 10))
        style.configure("Brand.TLabel", background=C["bg"], foreground=C["ink"], font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Sub.TLabel", background=C["bg"], foreground=C["muted"], font=("Microsoft YaHei UI", 10))
        style.configure("Panel.TLabel", background=C["panel"], foreground=C["ink"], font=("Microsoft YaHei UI", 10))
        style.configure(
            "Treeview",
            background=C["panel"],
            fieldbackground=C["panel"],
            foreground=C["ink"],
            rowheight=28,
            font=("Microsoft YaHei UI", 9),
            borderwidth=0,
        )
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), background=C["line"], foreground=C["ink"])
        style.map("Treeview", background=[("selected", C["select"])], foreground=[("selected", C["ink"])])

    def _btn(self, parent, text, command, primary=False, danger=False):
        bg = C["accent"] if primary else (C["danger"] if danger else C["panel"])
        fg = "#FFFBF5" if (primary or danger) else C["ink"]
        b = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=C["accent_hover"] if primary else C["line"],
            activeforeground=fg,
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold" if primary else "normal"),
        )
        return b

    def _build_ui(self) -> None:
        top = ttk.Frame(self, style="TFrame")
        top.pack(fill="x", padx=24, pady=(18, 8))

        left = ttk.Frame(top, style="TFrame")
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="托比改品台", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(
            left,
            text="墨西哥 TK × 妙手 · 手动认领上架 / 飞书满200自动值班",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        right = ttk.Frame(top, style="TFrame")
        right.pack(side="right")
        ttk.Label(right, text="目标店铺", style="Sub.TLabel").pack(anchor="e")
        self.shop_var = tk.StringVar(value="小赵1店")
        self.shop_box = ttk.Combobox(
            right,
            textvariable=self.shop_var,
            values=[n for _, n in SHOPS],
            state="readonly",
            width=14,
            font=("Microsoft YaHei UI", 10),
        )
        self.shop_box.pack(anchor="e", pady=(4, 0))

        body = ttk.Frame(self, style="TFrame")
        body.pack(fill="both", expand=True, padx=24, pady=8)

        side = tk.Frame(body, bg=C["panel"], highlightbackground=C["line"], highlightthickness=1)
        side.pack(side="left", fill="y", padx=(0, 12))
        side.configure(width=236)
        side.pack_propagate(False)

        pad = tk.Frame(side, bg=C["panel"])
        pad.pack(fill="both", expand=True, padx=14, pady=14)

        tk.Label(pad, text="筛选", bg=C["panel"], fg=C["ink"], font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        tk.Label(pad, text="公共箱分组", bg=C["panel"], fg=C["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(12, 2))
        self.group_var = tk.StringVar(value="小赵")
        self.group_entry = ttk.Entry(pad, textvariable=self.group_var, width=18)
        self.group_entry.pack(anchor="w")

        tk.Label(pad, text="只看成功采集", bg=C["panel"], fg=C["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(10, 2))
        self.success_only = tk.BooleanVar(value=True)
        tk.Checkbutton(
            pad,
            text="status = success",
            variable=self.success_only,
            bg=C["panel"],
            fg=C["ink"],
            activebackground=C["panel"],
            selectcolor=C["panel"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w")

        tk.Label(pad, text="最多加载条数", bg=C["panel"], fg=C["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(10, 2))
        self.limit_var = tk.StringVar(value="50")
        ttk.Entry(pad, textvariable=self.limit_var, width=8).pack(anchor="w")

        self._btn(pad, "刷新公共箱", self.refresh_list).pack(fill="x", pady=(16, 6))
        self._btn(pad, "全选当前列表", self.select_all).pack(fill="x", pady=4)
        self._btn(pad, "取消全选", self.clear_sel).pack(fill="x", pady=4)

        tk.Frame(pad, bg=C["line"], height=1).pack(fill="x", pady=14)

        self.btn_full = self._btn(pad, "全套流程\n认领 → 模板 → 上架", self.run_full, primary=True)
        self.btn_full.pack(fill="x", pady=4)
        self.btn_edit = self._btn(pad, "认领 + 模板（不上架）", lambda: self.run_full(publish=False))
        self.btn_edit.pack(fill="x", pady=4)

        tk.Frame(pad, bg=C["line"], height=1).pack(fill="x", pady=14)
        tk.Label(pad, text="飞书值班", bg=C["panel"], fg=C["ink"], font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        tk.Label(
            pad,
            text="满200对半分配两店\n每小时检查 · 单店日限300",
            bg=C["panel"],
            fg=C["muted"],
            justify="left",
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", pady=(4, 8))
        self.watcher_status = tk.StringVar(value="值班：关")
        tk.Label(
            pad,
            textvariable=self.watcher_status,
            bg=C["panel"],
            fg=C["ok"],
            font=("Microsoft YaHei UI", 9, "bold"),
            wraplength=180,
            justify="left",
        ).pack(anchor="w")
        self.btn_watch = self._btn(pad, "开启飞书值班", self.toggle_watcher, primary=True)
        self.btn_watch.pack(fill="x", pady=(10, 4))
        self.btn_watch_once = self._btn(pad, "立即检查一轮", self.run_watcher_once)
        self.btn_watch_once.pack(fill="x", pady=4)

        tk.Label(
            pad,
            text="手动上架默认：立即发布\n0.2kg · 25×22×4 · 西语",
            bg=C["panel"],
            fg=C["muted"],
            justify="left",
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", pady=(16, 0))

        main = tk.Frame(body, bg=C["bg"])
        main.pack(side="left", fill="both", expand=True)

        list_wrap = tk.Frame(main, bg=C["panel"], highlightbackground=C["line"], highlightthickness=1)
        list_wrap.pack(fill="both", expand=True)

        head = tk.Frame(list_wrap, bg=C["panel"])
        head.pack(fill="x", padx=12, pady=(10, 4))
        self.status_var = tk.StringVar(value="准备就绪")
        tk.Label(head, textvariable=self.status_var, bg=C["panel"], fg=C["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.count_var = tk.StringVar(value="0 条")
        tk.Label(head, textvariable=self.count_var, bg=C["panel"], fg=C["ink"], font=("Microsoft YaHei UI", 9, "bold")).pack(side="right")

        cols = ("sel", "group", "status", "title", "price", "id", "time")
        self.tree = ttk.Treeview(list_wrap, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("sel", text="选")
        self.tree.heading("group", text="分组")
        self.tree.heading("status", text="状态")
        self.tree.heading("title", text="标题")
        self.tree.heading("price", text="价")
        self.tree.heading("id", text="公共箱ID")
        self.tree.heading("time", text="采集时间")
        self.tree.column("sel", width=36, anchor="center")
        self.tree.column("group", width=70, anchor="center")
        self.tree.column("status", width=70, anchor="center")
        self.tree.column("title", width=420, anchor="w")
        self.tree.column("price", width=70, anchor="e")
        self.tree.column("id", width=110, anchor="center")
        self.tree.column("time", width=140, anchor="center")
        sy = ttk.Scrollbar(list_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        sy.pack(side="right", fill="y", pady=(0, 8), padx=(0, 8))
        self.tree.bind("<Double-1>", self._toggle_mark)
        self.tree.bind("<Button-1>", self._on_click_sel)

        self._marked: set[str] = set()

        log_wrap = tk.Frame(main, bg=C["panel"], highlightbackground=C["line"], highlightthickness=1)
        log_wrap.pack(fill="x", pady=(12, 0))
        tk.Label(log_wrap, text="运行日志", bg=C["panel"], fg=C["ink"], font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=12, pady=(8, 0))
        self.log = tk.Text(
            log_wrap,
            height=10,
            bg="#1C2B24",
            fg="#E7F0EA",
            insertbackground="#E7F0EA",
            relief="flat",
            font=("Cascadia Mono", 9),
            padx=10,
            pady=8,
        )
        self.log.pack(fill="x", padx=8, pady=8)
        self.log.configure(state="disabled")

    def log_write(self, msg: str) -> None:
        self._log_q.put(msg)

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self._log_q.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", msg.rstrip() + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._drain_log)

    def _shop_id(self) -> tuple[int, str]:
        name = self.shop_var.get()
        for sid, n in SHOPS:
            if n == name:
                return sid, n
        return SHOPS[0]

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for w in (self.btn_full, self.btn_edit, self.btn_watch_once):
            w.configure(state=state)
        if not self._watcher_on:
            self.btn_watch.configure(state=state)
        self.status_var.set("处理中…" if busy else ("值班运行中" if self._watcher_on else "准备就绪"))

    def _feishu_cfg(self) -> dict:
        return self._settings.get("feishu") or {}

    def _quota(self) -> DailyQuota:
        paths = self._settings.get("paths") or {}
        db = Path(paths.get("db_path") or ROOT / "data" / "runs.db")
        fcfg = self._feishu_cfg()
        return DailyQuota(db.parent / "daily_quota.json", int(fcfg.get("daily_limit_per_shop") or 300))

    def _feishu_client(self) -> FeishuBitable:
        f = self._feishu_cfg()
        return FeishuBitable(
            app_id=str(f.get("app_id") or ""),
            app_secret=str(f.get("app_secret") or ""),
            app_token=str(f.get("bitable_app_token") or ""),
            table_id=str(f.get("bitable_table_id") or ""),
        )

    def _update_watcher_label(self, extra: str = "") -> None:
        fcfg = self._feishu_cfg()
        split = fcfg.get("split") or []
        q = self._quota()
        parts = [f"{s.get('shop_name')}:{q.count(int(s['shop_id']))}/{q.limit}" for s in split]
        state = "开" if self._watcher_on else "关"
        text = f"值班：{state}\n今日 " + " · ".join(parts)
        if extra:
            text += f"\n{extra}"
        self.watcher_status.set(text)

    def toggle_watcher(self) -> None:
        if self._watcher_on:
            self._stop_watcher()
            return
        fcfg = self._feishu_cfg()
        if not (fcfg.get("app_id") and fcfg.get("bitable_app_token")):
            messagebox.showerror("未配置", "settings.yaml 缺少飞书 app_id / 表格 token")
            return
        self._watcher_on = True
        self._watcher_stop.clear()
        self.btn_watch.configure(text="停止飞书值班", bg=C["danger"], fg="#FFFBF5")
        self._update_watcher_label("下一轮即将检查…")
        self.log_write("已开启飞书值班（满200自动跑，每小时检查）")
        self._watcher_thread = threading.Thread(target=self._watcher_loop, daemon=True)
        self._watcher_thread.start()

    def _stop_watcher(self) -> None:
        self._watcher_on = False
        self._watcher_stop.set()
        self.btn_watch.configure(text="开启飞书值班", bg=C["accent"], fg="#FFFBF5")
        self._update_watcher_label("已停止")
        self.log_write("已停止飞书值班")
        if not self._busy:
            self.status_var.set("准备就绪")

    def run_watcher_once(self) -> None:
        if self._busy:
            return
        if not messagebox.askyesno("立即检查", "立刻读取飞书表并按规则处理一轮？\n（不足200条不会上架）"):
            return
        if not self._job_lock.acquire(blocking=False):
            messagebox.showwarning("忙碌中", "已有任务在跑，请稍后再试")
            return
        self._set_busy(True)
        self.log_write("手动触发：飞书检查一轮")

        def work():
            try:
                summary = self._run_feishu_cycle()
                self.after(0, lambda: self._update_watcher_label(summary.get("stopped_reason") or "本轮完成"))
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "飞书检查完成",
                        f"处理 {summary.get('processed', 0)} · 成功 {summary.get('ok', 0)} · 失败 {summary.get('fail', 0)}\n"
                        f"{summary.get('stopped_reason') or ''}",
                    ),
                )
            except Exception as e:
                self.log_write(f"飞书检查失败: {e}")
                self.after(0, lambda: messagebox.showerror("飞书检查失败", str(e)))
            finally:
                self._job_lock.release()
                self.after(0, lambda: self._set_busy(False))

        threading.Thread(target=work, daemon=True).start()

    def _run_feishu_cycle(self) -> dict:
        fcfg = self._feishu_cfg()
        paths = self._settings.get("paths") or {}
        blank = Path(paths.get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
        fill = [str(u) for u in (self._settings.get("fill_image_urls") or []) if str(u).startswith("http")]
        out_dir = Path(paths.get("preview_dir") or ROOT / "data" / "previews")
        feishu = self._feishu_client()
        # refresh pending count for UI
        try:
            pending = pending_rows(feishu.list_records(page_size=200), fcfg)
            self.after(0, lambda: self._update_watcher_label(f"待处理 {len(pending)} 条"))
        except Exception:
            pass
        return run_available_batches(
            client=self._client(),
            feishu=feishu,
            fcfg=fcfg,
            scheme=self._scheme,
            publish_cfg=dict(self._publish_cfg),
            blank_dir=blank,
            fill_urls=fill,
            quota=self._quota(),
            out_dir=out_dir,
            log=self.log_write,
        )

    def _watcher_loop(self) -> None:
        fcfg = self._feishu_cfg()
        interval = int(fcfg.get("poll_interval_seconds") or 3600)
        while self._watcher_on and not self._watcher_stop.is_set():
            if not self._job_lock.acquire(blocking=False):
                self.log_write("值班：其它任务进行中，跳过本轮")
            else:
                try:
                    self.after(0, lambda: self.status_var.set("飞书值班检查中…"))
                    summary = self._run_feishu_cycle()
                    reason = summary.get("stopped_reason") or "本轮完成"
                    self.after(0, lambda r=reason: self._update_watcher_label(r))
                except Exception as e:
                    self.log_write(f"值班异常: {e}")
                    self.after(0, lambda: self._update_watcher_label(f"异常: {e}"[:40]))
                finally:
                    self._job_lock.release()
                    self.after(0, lambda: self.status_var.set("值班运行中" if self._watcher_on else "准备就绪"))
            for _ in range(max(1, interval)):
                if self._watcher_stop.wait(1.0):
                    return
                if not self._watcher_on:
                    return

    def _on_close(self) -> None:
        if self._watcher_on:
            self._stop_watcher()
        self.destroy()

    def refresh_list(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.status_var.set("正在拉取公共采集箱…")

        def work():
            try:
                client = self._client()
                limit = max(10, min(100, int(self.limit_var.get() or 50)))
                items = list_public_items(client, page_size=limit, log=self.log_write)
                group = (self.group_var.get() or "").strip()
                success_only = self.success_only.get()
                filtered = []
                for it in items:
                    g = (it.get("commonCollectBoxGroupName") or "").strip()
                    if group and g != group:
                        continue
                    if success_only and str(it.get("status") or "").lower() != "success":
                        continue
                    filtered.append(it)
                filtered.sort(key=lambda x: str(x.get("gmtCreate") or ""), reverse=True)
                self._items = filtered
                self.after(0, self._fill_tree)
                self.log_write(f"已加载 {len(filtered)} 条（分组={group or '全部'}）")
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("刷新失败", str(e)))
                self.log_write(f"刷新失败: {e}")
            finally:
                self.after(0, lambda: self._set_busy(False))

        threading.Thread(target=work, daemon=True).start()

    def _fill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._marked.clear()
        for it in self._items:
            cid = str(it.get("commonCollectBoxDetailId"))
            title = (it.get("title") or "").replace("\n", " ")
            if len(title) > 64:
                title = title[:64] + "…"
            price = it.get("price") or it.get("minSkuPrice") or ""
            self.tree.insert(
                "",
                "end",
                iid=cid,
                values=(
                    "☐",
                    (it.get("commonCollectBoxGroupName") or ""),
                    it.get("status") or "",
                    title,
                    price,
                    cid,
                    it.get("gmtCreate") or "",
                ),
            )
        self.count_var.set(f"{len(self._items)} 条")

    def _on_click_sel(self, event) -> None:
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col != "#1":
            return
        row = self.tree.identify_row(event.y)
        if row:
            self._toggle_iid(row)

    def _toggle_mark(self, _event=None) -> None:
        row = self.tree.focus()
        if row:
            self._toggle_iid(row)

    def _toggle_iid(self, iid: str) -> None:
        vals = list(self.tree.item(iid, "values"))
        if iid in self._marked:
            self._marked.discard(iid)
            vals[0] = "☐"
        else:
            self._marked.add(iid)
            vals[0] = "☑"
        self.tree.item(iid, values=vals)
        self.count_var.set(f"{len(self._items)} 条 · 已选 {len(self._marked)}")

    def select_all(self) -> None:
        for iid in self.tree.get_children():
            if iid not in self._marked:
                self._toggle_iid(iid)

    def clear_sel(self) -> None:
        for iid in list(self._marked):
            self._toggle_iid(iid)

    def _selected_items(self) -> list[dict]:
        want = {int(x) for x in self._marked}
        out = []
        for it in self._items:
            try:
                cid = int(it.get("commonCollectBoxDetailId"))
            except Exception:
                continue
            if cid in want:
                out.append(it)
        return out

    def run_full(self, publish: bool = True) -> None:
        if self._busy:
            return
        selected = self._selected_items()
        if not selected:
            messagebox.showwarning("未选择", "请先勾选要处理的商品（点「选」列或双击行）")
            return
        shop_id, shop_name = self._shop_id()
        action = "全套流程（含上架）" if publish else "认领+模板"
        if not messagebox.askyesno(
            "确认",
            f"将对 {len(selected)} 条商品执行：{action}\n店铺：{shop_name}\n\n确定继续？",
        ):
            return
        if not self._job_lock.acquire(blocking=False):
            messagebox.showwarning("忙碌中", "飞书值班或其它任务正在跑，请稍后再试")
            return

        self._set_busy(True)
        self.log_write("=" * 48)
        self.log_write(f"开始 {action} · {shop_name} · {len(selected)} 条")

        def work():
            try:
                client = self._client()
                blank = Path((self._settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
                fill = [str(u) for u in (self._settings.get("fill_image_urls") or []) if str(u).startswith("http")]
                out_dir = Path((self._settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")
                pub = dict(self._publish_cfg)
                result = run_full_pipeline(
                    client,
                    items=selected,
                    shop_id=shop_id,
                    shop_name=shop_name,
                    scheme=self._scheme,
                    publish_cfg=pub,
                    blank_dir=blank,
                    fill_urls=fill,
                    out_dir=out_dir,
                    do_publish=publish,
                    log=self.log_write,
                )
                ok = sum(1 for r in result.edited if r.get("ok"))
                msg = f"完成：改品成功 {ok}/{len(result.edited)}"
                if result.report_path:
                    msg += f"\n报告：{result.report_path}"
                self.after(0, lambda: messagebox.showinfo("完成", msg))
            except Exception as e:
                self.log_write(f"失败: {e}")
                self.after(0, lambda: messagebox.showerror("失败", str(e)))
            finally:
                self._job_lock.release()
                self.after(0, lambda: self._set_busy(False))
                self.after(500, self.refresh_list)

        threading.Thread(target=work, daemon=True).start()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    app = TobeeApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
