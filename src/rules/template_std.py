"""Apply standard TikTok product attributes + centered detail images."""
from __future__ import annotations

from typing import Any


# Frozen IDs for cid=601226 (男士T恤 / Camisetas). Resolved from category metadata.
ATTR_TEMPLATE_601226: list[dict[str, Any]] = [
    {"attributeId": "100397", "attributeName": "Temporada", "attributeValues": [{"valueId": "1005840", "valueName": "Todas las estaciones"}]},  # 季节=四季
    {"attributeId": "100393", "attributeName": "Cuello", "attributeValues": [{"valueId": "1001126", "valueName": "Cuello redondo"}]},  # 领型=小圆领
    {"attributeId": "100198", "attributeName": "Patrón", "attributeValues": [{"valueId": "1001186", "valueName": "Gráfico"}]},  # 图案花纹=图文一体
    {"attributeId": "100396", "attributeName": "Tipo de manga", "attributeValues": [{"valueId": "1005904", "valueName": "Normal"}]},  # 袖型=常规
    {"attributeId": "100399", "attributeName": "Ajuste", "attributeValues": [{"valueId": "1001181", "valueName": "De corte holgado"}]},  # 包容度=宽松
    {"attributeId": "100401", "attributeName": "Instrucciones de lavado", "attributeValues": [{"valueId": "1001199", "valueName": "Lavable a máquina"}]},  # 洗涤说明=可机洗
    {"attributeId": "100398", "attributeName": "Estilo", "attributeValues": [{"valueId": "1001165", "valueName": "Básico"}]},  # 风格=基本
    {"attributeId": "100400", "attributeName": "Estiramiento", "attributeValues": [{"valueId": "1001193", "valueName": "Ligero"}]},  # 弹性=微弹
    {"attributeId": "100157", "attributeName": "Materiales", "attributeValues": [{"valueId": "1000039", "valueName": "Algodón"}]},  # 材质=棉
    {"attributeId": "100392", "attributeName": "Ocasión", "attributeValues": [{"valueId": "1001124", "valueName": "Casual"}]},  # 场合=休闲
    {"attributeId": "101127", "attributeName": "Tipo de tamaño", "attributeValues": [{"valueId": "1005904", "valueName": "Normal"}]},  # 尺码类型=常规
    {"attributeId": "100395", "attributeName": "Longitud de la manga", "attributeValues": [{"valueId": "1001141", "valueName": "Manga corta"}]},  # 袖长=短袖
    {"attributeId": "100394", "attributeName": "Largo de la ropa", "attributeValues": [{"valueId": "1000907", "valueName": "Mediano"}]},  # 衣长=中
    # 每包数量强制 1（禁止上架 2/3/… 件装）
    {"attributeId": "100347", "attributeName": "Cantidad por paquete", "attributeValues": [{"valueId": "1000256", "valueName": "1"}]},
]


def build_centered_detail_html(urls: list[str]) -> str:
    parts = ['<div style="text-align:center;">']
    for u in urls:
        u = str(u).strip()
        if not u.startswith("http"):
            continue
        parts.append(
            f'<p style="text-align:center;margin:0 0 8px 0;">'
            f'<img src="{u}" style="max-width:100%;height:auto;display:inline-block;margin:0 auto;" />'
            f"</p>"
        )
    parts.append("</div>")
    return "".join(parts)


def apply_brand(product: dict[str, Any], scheme: dict[str, Any]) -> dict[str, Any]:
    brand_id = str(scheme.get("brand_id") if scheme.get("brand_id") is not None else "0")
    brand_name = str(scheme.get("brand_name") or "无品牌")
    product["brandId"] = brand_id
    product["brandName"] = brand_name
    return {"brandId": brand_id, "brandName": brand_name}


def apply_category(product: dict[str, Any], scheme: dict[str, Any]) -> dict[str, Any]:
    note: dict[str, Any] = {"before": product.get("cid")}
    if scheme.get("force_cid") and scheme.get("cid"):
        product["cid"] = str(scheme["cid"])
        note["after"] = product["cid"]
        note["forced"] = True
    else:
        note["after"] = product.get("cid")
        note["forced"] = False
    return note


PACK_QTY_ONE = {
    "attributeId": "100347",
    "attributeName": "Cantidad por paquete",
    "attributeValues": [{"valueId": "1000256", "valueName": "1"}],
}


def upsert_pack_quantity_one(product: dict[str, Any]) -> dict[str, Any]:
    """Force 每包数量=1 without wiping other attributes."""
    attrs = list(product.get("productAttributes") or [])
    found = False
    for a in attrs:
        if not isinstance(a, dict):
            continue
        if str(a.get("attributeId") or "") == "100347":
            a["attributeName"] = "Cantidad por paquete"
            a["attributeValues"] = [{"valueId": "1000256", "valueName": "1"}]
            found = True
            break
    if not found:
        attrs.append(dict(PACK_QTY_ONE))
    product["productAttributes"] = attrs
    return {"attributeId": "100347", "value": "1", "inserted": not found}


def apply_product_attributes(product: dict[str, Any], scheme: dict[str, Any]) -> dict[str, Any]:
    """Overwrite productAttributes with standard tee template."""
    cid = str(product.get("cid") or scheme.get("cid") or "")
    attrs = list(ATTR_TEMPLATE_601226)
    # allow scheme override as raw list
    if isinstance(scheme.get("product_attributes"), list) and scheme["product_attributes"]:
        attrs = scheme["product_attributes"]
    product["productAttributes"] = attrs
    return {
        "cid": cid,
        "count": len(attrs),
        "ids": [a.get("attributeId") for a in attrs],
    }


def apply_detail_images(product: dict[str, Any], scheme: dict[str, Any]) -> dict[str, Any]:
    urls = [str(u) for u in (scheme.get("detail_image_urls") or []) if str(u).startswith("http")]
    if not urls:
        return {"applied": False, "count": 0}
    html = build_centered_detail_html(urls)
    product["notes"] = html
    return {"applied": True, "count": len(urls), "html_len": len(html)}
