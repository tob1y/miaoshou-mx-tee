# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 托比改品台 便携目录包"""
from pathlib import Path

root = Path(SPECPATH)

a = Analysis(
    [str(root / "gui_app.py")],
    pathex=[str(root), str(root / "src")],
    binaries=[],
    datas=[
        (str(root / "config"), "config"),
        (str(root / "src"), "src"),
        (str(root / "使用说明.txt"), "."),
    ],
    hiddenimports=[
        "yaml",
        "requests",
        "urllib3",
        "certifi",
        "charset_normalizer",
        "idna",
        "api",
        "api.miaoshou_client",
        "api.endpoints",
        "services.full_pipeline",
        "services.transformer",
        "services.publisher",
        "services.publish_prep",
        "services.auto_batch",
        "services.feishu_bitable",
        "services.link_resolve",
        "services.daily_quota",
        "rules",
        "rules.colors",
        "rules.sizes",
        "rules.titles",
        "rules.template_std",
        "db",
        "db.app_db",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["rich", "matplotlib", "numpy", "pandas"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="托比改品台",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="托比改品台",
)
