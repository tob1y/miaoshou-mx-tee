# -*- coding: utf-8 -*-
"""今天公共箱 success：按采集时间从早到晚逐条处理。

- 先填满小韩日配额，再上小赵
- 门禁未过 → 飞书电子表格（无权限则写本地复核表）
- 结束导出识图汇总 Excel（同识图测试报告格式）
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api import endpoints as ep
from api.miaoshou_client import MiaoshouClient
from services.auto_batch import claim_to_detail, edit_and_publish_one, qps_retry
from services.daily_quota import DailyQuota
from services.feishu_sheets import FeishuSheets, FeishuSheetsError
from services.transformer import transform_product

HAN_ID = 18545217
HAN_NAME = "小韩1店"
ZHAO_ID = 18545044
ZHAO_NAME = "小赵1店"
TZ = timezone(timedelta(hours=8))
HOLD_HEADERS = ["时间", "货源ID", "链接", "标题", "门禁原因", "店铺", "备注"]


def parse_gmt(s: str) -> datetime | None:
    s = str(s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def adapt_public_product(prod: dict) -> dict:
    out = dict(prod)
    color_name = str(out.get("colorPropName") or "Color")
    size_name = str(out.get("sizePropName") or "Tamaño")
    color_vals = []
    for key, val in (out.get("colorMap") or {}).items():
        name = val.get("name") if isinstance(val, dict) else key
        color_vals.append({"attrValue": str(name or key), "attrValueId": str(key)})
    size_vals = []
    for key, val in (out.get("sizeMap") or {}).items():
        name = val.get("name") if isinstance(val, dict) else key
        size_vals.append({"attrValue": str(name or key), "attrValueId": str(key)})
    props = []
    if color_vals:
        props.append({"name": color_name, "attrValueList": color_vals})
    if size_vals:
        props.append({"name": size_name, "attrValueList": size_vals})
    out["skuPropertyList"] = props
    return out


def already_published_sids() -> set[str]:
    done: set[str] = set()
    for log in (ROOT / "data/previews").glob("split_publish_han100_zhao100_*.log"):
        sid = ""
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"sid=(\d+)", line)
            if m and "common=" in line:
                sid = m.group(1)
                continue
            if sid and "OK 已上架" in line:
                done.add(sid)
                sid = ""
    for log in (ROOT / "data/previews").glob("chrono_publish_*.log"):
        sid = ""
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"sid=(\d+)", line)
            if m:
                sid = m.group(1)
            if not sid:
                continue
            if any(
                k in line
                for k in (
                    "OK 已上架",
                    "文字留库",
                    "识图/改品留库",
                    "  FAIL ",
                )
            ):
                done.add(sid)
                sid = ""
    return done


def list_today_success(client: MiaoshouClient, today0: datetime) -> list[dict]:
    rows: dict[int, dict] = {}
    for page in range(1, 80):
        body = qps_retry(
            lambda p=page: client.assert_success(
                client.post(ep.PUBLIC_LIST, {"pageNo": p, "pageSize": 100}),
                "公共箱",
            ),
            "public_list",
        )
        batch = (body.get("data") or {}).get("detailList") or []
        if not batch:
            break
        for it in batch:
            if str(it.get("status") or "").lower() != "success":
                continue
            gmt = parse_gmt(str(it.get("gmtCreate") or ""))
            if not gmt or gmt < today0:
                continue
            cid = it.get("commonCollectBoxDetailId")
            sid = str(it.get("itemNum") or it.get("sourceItemId") or "")
            if not sid:
                for s in it.get("sourceList") or []:
                    if isinstance(s, dict) and s.get("sourceItemId"):
                        sid = str(s["sourceItemId"])
                        break
            if not sid or cid is None:
                continue
            rows[int(cid)] = {
                "common_id": int(cid),
                "sid": sid,
                "title": str(it.get("title") or "")[:160],
                "gmt": str(it.get("gmtCreate") or ""),
            }
        oldest = parse_gmt(str(batch[-1].get("gmtCreate") or ""))
        if oldest and oldest < today0:
            break
        if len(batch) < 100:
            break
        time.sleep(0.2)
    # 时间顺序：早 → 晚
    return sorted(rows.values(), key=lambda x: x.get("gmt") or "")


def gate_text_only(
    client: MiaoshouClient,
    row: dict,
    scheme: dict,
    blank: Path,
    fill_urls: list[str],
) -> tuple[bool, str, str]:
    """文字门禁预检。返回 (ok, reason, title)。"""
    body = qps_retry(
        lambda cid=row["common_id"]: client.post(
            ep.PUBLIC_DETAIL, {"commonCollectBoxDetailId": int(cid)}
        ),
        "detail",
    )
    inner = (body.get("body") or body).get("data") or {}
    if "result" in (body.get("body") or {}) and (body.get("body") or {}).get("result") != "success":
        return False, str((body.get("body") or {}).get("message") or "detail_fail")[:120], ""
    prod = inner.get("editCommonCollectBoxDetail") or {}
    if not prod:
        return False, "no_detail", ""
    title = str(prod.get("oriTitle") or prod.get("title") or row.get("title") or "")
    vision_off = {"enabled": False}
    report = transform_product(
        adapt_public_product(prod),
        scheme,
        blank,
        fill_image_urls=fill_urls,
        vision_cfg=vision_off,
    )
    hold_r = report.get("hold_without_publish") or {}
    if hold_r.get("hold"):
        return False, str(hold_r.get("reason") or "hold"), title
    return True, "ok", title


class HoldSink:
    """门禁未过 → 只写飞书电子表格（不再写本地 Excel）。"""

    def __init__(self, sheets: FeishuSheets | None, log):
        self.sheets = sheets
        self.log = log
        self.feishu_ok = False
        self.written = 0
        self._init()

    def _init(self) -> None:
        if not self.sheets:
            self.log("未配置 hold_sheet_token，门禁未过仅记日志")
            return
        try:
            self.sheets.ensure_header(HOLD_HEADERS)
            self.feishu_ok = True
            self.log(f"飞书复核表已连通 sheet={self.sheets._sheet_id}")
        except Exception as e:
            self.log(f"飞书表暂不可写（每条会重试）: {e}")
            self.feishu_ok = False

    def add(
        self,
        *,
        sid: str,
        title: str,
        reason: str,
        shop: str = "",
        note: str = "",
    ) -> None:
        now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        link = f"https://shop.tiktok.com/mx/pdp/{sid}"
        row = [now, sid, link, (title or "")[:200], reason, shop, note]
        if not self.sheets:
            self.log(f"  [未过] sid={sid} reason={reason}（无飞书配置）")
            return
        # 每次尝试写入；权限刚开通时可恢复
        try:
            if not self.feishu_ok:
                self.sheets.ensure_header(HOLD_HEADERS)
                self.feishu_ok = True
                self.log(f"飞书复核表已恢复 sheet={self.sheets._sheet_id}")
            self.sheets.append_rows([row])
            self.written += 1
        except Exception as e:
            self.feishu_ok = False
            self.log(f"  飞书追加失败 sid={sid}: {e}")
            self.log(f"  [未过备份] {sid}\t{reason}\t{(title or '')[:80]}")


def export_metrics_excel(stamp: str, out_dir: Path, log) -> Path | None:
    metrics_path = out_dir / f"vision_metrics_{stamp}.json"
    result_path = out_dir / f"chrono_publish_{stamp}.json"
    if not metrics_path.exists():
        return None
    # 兼容导出脚本文件名
    alias = out_dir / f"split_publish_han100_zhao100_{stamp}.json"
    if result_path.exists() and not alias.exists():
        alias.write_text(result_path.read_text(encoding="utf-8"), encoding="utf-8")
    import subprocess

    py = sys.executable
    r = subprocess.run(
        [py, str(out_dir / "_export_vision_xlsx.py"), stamp],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log(f"导出Excel: code={r.returncode} {(r.stdout or '')[-200:]} {(r.stderr or '')[-200:]}")
    xlsx = out_dir / f"识图测试报告_{stamp}.xlsx"
    return xlsx if xlsx.exists() else None


def main() -> int:
    idle_rounds_max = 40  # ~空等 40*45s ≈ 30min 无新货则结束
    poll_sleep = 45
    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    max_process = None
    for a in args:
        if a.isdigit():
            max_process = int(a)

    today0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    m = settings.get("miaoshou") or {}
    fcfg = settings.get("feishu") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data/previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"chrono_publish_{stamp}.log"
    result_path = out_dir / f"chrono_publish_{stamp}.json"
    state_path = out_dir / f"chrono_publish_{stamp}_state.json"
    hold_xlsx = out_dir / f"门禁未过_{datetime.now(TZ).strftime('%Y%m%d')}.xlsx"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 120),
        min_interval=float(m.get("min_request_interval_seconds") or 0.8),
        max_retries=3,
    )
    scheme = json.loads((ROOT / "config/schemes/default.json").read_text(encoding="utf-8"))
    publish_cfg = yaml.safe_load((ROOT / "config/publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path(paths.get("blank_tee_dir") or ROOT / "assets/blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    vision_cfg = dict(settings.get("vision") or {})
    quota = DailyQuota(
        ROOT / "data/daily_quota.json",
        limit_per_shop=int(fcfg.get("daily_limit_per_shop") or 300),
    )

    sheets = None
    token = str(fcfg.get("hold_sheet_token") or "").strip()
    if token:
        sheets = FeishuSheets(str(fcfg["app_id"]), str(fcfg["app_secret"]), token)
    hold_sink = HoldSink(sheets, log)

    processed: set[str] = set(already_published_sids())
    # 本进程已处理（含留库）
    handled: set[str] = set()
    results: list[dict] = []
    stats = {
        HAN_ID: {"ok": 0, "held": 0, "fail": 0},
        ZHAO_ID: {"ok": 0, "held": 0, "fail": 0},
    }
    idle = 0
    total_handled = 0

    log(
        f"启动 chrono：韩剩{quota.remaining(HAN_ID)} 赵剩{quota.remaining(ZHAO_ID)}；"
        f"已上架跳过种子={len(processed)}；识图={vision_cfg.get('model')}"
    )

    def pick_shop() -> tuple[int, str] | None:
        if quota.remaining(HAN_ID) > 0:
            return HAN_ID, HAN_NAME
        if quota.remaining(ZHAO_ID) > 0:
            return ZHAO_ID, ZHAO_NAME
        return None

    def save_state() -> None:
        state_path.write_text(
            json.dumps(
                {
                    "handled": len(handled),
                    "stats": stats,
                    "quota_han": quota.count(HAN_ID),
                    "quota_zhao": quota.count(ZHAO_ID),
                    "results_n": len(results),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        result_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    def write_vision_metrics() -> None:
        vrows = []
        for r in results:
            vm = r.get("vision_metrics")
            if isinstance(vm, dict):
                vrows.append(vm)
            else:
                # 文字门禁留库也记一条便于汇总
                hold = r.get("hold_without_publish") or {}
                if r.get("held_in_box") or hold.get("hold"):
                    vrows.append(
                        {
                            "source_id": r.get("source_id"),
                            "common_id": r.get("common_id"),
                            "detail_id": r.get("detail_id"),
                            "hold": True,
                            "reason": hold.get("reason") or r.get("reason") or "hold",
                            "images_scanned": 0,
                            "images_total": 0,
                            "elapsed_s": 0,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                            "cny": 0,
                        }
                    )
                elif r.get("ok"):
                    # 可能有完整 vision 嵌在 hold_without_publish
                    pass
        # 从 edit 回写更完整
        vrows2 = [r["vision_metrics"] for r in results if isinstance(r.get("vision_metrics"), dict)]
        if not vrows2:
            vrows2 = vrows
        if not vrows2:
            return
        by_reason: dict[str, int] = {}
        for r in results:
            hold = r.get("hold_without_publish") or {}
            why = str(hold.get("reason") or r.get("reason") or ("ok" if r.get("ok") else "fail"))
            by_reason[why] = by_reason.get(why, 0) + 1
        metrics = {
            "stamp": stamp,
            "model": vision_cfg.get("model"),
            "image_detail": vision_cfg.get("image_detail"),
            "n": len(vrows2),
            "hold_n": sum(1 for v in vrows2 if v.get("hold")),
            "pass_n": sum(1 for v in vrows2 if not v.get("hold")),
            "sum_images_scanned": sum(int(v.get("images_scanned") or 0) for v in vrows2),
            "sum_elapsed_s": round(sum(float(v.get("elapsed_s") or 0) for v in vrows2), 2),
            "sum_prompt_tokens": sum(int(v.get("prompt_tokens") or 0) for v in vrows2),
            "sum_completion_tokens": sum(int(v.get("completion_tokens") or 0) for v in vrows2),
            "sum_cny": round(sum(float(v.get("cny") or 0) for v in vrows2), 4),
            "avg_s_per_link": round(
                sum(float(v.get("elapsed_s") or 0) for v in vrows2) / max(1, len(vrows2)), 2
            ),
            "avg_images_per_link": round(
                sum(int(v.get("images_scanned") or 0) for v in vrows2) / max(1, len(vrows2)), 2
            ),
            "avg_tokens_per_link": round(
                sum(int(v.get("total_tokens") or 0) for v in vrows2) / max(1, len(vrows2)), 1
            ),
            "avg_cny_per_link": round(
                sum(float(v.get("cny") or 0) for v in vrows2) / max(1, len(vrows2)), 5
            ),
            "avg_s_per_image": round(
                sum(float(v.get("elapsed_s") or 0) for v in vrows2)
                / max(1, sum(int(v.get("images_scanned") or 0) for v in vrows2)),
                2,
            )
            if sum(int(v.get("images_scanned") or 0) for v in vrows2)
            else 0,
            "by_reason": by_reason,
            "items": vrows2,
        }
        (out_dir / f"vision_metrics_{stamp}.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    while True:
        shop_pick = pick_shop()
        if not shop_pick:
            log("两店配额已满，结束")
            break
        if max_process is not None and total_handled >= max_process:
            log(f"达到本进程上限 {max_process}，结束")
            break

        pool = list_today_success(client, today0)
        pending = [
            r
            for r in pool
            if str(r["sid"]) not in processed
            and str(r["sid"]) not in handled
        ]
        log(
            f"扫描 success={len(pool)} pending={len(pending)} "
            f"韩ok={stats[HAN_ID]['ok']} 赵ok={stats[ZHAO_ID]['ok']} "
            f"held={stats[HAN_ID]['held']+stats[ZHAO_ID]['held']} "
            f"配额剩 韩{quota.remaining(HAN_ID)} 赵{quota.remaining(ZHAO_ID)}"
        )
        if not pending:
            idle += 1
            if idle >= idle_rounds_max:
                log(f"连续 {idle} 轮无新货，结束")
                break
            log(f"暂无新货，{poll_sleep}s 后重扫 ({idle}/{idle_rounds_max})")
            time.sleep(poll_sleep)
            continue
        idle = 0

        # 一次处理一条（最早）
        row = pending[0]
        sid = str(row["sid"])
        common_id = int(row["common_id"])
        shop_id, shop_name = shop_pick
        total_handled += 1
        log(
            f"[{shop_name} #{total_handled}] gmt={row.get('gmt')} "
            f"common={common_id} sid={sid} title={(row.get('title') or '')[:60]}"
        )

        one: dict = {
            "shop_id": shop_id,
            "shop_name": shop_name,
            "source_id": sid,
            "common_id": common_id,
            "gmt": row.get("gmt"),
            "ok": False,
        }
        t0 = time.time()
        try:
            ok_text, reason_text, title = gate_text_only(
                client, row, scheme, blank, fill_urls
            )
            one["title"] = title or row.get("title") or ""
            if not ok_text:
                one["held_in_box"] = True
                one["reason"] = reason_text
                one["hold_without_publish"] = {
                    "hold": True,
                    "reason": reason_text,
                    "title": one["title"][:140],
                }
                stats[shop_id]["held"] += 1
                hold_sink.add(
                    sid=sid,
                    title=one["title"],
                    reason=reason_text,
                    shop=shop_name,
                    note="文字门禁",
                )
                log(f"  文字留库 {reason_text}")
                handled.add(sid)
            else:
                # 认领 + 识图改品上架
                def _once() -> dict:
                    detail_id = claim_to_detail(
                        client,
                        {
                            "commonCollectBoxDetailId": common_id,
                            "itemNum": sid,
                            "status": "success",
                        },
                        shop_id,
                        sid,
                        log,
                    )
                    one["detail_id"] = detail_id
                    return edit_and_publish_one(
                        client,
                        detail_id=detail_id,
                        shop_id=shop_id,
                        shop_name=shop_name,
                        scheme=scheme,
                        publish_cfg=publish_cfg,
                        blank_dir=blank,
                        fill_urls=fill_urls,
                        log=log,
                        vision_cfg=vision_cfg,
                    )

                try:
                    ep_res = _once()
                except Exception as e1:
                    if "并发" in str(e1) or "concurrent" in str(e1).lower():
                        log("  改品并发，重试一次")
                        time.sleep(2)
                        ep_res = _once()
                    else:
                        raise
                one.update(ep_res if isinstance(ep_res, dict) else {})
                hold = one.get("hold_without_publish") or {}
                vision_m = hold.get("vision") if isinstance(hold, dict) else {}
                if isinstance(vision_m, dict) and (
                    vision_m.get("images_scanned") is not None
                    or vision_m.get("prompt_tokens")
                    or vision_m.get("elapsed_s") is not None
                ):
                    one["vision_metrics"] = {
                        "source_id": sid,
                        "common_id": common_id,
                        "detail_id": one.get("detail_id"),
                        "hold": bool(hold.get("hold")),
                        "reason": hold.get("reason")
                        or vision_m.get("reason")
                        or ("ok" if one.get("ok") else "hold"),
                        "images_scanned": vision_m.get("images_scanned"),
                        "images_total": vision_m.get("images_total"),
                        "elapsed_s": vision_m.get("elapsed_s"),
                        "prompt_tokens": vision_m.get("prompt_tokens"),
                        "completion_tokens": vision_m.get("completion_tokens"),
                        "total_tokens": vision_m.get("total_tokens"),
                        "cny": vision_m.get("cny"),
                        "cache_hits": vision_m.get("cache_hits"),
                        "hits": vision_m.get("hits"),
                    }
                    log(
                        f"  识图 图={vision_m.get('images_scanned')}/{vision_m.get('images_total')} "
                        f"{vision_m.get('elapsed_s')}s ¥{vision_m.get('cny')} "
                        f"cache={vision_m.get('cache_hits')} reason={one['vision_metrics']['reason']}"
                    )
                if one.get("ok"):
                    stats[shop_id]["ok"] += 1
                    quota.add(shop_id, 1)
                    processed.add(sid)
                    handled.add(sid)
                    log(
                        f"  OK 已上架 {time.time()-t0:.1f}s detail={one.get('detail_id')} "
                        f"({stats[shop_id]['ok']}) 配额={quota.count(shop_id)}"
                    )
                elif one.get("held_in_box") or hold.get("hold"):
                    reason = str(hold.get("reason") or one.get("reason") or "hold")
                    stats[shop_id]["held"] += 1
                    handled.add(sid)
                    hold_sink.add(
                        sid=sid,
                        title=str(hold.get("title") or one.get("title") or row.get("title") or ""),
                        reason=reason,
                        shop=shop_name,
                        note="识图/改品门禁",
                    )
                    log(f"  识图/改品留库 {reason}")
                else:
                    stats[shop_id]["fail"] += 1
                    handled.add(sid)
                    hold_sink.add(
                        sid=sid,
                        title=one.get("title") or row.get("title") or "",
                        reason=str(one.get("error") or "fail")[:120],
                        shop=shop_name,
                        note="上架失败",
                    )
                    log(f"  FAIL {one.get('error') or one}")
        except Exception as e:
            stats[shop_id]["fail"] += 1
            handled.add(sid)
            one["error"] = str(e)[:300]
            hold_sink.add(
                sid=sid,
                title=row.get("title") or "",
                reason=str(e)[:160],
                shop=shop_name,
                note="异常",
            )
            log(f"  FAIL {e}")

        results.append(one)
        if total_handled % 3 == 0:
            save_state()
            write_vision_metrics()
        time.sleep(0.35)

    save_state()
    write_vision_metrics()
    # 兼容导出脚本
    alias = out_dir / f"split_publish_han100_zhao100_{stamp}.json"
    alias.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    xlsx = export_metrics_excel(stamp, out_dir, log)
    summary = {
        "stamp": stamp,
        "han": stats[HAN_ID],
        "zhao": stats[ZHAO_ID],
        "quota_han": quota.count(HAN_ID),
        "quota_zhao": quota.count(ZHAO_ID),
        "handled": len(handled),
        "hold_feishu_written": hold_sink.written,
        "report_xlsx": str(xlsx) if xlsx else None,
        "feishu_sheet_ok": hold_sink.feishu_ok,
    }
    log(f"=== 结束 {json.dumps(summary, ensure_ascii=False)} ===")
    (out_dir / f"chrono_publish_{stamp}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
