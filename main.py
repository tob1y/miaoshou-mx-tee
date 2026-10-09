"""
墨西哥 TK × 妙手采集箱改品 — 可用 CLI

用法:
  python main.py shops
  python main.py preview --detail-id 123 --shop-id 456
  python main.py apply   --detail-id 123 --shop-id 456 --confirm
  python main.py dry-run --sample demo
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import yaml
from rich.console import Console
from rich.panel import Panel

from api.miaoshou_client import MiaoshouClient, MiaoshouError
from db.app_db import RunDB
from services.transformer import transform_product
from services.publisher import publish_details
from services.publish_prep import apply_publish_prep, build_publish_api_body

console = Console(force_terminal=True)


def load_settings() -> dict:
    path = ROOT / "config" / "settings.yaml"
    if not path.exists():
        path = ROOT / "config" / "settings.example.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_scheme(name: str | None = None) -> dict:
    settings = load_settings()
    scheme_name = name or settings.get("default_scheme") or "default"
    path = ROOT / "config" / "schemes" / f"{scheme_name}.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_publish_config(name: str | None = None) -> dict:
    """默认小赵1店发布弹窗配置 config/publish_xiaozhao.yaml"""
    fname = name or "publish_xiaozhao"
    if not fname.endswith((".yaml", ".yml")):
        fname = f"{fname}.yaml"
    path = ROOT / "config" / fname
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def make_client(settings: dict) -> MiaoshouClient:
    import os

    m = settings.get("miaoshou") or {}
    app_key = str(os.getenv("MIAOSHOU_APP_KEY") or m.get("app_key") or "")
    app_secret = str(os.getenv("MIAOSHOU_APP_SECRET") or m.get("app_secret") or "")
    return MiaoshouClient(
        app_key=app_key,
        app_secret=app_secret,
        base_url=str(m.get("base_url") or "https://openapi-erp.91miaoshou.com"),
        timeout=float(m.get("timeout_seconds") or 60),
        min_interval=float(m.get("min_request_interval_seconds") or 0.8),
        sign_join=str(m.get("sign_join") or "concat"),
    )


def sample_product() -> dict:
    return {
        "title": "Camiseta Verde Militar con Diseño Abstracto, Disponible en Verde y Negro, Talla M L XL",
        "imgUrls": ["https://example.com/1.jpg", "https://example.com/2.jpg"],
        "skuPropertyList": [
            {
                "name": "Color",
                "attrValueList": [
                    {"attrValue": "Negro"},
                    {"attrValue": "Verde"},
                    {"attrValue": "Blanco"},
                ],
            },
            {
                "name": "Talla",
                "attrValueList": [
                    {"attrValue": "M"},
                    {"attrValue": "L"},
                    {"attrValue": "XL"},
                    {"attrValue": "XXL"},
                ],
            },
        ],
        "skuMap": {
            "Negro;M": {"price": 199, "stock": 10, "isDelete": False},
            "Negro;L": {"price": 199, "stock": 10, "isDelete": False},
            "Verde;M": {"price": 199, "stock": 10, "isDelete": False},
            "Blanco;XL": {"price": 199, "stock": 10, "isDelete": False},
            "Negro;XXL": {"price": 199, "stock": 5, "isDelete": False},
        },
        "weight": 180,
    }


def save_preview(preview_dir: Path, payload: dict, tag: str) -> Path:
    preview_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = preview_dir / f"{tag}_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def print_report(report: dict) -> None:
    console.print(Panel.fit("改品预览", title="preview"))
    console.print("[bold]颜色[/bold]:", report.get("colors"))
    console.print("[bold]尺码[/bold]:", report.get("sizes"))
    title = report.get("title") or {}
    console.print("[bold]原标题[/bold]:", title.get("original"))
    console.print("[bold]新标题[/bold]:", title.get("proposed"))
    console.print("[bold]主图[/bold]:", report.get("images"))


def cmd_shops(_: argparse.Namespace) -> int:
    settings = load_settings()
    client = make_client(settings)
    try:
        body = client.get_shop_list()
    except MiaoshouError as e:
        console.print(f"[red]{e}[/red]")
        return 1
    console.print_json(json.dumps(body, ensure_ascii=False))
    return 0


def cmd_dry_run(_: argparse.Namespace) -> int:
    settings = load_settings()
    scheme = load_scheme()
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    report = transform_product(sample_product(), scheme, blank, fill_image_urls=fill_urls)
    print_report(report)
    path = save_preview(
        Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews"),
        {
            "scheme": scheme,
            "colors": report.get("colors"),
            "sizes": report.get("sizes"),
            "title": report.get("title"),
            "images": report.get("images"),
            "product": report.get("product"),
        },
        "dryrun",
    )
    console.print(f"已保存: {path}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    settings = load_settings()
    scheme = load_scheme(args.scheme)
    client = make_client(settings)
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    try:
        oss, product = client.get_shop_detail(args.detail_id, args.shop_id)
    except MiaoshouError as e:
        console.print(f"[red]{e}[/red]")
        return 1
    report = transform_product(product, scheme, blank, fill_image_urls=fill_urls)
    print_report(report)
    payload = {
        "detail_id": args.detail_id,
        "shop_id": args.shop_id,
        "ossMd5": oss,
        "scheme": scheme,
        "report": {k: v for k, v in report.items() if k != "product"},
        "product": report["product"],
    }
    path = save_preview(
        Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews"),
        payload,
        f"d{args.detail_id}_s{args.shop_id}",
    )
    db = RunDB(Path((settings.get("paths") or {}).get("db_path") or ROOT / "data" / "runs.db"))
    db.log(args.detail_id, args.shop_id, "preview", payload)
    console.print(f"预览已保存: {path}")
    console.print("确认无误后执行: python main.py apply --detail-id ... --shop-id ... --confirm")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    if not args.confirm:
        console.print("未加 --confirm，拒绝写回。请先 preview。")
        return 2
    settings = load_settings()
    scheme = load_scheme(args.scheme)
    client = make_client(settings)
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    fill_urls = [str(u) for u in (settings.get("fill_image_urls") or []) if str(u).startswith("http")]
    db = RunDB(Path((settings.get("paths") or {}).get("db_path") or ROOT / "data" / "runs.db"))
    try:
        oss, product = client.get_shop_detail(args.detail_id, args.shop_id)
        report = transform_product(product, scheme, blank, fill_image_urls=fill_urls)
        print_report(report)
        pending = (report["product"] or {}).get("_pending_local_images") or []
        if pending:
            console.print(
                f"[yellow]注意: 有 {len(pending)} 张本地补图，开放平台需图床 URL；"
                f"请在 settings.yaml 填写 fill_image_urls。本次不写入这些本地路径。[/yellow]"
            )
            report["product"].pop("_pending_local_images", None)
        client.save_shop_detail(oss, report["product"], args.detail_id, args.shop_id)
        # 回读校验（CONFIRMED 流程）
        oss2, saved = client.get_shop_detail(args.detail_id, args.shop_id)
        errors = verify_basic(saved, report["product"], scheme)
        if errors:
            db.log(args.detail_id, args.shop_id, "verify_failed", {"errors": errors}, "; ".join(errors))
            console.print("[red]保存后校验失败:[/red]", errors)
            return 1
        db.log(args.detail_id, args.shop_id, "completed", {"title": saved.get("title")})
        console.print("[green]保存并回读校验通过[/green]")
        console.print("标题:", saved.get("title"))
        return 0
    except MiaoshouError as e:
        db.log(args.detail_id, args.shop_id, "failed", error=str(e))
        console.print(f"[red]{e}[/red]")
        return 1


def verify_basic(actual: dict, expected: dict, scheme: dict) -> list[str]:
    errors: list[str] = []
    title = str(actual.get("title") or "")
    if "algod" not in title.lower():
        errors.append("标题缺少 Algodón")
    if "tallas s-3xl" not in title.lower():
        errors.append("标题缺少 Tallas S-3XL")
    if "playera" not in title.lower() and "camiseta" not in title.lower():
        errors.append("标题缺少 Playera/Camiseta")
    # size presence
    props = actual.get("skuPropertyList") or []
    size_labels = []
    for p in props:
        name = str(p.get("name") or p.get("attrName") or "").lower()
        if "talla" in name or "size" in name:
            for v in p.get("attrValueList") or []:
                if isinstance(v, dict) and not v.get("isDelete"):
                    size_labels.append(str(v.get("attrValue") or ""))
    want = [str(x).upper() for x in (scheme.get("sizes") or [])]
    have = {x.upper() for x in size_labels}
    for w in want:
        if w not in have and w.replace("2XL", "XXL").replace("3XL", "XXXL") not in have:
            # accept either write style
            alt = {"2XL": "XXL", "3XL": "XXXL", "XXL": "2XL", "XXXL": "3XL"}.get(w, w)
            if alt not in have:
                errors.append(f"缺少尺码 {w}")
    return errors


def cmd_doctor(_: argparse.Namespace) -> int:
    settings = load_settings()
    client = make_client(settings)
    blank = Path((settings.get("paths") or {}).get("blank_tee_dir") or ROOT / "assets" / "blank_tees")
    scheme_path = ROOT / "config" / "schemes" / f"{settings.get('default_scheme') or 'default'}.json"
    pub_path = ROOT / "config" / "publish_xiaozhao.yaml"
    console.print(Panel.fit("环境自检", title="doctor"))
    console.print("密钥已配置:", client.configured())
    console.print("sign_join:", (settings.get("miaoshou") or {}).get("sign_join"))
    console.print("方案文件:", scheme_path.exists(), scheme_path)
    console.print("发布配置:", pub_path.exists(), pub_path)
    imgs = list(blank.glob("*")) if blank.exists() else []
    console.print("素材图数量:", len([p for p in imgs if p.suffix.lower() in {'.png','.jpg','.jpeg','.webp'}]), blank)
    # local regression
    import subprocess

    r = subprocess.run([sys.executable, str(ROOT / "tests" / "overnight_regression.py")], cwd=str(ROOT))
    console.print("本地回归 exit:", r.returncode)
    if client.configured():
        try:
            body = client.get_shop_list()
            console.print("[green]shops API 调用成功[/green]")
            console.print(str(body)[:500])
        except MiaoshouError as e:
            console.print("[yellow]shops API 失败（可改 sign_join 再试）:[/yellow]", e)
    else:
        console.print("[yellow]未配置密钥，跳过线上 shops 测试。填 settings.yaml 或环境变量 MIAOSHOU_APP_KEY/SECRET[/yellow]")
    return 0 if r.returncode == 0 else 1


def cmd_publish_dry_config(_: argparse.Namespace) -> int:
    """只打印发布配置 + 样例 API body，不调发布接口、不改商品。"""
    cfg = load_publish_config()
    sample_ids = [0]
    body = build_publish_api_body(sample_ids, [int(cfg["shop_id"])], cfg)
    demo = {
        "title": "A" * 300,
        "weight": 1,
        "notes": "x" * 20,
        "skuPropertyList": [
            {
                "attrName": "N" * 40,
                "attrValueList": [{"attrValue": "V" * 80}],
            }
        ],
        "skuMap": {"k": {"platformSku": "P" * 100}},
    }
    prep = apply_publish_prep(demo, cfg)
    console.print(Panel.fit("发布配置（小赵1店弹窗标准）", title="publish-config"))
    console.print_json(json.dumps(cfg, ensure_ascii=False))
    console.print(Panel.fit("本地预处理样例", title="prep"))
    console.print("prep actions:", prep.get("actions"))
    console.print("title len:", len(demo.get("title") or ""))
    console.print("attrName:", demo["skuPropertyList"][0]["attrName"])
    console.print("attrValue:", demo["skuPropertyList"][0]["attrValueList"][0]["attrValue"])
    console.print(Panel.fit("将提交的 API body 结构", title="api-body"))
    console.print_json(json.dumps(body, ensure_ascii=False))
    console.print("[yellow]未调用发布接口；真实发布用: python main.py publish --detail-id ... --confirm[/yellow]")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """发布到小赵1店。默认 dry-run；加 --confirm 才真正创建发布任务。"""
    cfg = load_publish_config(args.config)
    if args.shop_id:
        cfg["shop_id"] = int(args.shop_id)
    settings = load_settings()
    client = make_client(settings)

    detail_ids: list[int] = []
    if args.detail_id:
        detail_ids.extend(int(x) for x in args.detail_id)
    if args.detail_ids_file:
        text = Path(args.detail_ids_file).read_text(encoding="utf-8")
        for line in text.replace(",", "\n").splitlines():
            line = line.strip()
            if line.isdigit():
                detail_ids.append(int(line))
    detail_ids = list(dict.fromkeys(detail_ids))
    if not detail_ids:
        console.print("[red]请提供 --detail-id 或 --detail-ids-file[/red]")
        return 2

    dry_run = not bool(args.confirm)
    if dry_run:
        console.print("[cyan]DRY-RUN：只预处理/组包，不调用发布接口、不写回商品[/cyan]")
    else:
        console.print("[red]CONFIRM：将真实创建发布任务 → 小赵1店[/red]")

    try:
        # dry_run 路径：不 claim/save；publisher 内部 dry_run 仍可能 get_shop_detail
        # 为「先不做商品」模式：加 --offline 只组包
        if args.offline:
            body = build_publish_api_body(detail_ids, [int(cfg["shop_id"])], cfg)
            result = {
                "offline": True,
                "dry_run": True,
                "shop_id": cfg.get("shop_id"),
                "detail_ids": detail_ids,
                "would_post": body,
                "config_snapshot": {
                    "publish_mode": cfg.get("publish_mode"),
                    "package": cfg.get("package"),
                    "auto_translate": cfg.get("auto_translate"),
                    "anti_duplicate": cfg.get("anti_duplicate"),
                },
            }
        else:
            result = publish_details(
                client,
                detail_ids,
                cfg,
                dry_run=dry_run,
                confirm=bool(args.confirm),
                prep_before_publish=not bool(args.skip_prep),
            )
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        return 1

    path = save_preview(
        Path((settings.get("paths") or {}).get("preview_dir") or ROOT / "data" / "previews"),
        result,
        "publish_dryrun" if dry_run else "publish",
    )
    console.print_json(json.dumps({k: result[k] for k in result if k != "prep"}, ensure_ascii=False)[:4000])
    console.print(f"报告已保存: {path}")
    return 0


def cmd_publish_status(args: argparse.Namespace) -> int:
    settings = load_settings()
    cfg = load_publish_config(args.config)
    client = make_client(settings)
    shop_id = int(args.shop_id or cfg.get("shop_id") or 0) or None
    try:
        body = client.search_publish_tasks(page_no=args.page, page_size=args.page_size, shop_id=shop_id)
    except MiaoshouError as e:
        console.print(f"[red]{e}[/red]")
        return 1
    console.print_json(json.dumps(body, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="妙手 TK 采集箱改品")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("doctor", help="环境自检 + 本地回归")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("shops", help="列出店铺（测签名/权限）")
    s.set_defaults(func=cmd_shops)

    s = sub.add_parser("dry-run", help="本地样例预览，不调 API")
    s.set_defaults(func=cmd_dry_run)

    s = sub.add_parser("preview", help="拉取商品并生成预览，不写回")
    s.add_argument("--detail-id", type=int, required=True)
    s.add_argument("--shop-id", type=int, required=True)
    s.add_argument("--scheme", default=None)
    s.set_defaults(func=cmd_preview)

    s = sub.add_parser("apply", help="拉取→改品→保存→回读校验")
    s.add_argument("--detail-id", type=int, required=True)
    s.add_argument("--shop-id", type=int, required=True)
    s.add_argument("--scheme", default=None)
    s.add_argument("--confirm", action="store_true")
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("publish-config", help="打印小赵1店发布弹窗标准（不调接口）")
    s.set_defaults(func=cmd_publish_dry_config)

    s = sub.add_parser("publish", help="发布到店铺（默认 dry-run；--confirm 才真发布）")
    s.add_argument("--detail-id", type=int, action="append", default=[], help="可重复传入")
    s.add_argument("--detail-ids-file", default=None, help="文本文件，每行或逗号分隔 detailId")
    s.add_argument("--shop-id", type=int, default=None, help="默认读 publish_xiaozhao.yaml=小赵1店")
    s.add_argument("--config", default="publish_xiaozhao", help="config/ 下发布配置名")
    s.add_argument("--skip-prep", action="store_true", help="跳过发布前重量/截断写回")
    s.add_argument("--offline", action="store_true", help="完全离线组包，不读商品、不调 API")
    s.add_argument("--confirm", action="store_true", help="确认真实创建发布任务")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("publish-status", help="查询发布记录")
    s.add_argument("--shop-id", type=int, default=None)
    s.add_argument("--config", default="publish_xiaozhao")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--page-size", type=int, default=20)
    s.set_defaults(func=cmd_publish_status)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
