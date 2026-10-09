"""
飞书值班程序（无需 AI 操控）

每 poll_interval_seconds（默认 1 小时）查看飞书表：
- 未上架链接 < batch_size(200)：不动
- ≥ 200：对半分配小韩/小赵，采集→认领→改品→上架→回写表格
- 同一轮内循环处理，直到不足 200 或两店今日均达 daily_limit_per_shop(300)

用法：
  python run_feishu_watcher.py           # 常驻
  python run_feishu_watcher.py --once    # 只检查一轮
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from api.miaoshou_client import MiaoshouClient
from services.auto_batch import run_available_batches
from services.daily_quota import DailyQuota
from services.feishu_bitable import FeishuBitable


def load_settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}


def build_client(settings: dict) -> MiaoshouClient:
    m = settings.get("miaoshou") or {}
    return MiaoshouClient(
        app_key=str(m.get("app_key") or ""),
        app_secret=str(m.get("app_secret") or ""),
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 120),
        min_interval=float(m.get("min_request_interval_seconds") or 1.2),
        max_retries=3,
    )


def one_cycle(settings: dict) -> dict:
    fcfg = settings.get("feishu") or {}
    paths = settings.get("paths") or {}
    out_dir = Path(paths.get("preview_dir") or ROOT / "data" / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "feishu_watcher.log"

    def log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    feishu = FeishuBitable(
        app_id=str(fcfg["app_id"]),
        app_secret=str(fcfg["app_secret"]),
        app_token=str(fcfg["bitable_app_token"]),
        table_id=str(fcfg["bitable_table_id"]),
    )
    quota = DailyQuota(
        Path(paths.get("db_path") or ROOT / "data" / "runs.db").parent / "daily_quota.json",
        limit_per_shop=int(fcfg.get("daily_limit_per_shop") or 300),
    )
    scheme_name = settings.get("default_scheme") or "default"
    scheme = json.loads((ROOT / "config" / "schemes" / f"{scheme_name}.json").read_text(encoding="utf-8"))
    publish_cfg = yaml.safe_load((ROOT / "config" / "publish_xiaozhao.yaml").read_text(encoding="utf-8")) or {}
    blank = Path(paths.get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]

    log(
        f"检查开始 batch={fcfg.get('batch_size')} "
        f"quota={quota.snapshot([int(s['shop_id']) for s in (fcfg.get('split') or [])])}"
    )
    summary = run_available_batches(
        client=build_client(settings),
        feishu=feishu,
        fcfg=fcfg,
        scheme=scheme,
        publish_cfg=publish_cfg,
        blank_dir=blank,
        fill_urls=fill_urls,
        quota=quota,
        out_dir=out_dir,
        log=log,
    )
    log(f"本轮结束: {json.dumps(summary, ensure_ascii=False)}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="飞书满200自动入库改品上架值班")
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出")
    ap.add_argument("--interval", type=int, default=0, help="覆盖轮询秒数（默认读配置 3600）")
    args = ap.parse_args()

    settings = load_settings()
    fcfg = settings.get("feishu") or {}
    interval = int(args.interval or fcfg.get("poll_interval_seconds") or 3600)

    print(
        f"飞书值班启动 once={args.once} interval={interval}s "
        f"batch={fcfg.get('batch_size')} daily_limit={fcfg.get('daily_limit_per_shop')}",
        flush=True,
    )

    while True:
        try:
            one_cycle(settings)
        except KeyboardInterrupt:
            print("收到中断，退出", flush=True)
            return 0
        except Exception:
            print("本轮异常:\n" + traceback.format_exc(), flush=True)
            out = Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews")
            out.mkdir(parents=True, exist_ok=True)
            (out / "feishu_watcher_errors.log").open("a", encoding="utf-8").write(
                f"\n==== {datetime.now()} ====\n{traceback.format_exc()}\n"
            )
        if args.once:
            return 0
        print(f"休眠 {interval} 秒…", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
