"""Overnight automated regression for clean_rebuild (no live API required)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from services.transformer import transform_product


def load_scheme(name: str = "default") -> dict:
    return json.loads((ROOT / "config" / "schemes" / f"{name}.json").read_text(encoding="utf-8"))


BLANK = ROOT / "assets" / "blank_tees"


def base_product(**over):
    p = {
        "title": "Camiseta Verde con Diseño Gato, Disponible en Verde y Negro",
        "imgUrls": ["https://ex/1.jpg"],
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
                    {"attrValue": "XXL"},
                ],
            },
        ],
        "skuMap": {
            "Negro;M": {"price": 1, "stock": 1, "isDelete": False},
            "Verde;M": {"price": 1, "stock": 1, "isDelete": False},
            "Blanco;XXL": {"price": 1, "stock": 1, "isDelete": False},
        },
    }
    p.update(over)
    return p


def colors_of(product):
    for prop in product.get("skuPropertyList") or []:
        name = str(prop.get("name") or "").lower()
        if "color" in name:
            return [
                v.get("attrValue")
                for v in (prop.get("attrValueList") or [])
                if isinstance(v, dict) and not v.get("isDelete")
            ]
    return []


def sizes_of(product):
    for prop in product.get("skuPropertyList") or []:
        name = str(prop.get("name") or "").lower()
        if "talla" in name or "size" in name:
            return [
                v.get("attrValue")
                for v in (prop.get("attrValueList") or [])
                if isinstance(v, dict) and not v.get("isDelete")
            ]
    return []


def test_delete_out_of_library():
    r = transform_product(base_product(), load_scheme("default"), BLANK)
    cols = colors_of(r["product"])
    assert "Verde" not in cols
    assert "Negro" in cols and "Blanco" in cols
    assert r["colors"]["mode"] == "delete_out_of_library"


def test_keep_single_black():
    p = base_product(
        title="Playera Negra 100% Algodón con Frase Divertida",
        skuPropertyList=[
            {"name": "Color", "attrValueList": [{"attrValue": "negro"}]},
            {"name": "Talla", "attrValueList": [{"attrValue": "S"}, {"attrValue": "M"}, {"attrValue": "L"}, {"attrValue": "XL"}, {"attrValue": "2XL"}, {"attrValue": "3XL"}]},
        ],
        skuMap={"negro;S": {"stock": 1, "isDelete": False}},
        imgUrls=["u"] * 9,
    )
    r = transform_product(p, load_scheme("default"), BLANK)
    assert r["colors"]["mode"] == "keep_all_in_library"
    assert r["images"]["need"] == 0
    assert "Negra" in r["product"]["title"] or "Negro" in r["product"]["title"]


def test_style_codes_skip_color():
    p = base_product(
        title="Playera Estampado Anime YT1238",
        skuPropertyList=[
            {"name": "Color", "attrValueList": [{"attrValue": "YT1238"}, {"attrValue": "YT1239"}]},
            {"name": "Talla", "attrValueList": [{"attrValue": "XL"}, {"attrValue": "2XL"}]},
        ],
        skuMap={"YT1238;XL": {"stock": 1, "isDelete": False}},
    )
    r = transform_product(p, load_scheme("default"), BLANK)
    assert r["colors"]["mode"] == "skip_style_codes"
    cols = colors_of(r["product"])
    assert "YT1238" in cols and "YT1239" in cols


def test_sizes_force_full_set():
    r = transform_product(base_product(), load_scheme("default"), BLANK)
    sizes = sizes_of(r["product"])
    assert sizes == ["S", "M", "L", "XL", "2XL", "3XL"]
    assert "S" in r["sizes"]["added"] and "3XL" in r["sizes"]["added"]


def test_title_constraints():
    r = transform_product(base_product(), load_scheme("default"), BLANK)
    t = r["product"]["title"]
    assert len(t) <= 200
    assert "100% Algod" in t
    assert "Tallas S-3XL" in t
    assert "Playera" in t
    assert "1 Pieza" in t


def test_six_color_scheme_keeps_pink():
    p = base_product(
        skuPropertyList=[
            {
                "name": "Color",
                "attrValueList": [
                    {"attrValue": "Rosa"},
                    {"attrValue": "Amarillo"},
                    {"attrValue": "Negro"},
                ],
            },
            {"name": "Talla", "attrValueList": [{"attrValue": "L"}]},
        ]
    )
    r = transform_product(p, load_scheme("six_colors"), BLANK)
    cols = colors_of(r["product"])
    assert "Rosa" in cols and "Negro" in cols
    assert "Amarillo" not in cols


def test_main_images_fill_plan():
    r = transform_product(base_product(), load_scheme("default"), BLANK)
    assert r["images"]["need"] == 8
    assert len(r["images"]["added_local"]) == 8


def test_case_insensitive_colors():
    p = base_product(
        skuPropertyList=[
            {"name": "Color", "attrValueList": [{"attrValue": "NEGRO"}, {"attrValue": "blanco"}]},
            {"name": "Talla", "attrValueList": [{"attrValue": "m"}]},
        ]
    )
    r = transform_product(p, load_scheme("default"), BLANK)
    assert r["colors"]["mode"] == "keep_all_in_library"


def main():
    tests = [
        test_delete_out_of_library,
        test_keep_single_black,
        test_style_codes_skip_color,
        test_sizes_force_full_set,
        test_title_constraints,
        test_six_color_scheme_keeps_pink,
        test_main_images_fill_plan,
        test_case_insensitive_colors,
    ]
    failed = []
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except Exception as e:
            print("FAIL", t.__name__, ":", e)
            failed.append((t.__name__, str(e)))
    report = {
        "passed": len(tests) - len(failed),
        "failed": len(failed),
        "failures": failed,
    }
    out = ROOT / "data" / "overnight_test_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
