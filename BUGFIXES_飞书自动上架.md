# 飞书自动上架 — 已记录并修正的 Bug

试跑两链（2026-09-11）中踩到的问题，均已写入代码。

## BUG-1 短链直接采集易 504
- **现象**：`vt.tiktok.com/...` 调妙手 `fetch_item` 网关超时
- **修正**：`link_resolve.resolve_tiktok_link` 先解析出商品 ID，再用  
  `https://shop.tiktok.com/mx/pdp/{id}` 采集

## BUG-2 按「最新公共箱」模糊匹配会认领错品
- **现象**：采集失败后用 recency 匹配到无关旧商品
- **修正**：只用 `sourceItemId` / `itemNum` 精确匹配；禁止 recency fallback

## BUG-3 重复采集得到 `status=skip`
- **现象**：已采过的商品再次 fetch 返回 skip（产品已经采集过），认领报「未采集成功」
- **修正**：`pick_success_public` 只认 `status=success` 的最早一条

## BUG-4 公共箱已认领到 A 店后，B 店 CLAIM_PUBLIC 映射为空
- **现象**：`认领映射 {}`，随后 `detailIds必填`
- **修正**：`claim_to_detail` 空映射时查 TikTok 采集箱已有 `collectBoxDetailId`，再 `claim_to_shops` 到目标店

## BUG-5 防重上架按全局 published 过滤，第二家店被跳过
- **现象**：`skipped_published`，同 detail 在 A 店发过后 B 店发不出去
- **修正**：
  1. `publisher.filter_unpublished` 增加 `shop_id`，仅当本店已在 published 列表才跳过
  2. 自动值班路径默认对本批关闭 `filter_published`（两店分发场景）

## BUG-6 飞书状态值不是「成功/失败」
- **现象**：表字段单选项为「是 / 否」
- **修正**：配置 `yes_value`/`no_value`，回写用「是」「否」

## BUG-7 批次与日限策略（产品需求）
- 满 **200** 才跑；不足不动
- 每小时看一次表
- 同一检查周期内循环，直到不足 200
- 单店每天最多 **300**；两店都满则当日停止

---

程序入口：`run_feishu_watcher.py` / `启动飞书值班.bat`  
日志：`data/previews/feishu_watcher.log`  
日配额：`data/daily_quota.json`
