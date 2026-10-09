# 墨西哥短袖 T 恤 · 妙手改品上架

私有仓库。给同事用 **源码 + Python**，不要只拷 `dist` 里的 exe。

密钥、店铺 ID、识图 Key **不要提交**。每人复制模板后自己填。

## 别人电脑怎么跑

1. 安装 [Python 3.11+](https://www.python.org/downloads/)（勾选 Add to PATH）
2. 克隆本仓库
3. 安装依赖、准备配置：

```bat
cd /d 本仓库目录
python -m pip install -r requirements.txt
copy config\settings.example.yaml config\settings.yaml
```

4. 编辑 `config\settings.yaml`：填妙手 `app_key` / `app_secret`、自己的店铺 ID、识图 `vision.api_key`
5. 测连通：

```bat
python main.py shops
```

6. 日常半袖流水线（公共箱已采集 success 后）：

```bat
python scripts\screen_today_success_vision.py
python scripts\publish_usable_han_then_zhao.py --queue=data\previews\可用队列.json
```

改色/尺码方案在 `config\schemes\default.json`（短袖）。桌面 GUI：`python gui_app.py`。

## 不会进仓库的东西

- `config/settings.yaml`（真密钥）
- `data/`（识图日志、上架队列、报表）
- `dist/`（本机打包产物）

## 分享权限

仓库默认 **private**。在 GitHub 仓库页 → Settings → Collaborators 加同事账号。
