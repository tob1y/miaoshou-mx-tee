# -*- coding: utf-8 -*-
"""小赵试跑 10 条：新逻辑「只删不补 + 纯黑」真实认领改品上架。"""
from __future__ import annotations

import json
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from api.miaoshou_client import MiaoshouClient
from services.auto_batch import claim_to_detail, edit_and_publish_one
from services.daily_quota import DailyQuota

ZHAO_ID = 18545044
ZHAO_NAME = "小赵1店"
TZ = timezone(timedelta(hours=8))
PREV = ROOT / "data" / "previews"
DESK = Path.home() / "Desktop"
PREV_BATCH1 = PREV / "vision_body_color_batch10_20260923_164431.json"


def pick_10() -> list[dict]:
    src = json.loads((PREV / "no_concrete_color_han_today.json").read_text(encoding="utf-8"))
    done: set[str] = set()
    if PREV_BATCH1.exists():
        for it in json.loads(PREV_BATCH1.read_text(encoding="utf-8")).get("items") or []:
            done.add(str(it.get("source_id") or ""))
    # 也跳过已在小赵上架日志里的
    for log in PREV.glob("zhao_*.log"):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "OK 已上架" in line:
                # 粗略：同文件前一行 sid=
                pass
    pure = [
        x
        for x in (src.get("items") or [])
        if str(x.get("reason") or "") == "title_and_options_no_concrete_color"
        and x.get("common_id")
        and str(x.get("source_id") or "") not in done
    ]
    return pure[:10]


