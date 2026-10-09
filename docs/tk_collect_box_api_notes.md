# TK 采集箱 OpenAPI（用户粘贴整理）

来源：妙手开放平台 Apifox 文档（用户分批粘贴）

## 已收录路径

| 接口 | Path |
|------|------|
| 类目树 | `/open/v1/product/collect_box/tiktok/collect_box/get_category_tree_by_site` body:`{site}` |
| 类目属性 | `/open/v1/product/collect_box/tiktok/collect_box/get_category_metadata` body:`{cid, site?, shopIds?}` |
| 仓库列表 | `/open/v1/product/collect_box/tiktok/collect_box/get_shop_warehouse_list` body:`{shopIds}` |
| 制造商 | `/open/v1/product/collect_box/tiktok/collect_box/get_manufacturer_list` body:`{shopId, refresh}` |
| 欧盟责任人 | `/open/v1/product/collect_box/tiktok/collect_box/get_responsible_person_list` body:`{shopId, refresh}` |
| 认领店铺 | `/open/v1/product/collect_box/tiktok/collect_box/claim_to_shop` body:`{shopIds, detailIds}` |
| 采集箱列表 | `/open/v1/product/collect_box/tiktok/collect_box/search_collect_box_detail_list` |
| 店铺模式详情 | `/open/v1/product/collect_box/tiktok/collect_box/get_shop_collect_item_info` `{detailId, shopId}` |
| 站点模式详情 | `/open/v1/product/collect_box/tiktok/collect_box/get_site_collect_item_info` `{detailId, site}` |
| 保存站点模式 | `/open/v1/product/collect_box/tiktok/collect_box/save_site_collect_item_info` |
| 产品翻译 | `/open/v1/product/collect_box/tiktok/collect_box/translate_collect_item_info` |
| AI匹配类目 | `/open/v1/product/collect_box/tiktok/collect_box/ai_match_cid_by_shop_ids` |
| AI属性匹配 | `/open/v1/product/collect_box/tiktok/collect_box/ai_match_product_attribute` |
| 品牌列表 | `/open/v1/product/collect_box/tiktok/collect_box/get_brand_list` |
| 发布产品 | `/open/v1/product/collect_box/tiktok/collect_box/save_move_collect_task` `{shopIds, detailIds}` + 可选扩展 |
| 发布记录 | `/open/v1/product/collect_box/tiktok/move_collect/search_move_collect_list` |

本地发布弹窗标准：`config/publish_xiaozhao.yaml`（小赵1店）

CLI：
- `python main.py publish-config` — 打印配置，不调接口
- `python main.py publish --detail-id ... --offline` — 离线组包
- `python main.py publish --detail-id ...` — dry-run
- `python main.py publish --detail-id ... --confirm` — 真实发布
- `python main.py publish-status` — 发布记录
| 定价模板 | `/open/v1/product/collect_box/tiktok/price_template/get_price_template_list` |
| AI白底图 | `/open/v1/product/picture/matting/auto_ai_matting_multi` |
| AI智能消除 | `/open/v1/product/common/image_removal/remove_image` |
| 翻译语言配置 | `/open/v1/product/common/translate/get_support_language_config` |
| 图片翻译 | `/open/v1/product/common/translate/translate_image` |
| AI生成标题/描述 | `/open/v1/product/common/open_ai/generate_product_info` |
| AI语言map | `/open/v1/product/common/open_ai/get_language_name_code_map` |
| AI名称列表 | `/open/v1/product/common/open_ai/get_generate_product_info_support_ai_name_list` |
| AI润色规格 | `/open/v1/product/common/open_ai/generate_sku_spec_name` |

## 采集箱列表 filter（关键）

```json
{
  "pageNo": 1,
  "pageSize": 500,
  "filter": {
    "status": "notPublished",
    "sourceItemIdKeyword": ""
  }
}
```

`status`：`notPublished` 未发布 / `timingPublish` 定时发布 / `published` 已发布

## skuMap key 规则（关键）

格式：`;属性值ID1;属性值ID2;`  
**属性值 ID 从小到大排序**，分号分隔，首尾带分号。

## 重量

`weight` 单位 **kg**，范围 0.001～100。

## 图片 / 补主图（官方说明）

开放平台**暂无直接图片上传 API**。API 改品只能传 **公网图片 URL**（自建/第三方图床）。

流程：本地通用主图 → 上传图床 → 把 URL 写入 `config/settings.yaml` 的 `fill_image_urls`（按顺序）→ 改品时补到 9 张。

本地备份目录：`E:/tk-miaoshou-rework/assets/blank_tees`（001~008.png）

## 分组 / 备注（OpenAPI 现状 2026-09-15）

账号内已见分组：`郭建钢`(1053846)、`小赵`(1053850)；新认领未发布货常落在「郭建钢」或空分组。

实测：
- `save_shop_collect_item_info` 写入 `remark` / `collectBoxGroupId` **不生效**（保存 success，列表仍空备注、分组不变）
- 开放路径探测：`move_*group` / `update_remark` / `get_*group_list` 等均为 `routeNotFound`
- 列表 `filter.collectBoxGroupId` 亦不生效

因此 **API 暂无法**：把商品移到「小韩/小赵」组、或在妙手采集箱写备注。  
门禁原因改写飞书列「门禁原因」；店铺写「分配店铺」。

## 尚未接通

- 小红旗 / `isMark`
- **引用模板 / 应用定价模板**（列表对本账号为空）
- **采集箱分组移动 / 备注写入**（见上）
