# 妙手 OpenAPI 签名失败 — 请帮我算出正确签名算法

## 目标
用 Python 调用妙手 ERP 开放平台，先跑通最简单的「店铺列表」接口。  
当前卡死在 **签名验证不通过 (`signInvalid`)**。

成功标准：下面这个请求返回 `result=success`（或非 signInvalid），并能列出店铺。

---

## 已知确定信息（已从第三方客户端反编译确认）

### Base URL
```
https://openapi-erp.91miaoshou.com
```

### 测试接口（最简单）
```
POST /open/v1/product/shop/shop/get_shop_list
Body: {}
```

### 请求头（已确认字段名）
```
x-app-key: <AppId，形如 ak_ + 32位hex，总长35>
x-timestamp: <Unix 秒级时间戳字符串，如 "1725860000">
x-sign: <签名>
Content-Type: application/json
```

### Body 序列化（已确认）
```python
json.dumps(data, ensure_ascii=False, separators=(',', ':'))
# 空对象就是 "{}"
```

### 签名函数参数（已确认参数名与顺序）
```python
def generate_sign(app_secret, path, timestamp, app_key, body_json) -> str:
    ...
```
- 算法确认用了：`hmac` + `hashlib.sha256` + `.hexdigest()`
- 常量里只有 `'utf-8'`，**没有** `&`、`=` 等分隔符常量（强烈暗示是字符串直接拼接）
- 推测（但线上验证失败）：
```python
content = str(path) + str(timestamp) + str(app_key) + str(body_json)
return hmac.new(app_secret.encode('utf-8'), content.encode('utf-8'), hashlib.sha256).hexdigest()
```

### 凭证格式（我已配置）
| 项 | 格式 |
|----|------|
| app_key | `ak_` + 32 hex，长度 **35**（去掉空格后） |
| app_secret | 64 位 hex，长度 **64** |
| 权限 | 开放平台已审核通过 |

> 请把真实 `app_key` / `app_secret` 从本地 `config/settings.yaml` 复制到下面测试脚本里（不要泄露到公开仓库）。

---

## 当前失败响应（HTTP 200）
```json
{
  "result": "fail",
  "code": "signInvalid",
  "message": "签名验证不通过",
  "data": null
}
```
说明：服务器能识别到请求格式，但 **HMAC 明文拼法或时间戳单位或密钥用法不对**。  
不是「缺权限」那种错误码（若密钥完全无效，有时也会仍报 signInvalid，需区分）。

---

## 我已经试过、全部仍 signInvalid 的变体

1. `path + timestamp + app_key + body_json`（当前默认）
2. `path + timestamp + body_json`（不含 app_key）
3. `app_key + timestamp + path + body_json`
4. `path=&timestamp=&app_key=&body=` 带 `&` 拼接
5. path 去掉前导 `/`
6. content = `base_url + path + ...`
7. timestamp 用毫秒 / 用日期时间字符串
8. header 大小写变体（`X-App-Key` 等）
9. 签名结果 upper()
10. MD5 代替 HMAC（失败）
11. body 参与签名时用空字符串 `""` vs `"{}"`
12. AppId 尾部空格：已去掉；客户端也会 `"".join(s.split())` 清洗空白

---

## 最小可复现脚本（请改签名直到成功）

```python
import hashlib, hmac, json, time, requests

BASE = "https://openapi-erp.91miaoshou.com"
PATH = "/open/v1/product/shop/shop/get_shop_list"
APP_KEY = "粘贴你的ak_..."
APP_SECRET = "粘贴你的64位secret..."

body_json = json.dumps({}, ensure_ascii=False, separators=(',', ':'))
ts = str(int(time.time()))  # 若失败可改试 str(int(time.time()*1000))

# === 请修改这里的 content 拼法 ===
content = PATH + ts + APP_KEY + body_json
sign = hmac.new(APP_SECRET.encode('utf-8'), content.encode('utf-8'), hashlib.sha256).hexdigest()

headers = {
    "x-app-key": APP_KEY,
    "x-timestamp": ts,
    "x-sign": sign,
    "Content-Type": "application/json",
}
r = requests.post(BASE + PATH, data=body_json.encode('utf-8'), headers=headers, timeout=30)
print(r.status_code, r.text)
print("content=", content)
print("sign=", sign)
```

成功后请输出：
1. 最终正确的 `content` 公式  
2. timestamp 是秒还是毫秒  
3. HMAC key 是 secret 原文还是 secret 的某种变换  
4. 完整可运行 Python 函数 `generate_sign(...)`

---

## 可参考的官方文档
- 开放平台介绍：https://erp.91miaoshou.com/open_platform.html  
- Apifox 共享文档（需密码，我这边 HTTP 拉不到正文）：  
  `https://s.apifox.cn/fd54e57e-9b98-4c34-bada-306221c39e68`  
  密码：`d8DUQdqH`  
  **请优先从文档里找「签名规则 / Python 示例」原样照抄。**

---

## 本地工程位置（如需对照）
- 客户端：`E:\tk-miaoshou-rework\clean_rebuild\src\api\miaoshou_client.py`
- 配置：`E:\tk-miaoshou-rework\clean_rebuild\config\settings.yaml`
- CLI：`python main.py doctor` / `python main.py shops`
- 反编译结论：`E:\tk-miaoshou-rework\reverse\reports\api_map.md`

业务逻辑（尺码/颜色/标题）本地回归已通过；**只差签名联调**。