def write_report(stamp: str, rows: list[dict], summary: dict) -> Path:
    thin = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )
    hf = PatternFill("solid", fgColor="1F4E79")
    hfont = Font(color="FFFFFF", bold=True)
    lab = PatternFill("solid", fgColor="D6EAF8")
    wb = Workbook()
    ws = wb.active
    ws.title = "汇总"
    ws["A1"] = "小赵识色上架试跑（只删不补）"
    ws["A1"].font = Font(bold=True, size=14, color="1F4E79")
    ws.merge_cells("A1:B1")
    ws["A2"] = "批次"
    ws["B2"] = stamp
    ws["A3"] = "店铺"
    ws["B3"] = ZHAO_NAME
    ws["A4"] = "模型"
    ws["B4"] = f"{summary.get('model')} / delete_only_no_fill / 纯黑"
    ws["A5"] = "说明"
    ws["B5"] = (
        "无明确颜色走识图：长袖/卫衣/多件套/异色袖/看不清/非纯黑→留库；"
        "库外色删主图与选项；不填充黑白底。"
    )
    ws.merge_cells("B5:F5")
    pairs = [
        ("试跑条数", summary["n"]),
        ("上架成功", summary["ok"]),
        ("留库", summary["held"]),
        ("失败", summary["fail"]),
        ("识图费用合计(元)", summary["sum_cny"]),
        ("平均每条识图秒", summary["avg_vision_s"]),
    ]
    ws["A7"] = "指标"
    ws["B7"] = "数值"
    ws["A7"].fill = hf
    ws["B7"].fill = hf
    ws["A7"].font = hfont
    ws["B7"].font = hfont
    for i, (k, v) in enumerate(pairs, 8):
        ws[f"A{i}"] = k
        ws[f"B{i}"] = v
        ws[f"A{i}"].fill = lab
        ws[f"A{i}"].border = thin
        ws[f"B{i}"].border = thin
    ws["A15"] = "原因分布"
    ws["A15"].font = Font(bold=True)
    ws["A16"] = "原因"
    ws["B16"] = "条数"
    ws["A16"].fill = hf
    ws["B16"].fill = hf
    ws["A16"].font = hfont
    ws["B16"].font = hfont
    for i, (r, n) in enumerate(sorted(summary["by_reason"].items(), key=lambda x: -x[1]), 17):
        ws[f"A{i}"] = r
        ws[f"B{i}"] = n
        ws[f"A{i}"].border = thin
        ws[f"B{i}"].border = thin
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 40

    ws2 = wb.create_sheet("明细")
    headers = [
        "序号",
        "货源ID",
        "detail_id",
        "标题",
        "结果",
        "原因",
        "映射色",
        "识图动作",
        "扫图",
        "识图秒",
        "费用元",
        "链接",
    ]
    for col, h in enumerate(headers, 1):
        cell = ws2.cell(1, col, h)
        cell.fill = hf
        cell.font = hfont
        cell.border = thin
        cell.alignment = Alignment(wrap_text=True, horizontal="center")
    for i, r in enumerate(rows, 1):
        hold = r.get("hold_without_publish") or {}
        vision = hold.get("vision") if isinstance(hold.get("vision"), dict) else {}
        if r.get("ok"):
            result = "上架成功"
            reason = "ok"
        elif r.get("held_in_box") or hold.get("hold"):
            result = "留库"
            reason = hold.get("reason") or r.get("reason") or "hold"
        else:
            result = "失败"
            reason = str(r.get("error") or r.get("reason") or "fail")[:80]
        vals = [
            i,
            r.get("source_id"),
            r.get("detail_id"),
            (r.get("title") or hold.get("title") or "")[:80],
            result,
            reason,
            ",".join(vision.get("mapped_colors") or []),
            " | ".join((vision.get("actions") or [])[:6]),
            vision.get("images_scanned"),
            vision.get("elapsed_s"),
            vision.get("cny"),
            f"https://shop.tiktok.com/mx/pdp/{r.get('source_id')}",
        ]
        for col, val in enumerate(vals, 1):
            cell = ws2.cell(i + 1, col, val)
            cell.border = thin
    ws2.column_dimensions["D"].width = 40
    ws2.column_dimensions["H"].width = 48

    out = PREV / f"识色上架试跑_小赵10_{stamp}.xlsx"
    wb.save(out)
    desk = DESK / out.name
    shutil.copy2(out, desk)
    return desk


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8"))
    scheme = json.loads((ROOT / "config/schemes/default.json").read_text(encoding="utf-8"))
    publish_cfg = yaml.safe_load((ROOT / "config/publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    m = settings["miaoshou"]
    paths = settings.get("paths") or {}
    fcfg = settings.get("feishu") or {}
    blank = Path(paths.get("blank_tee_dir") or ROOT / "assets/blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    vision_cfg = dict(settings.get("vision") or {})

    client = MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or ""),
        timeout=float(m.get("timeout_seconds") or 120),
        min_interval=1.0,
        max_retries=5,
    )
    quota = DailyQuota(
        ROOT / "data/daily_quota.json",
        limit_per_shop=int(fcfg.get("daily_limit_per_shop") or 300),
    )
    pool = pick_10()
    log_path = PREV / f"zhao_color10_{stamp}.log"
    result_path = PREV / f"zhao_color10_{stamp}.json"

    def log(msg: str) -> None:
        line = f"{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(
        f"=== 小赵识色10试跑 stamp={stamp} model={vision_cfg.get('model')} "
        f"quota_left={quota.remaining(ZHAO_ID)} n={len(pool)} ==="
    )
    rows: list[dict] = []
    stats = {"ok": 0, "held": 0, "fail": 0}

    for i, it in enumerate(pool, 1):
        if quota.remaining(ZHAO_ID) <= 0:
            log("配额已满，停止")
            break
        sid = str(it.get("source_id") or "")
        cid = int(it["common_id"])
        title = str(it.get("title") or "")[:100]
        log(f"[{i}/10] sid={sid} common={cid}")
        one: dict = {
            "shop_id": ZHAO_ID,
            "shop_name": ZHAO_NAME,
            "source_id": sid,
            "common_id": cid,
            "title": title,
            "ok": False,
        }
        try:
            detail_id = claim_to_detail(
                client,
                {
                    "commonCollectBoxDetailId": cid,
                    "itemNum": sid,
                    "status": "success",
                },
                ZHAO_ID,
                sid,
                log,
            )
            one["detail_id"] = detail_id
            ep_res = edit_and_publish_one(
                client,
                detail_id=detail_id,
                shop_id=ZHAO_ID,
                shop_name=ZHAO_NAME,
                scheme=scheme,
                publish_cfg=publish_cfg,
                blank_dir=blank,
                fill_urls=fill_urls,
                log=log,
                vision_cfg=vision_cfg,
                skip_reclaim=True,
            )
            one.update({k: v for k, v in ep_res.items() if k != "prep"})
            hold = one.get("hold_without_publish") or {}
            if one.get("ok"):
                stats["ok"] += 1
                quota.add(ZHAO_ID, 1)
                log(f"  OK 已上架 detail={detail_id} 配额={quota.count(ZHAO_ID)}")
            elif one.get("held_in_box") or hold.get("hold"):
                stats["held"] += 1
                log(f"  留库 {hold.get('reason')}")
            else:
                stats["fail"] += 1
                log(f"  FAIL detail={detail_id}")
        except Exception as e:
            stats["fail"] += 1
            one["error"] = str(e)[:300]
            log(f"  FAIL {e}")
        rows.append(one)
        result_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        time.sleep(0.4)

    by_reason: Counter[str] = Counter()
    sum_cny = 0.0
    sum_vs = 0.0
    for r in rows:
        hold = r.get("hold_without_publish") or {}
        vision = hold.get("vision") if isinstance(hold.get("vision"), dict) else {}
        sum_cny += float(vision.get("cny") or 0)
        sum_vs += float(vision.get("elapsed_s") or 0)
        if r.get("ok"):
            by_reason["ok"] += 1
        elif r.get("held_in_box") or hold.get("hold"):
            by_reason[str(hold.get("reason") or "hold")] += 1
        else:
            by_reason[str(r.get("error") or "fail")[:60]] += 1

    summary = {
        "stamp": stamp,
        "model": vision_cfg.get("model"),
        "n": len(rows),
        "ok": stats["ok"],
        "held": stats["held"],
        "fail": stats["fail"],
        "sum_cny": round(sum_cny, 5),
        "avg_vision_s": round(sum_vs / max(1, len(rows)), 2),
        "by_reason": dict(by_reason),
        "quota_zhao": quota.count(ZHAO_ID),
    }
    bundle = {"summary": summary, "items": rows}
    result_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    xlsx = write_report(stamp, rows, summary)
    log(f"DONE {summary}")
    log(f"JSON {result_path}")
    log(f"XLSX {xlsx}")


if __name__ == "__main__":
    main()
