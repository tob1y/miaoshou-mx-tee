"""Build portable Windows zip: extract → double-click 托比改品台.exe"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "托比改品台"
STAGE = ROOT / "dist" / "托比改品台_便携版"
PARENT_ASSETS = ROOT.parent / "assets" / "blank_tees"
OUT_DIR = Path(r"E:\tk-miaoshou-rework")


def main() -> int:
    print("1) PyInstaller 打包…")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(ROOT / "tobee_gui.spec"),
    ]
    subprocess.check_call(cmd, cwd=str(ROOT))
    if not (DIST / "托比改品台.exe").exists():
        raise SystemExit(f"未找到 exe: {DIST}")

    print("2) 组装便携目录…")
    if STAGE.exists():
        shutil.rmtree(STAGE)
    shutil.copytree(DIST, STAGE)

    # 程序旁可写配置（覆盖为 portable 相对路径版）
    cfg_dst = STAGE / "config"
    cfg_dst.mkdir(exist_ok=True)
    # 从源码复制整份 config，再把 settings.yaml 换成 portable
    src_cfg = ROOT / "config"
    for p in src_cfg.rglob("*"):
        if p.is_file():
            rel = p.relative_to(src_cfg)
            target = cfg_dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
    portable = ROOT / "config" / "settings.portable.yaml"
    shutil.copy2(portable, cfg_dst / "settings.yaml")

    # 素材
    assets = STAGE / "assets" / "blank_tees"
    assets.mkdir(parents=True, exist_ok=True)
    if PARENT_ASSETS.exists():
        for p in PARENT_ASSETS.iterdir():
            if p.is_file():
                shutil.copy2(p, assets / p.name)
    # 也尝试 clean_rebuild/assets
    local_assets = ROOT / "assets" / "blank_tees"
    if local_assets.exists():
        for p in local_assets.iterdir():
            if p.is_file():
                shutil.copy2(p, assets / p.name)

    (STAGE / "data" / "previews").mkdir(parents=True, exist_ok=True)
    readme = STAGE / "使用说明_便携版.txt"
    readme.write_text(
        "托比改品台（Windows 便携版）\n"
        "====================\n\n"
        "1. 解压到任意文件夹（不要只解压 exe，请解压整个文件夹）\n"
        "2. 双击「托比改品台.exe」启动\n"
        "3. 密钥在 config\\settings.yaml（已预填；换账号请改 app_key / app_secret）\n"
        "4. 默认店铺：小赵1店；界面可切换小韩1店\n"
        "5. 需要能访问互联网（妙手开放平台）\n\n"
        "首次若被 Windows 拦截：点击「更多信息」→ 仍要运行\n",
        encoding="utf-8",
    )

    # 根目录快捷批处理（有些环境双击 exe 被拦时备用）
    (STAGE / "启动改品台.bat").write_text(
        "@echo off\r\ncd /d \"%~dp0\"\r\nstart \"\" \"托比改品台.exe\"\r\n",
        encoding="utf-8",
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    zip_path = OUT_DIR / f"托比改品台_Windows便携版_{stamp}.zip"
    print(f"3) 压缩 → {zip_path}")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in STAGE.rglob("*"):
            if p.is_file():
                zf.write(p, arcname=str(Path("托比改品台") / p.relative_to(STAGE)))

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"完成: {zip_path} ({size_mb:.1f} MB)")
    print(f"便携目录: {STAGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
