# 售后登记表 / 理赔工作台（claims-system）

电商售后理赔登记与追款系统。登记售后事件 → 向仓库/快递索赔 → 跟踪实收 → 统计净损失。
单页前端 + FastAPI 后端，Docker 部署在群晖 NAS 上。

- 线上地址：`http://192.168.3.55:8021/`（内网）
- 当前版本：见 `VERSION`
- 技术栈：FastAPI + SQLAlchemy + SQLite/Postgres，前端为原生 JS 单页（无框架、无构建）

---

## 目录结构

```
backend/app/
  api.py            39 个接口（全部路由集中在此）
  models.py         数据模型（ClaimEvent / ClaimReply / Evidence …）
  main.py           应用入口、静态文件挂载
  security.py       登录鉴权、token 签发
  config.py         配置（从 .env 读）
  migrate.py        数据库结构迁移
  manual_api.py     人工补齐相关接口
  services/
    engine.py       业务引擎：统计汇总、事件序列化、重复检测
    wangdian.py     旺店通开放平台对接（订单/物流/入库查询）
    report.py       报表与 XLSX 导出
    permissions.py  角色与字段权限
    legacy_import.py 旧台账导入
  static/
    index.html      理赔工作台（单页，约 1900 行）
    work_orders.html
backend/tests/      回归与自检脚本
tools/check_inline_js.js   前端内联 JS 语法门禁（部署时强制执行）
scripts/git.sh      NAS 无 git，用 alpine/git 容器操作本仓库
deploy.sh           一键部署：备份 → JS 校验 → 构建 → 重启 → 冒烟
autoupdate.sh       从 GitHub 拉最新代码并自动部署（失败回滚）
```

---

## 配置

复制 `.env.example` 为 `.env` 并填写。`.env` 已被 `.gitignore` 排除，**不要提交到仓库**。

必改项：

| 变量 | 说明 |
|---|---|
| `APP_SECRET` | token 签名密钥，务必长随机串 |
| `ADMIN_PASSWORD` | 初始管理员密码，首次启动自动建号 |
| `WDT_SID` / `WDT_APPKEY` / `WDT_APPSECRET` | 旺店通开放平台凭证 |
| `DISABLE_AUTH` | 生产必须 `false` |

---

## 部署

### 生产：镜像发布（推荐）

代码推送到 `main` → GitHub Actions 自动构建并推送到 GHCR
（`ghcr.io/jinfenghua1990/jxf:sha-<SHA>` 与 `:latest`）→ NAS 拉镜像部署，**不在 NAS 上构建**。

```bash
sudo bash /volume1/docker/claims-system/deploy/update.sh                 # 更新到 .env 里 APP_IMAGE 的版本
sudo bash /volume1/docker/claims-system/deploy/update.sh ghcr.io/jinfenghua1990/jxf:sha-<SHA>
```

`deploy/update.sh` 的流程：备份数据库（`pg_dump`）与 `data/` → 拉镜像（失败重试，不动现有容器）
→ 切换 `APP_IMAGE` → 重建 app → 等健康检查 → **失败自动切回上一个镜像**。

镜像模式通过 `deploy/override-image.yml` 叠加到 `docker-compose.yml`，只覆盖 `app.image`，
数据库/备份服务配置完全共用，不维护第二份 compose。

### 本地构建（应急 / 离线）

```bash
sudo bash /volume1/docker/claims-system/deploy.sh
```

流程：备份到 `backups/deploy-<时间戳>/` → `node tools/check_inline_js.js` 前端语法门禁
→ `docker compose build`（镜像内还有一层门禁）→ `up -d` → 冒烟测试
（首页 / healthz / 管理员登录 / page-version / me）。

任一步失败则中止，旧容器继续服务。**回滚**：把 `backups/deploy-<时间戳>/` 里的文件拷回原位，重跑 `deploy.sh`。

---

## 自动同步（跟随 GitHub main）

`deploy/autoupdate.sh` 每 30 分钟由 cron 触发：

1. 查 GitHub API 取 `main` 最新 commit SHA
2. 读运行中容器镜像的 `org.opencontainers.image.revision` 标签
3. 相同 → 退出；不同 → 确认该 SHA 的镜像已发布（`docker manifest inspect`）
4. 调 `deploy/update.sh` 部署 `sha-<SHA>` 不可变镜像

```bash
sudo bash deploy/autoupdate.sh            # 立即检查
sudo bash deploy/autoupdate.sh --dry-run  # 只报告
```

日志：`backups/autoupdate.log`

**设计要点**：NAS 上不跑 git、不构建，只拉已发布的不可变镜像；按 SHA 比对而不是按 `latest` 标签，
避免「代码推了但镜像还没构建完」导致的无谓重启。

**安全前提**：仓库是公开的，能往 `main` 推代码就等于能改生产。请保护账号与分支权限。

---

## 主要接口

| 分组 | 接口 |
|---|---|
| 鉴权 | `POST /api/login`、`GET /api/me`、`POST /api/auth/change-password` |
| 事件 | `GET/POST /api/events`、`GET/PUT/DELETE /api/events/{code}` |
| 统计 | `GET /api/stats`、`GET /api/todo`、`GET /api/duplicates` |
| 旺店通 | `GET /api/wdt/shipment`、`POST /api/events/{code}/sync-wdt`、`/sync-shipment`、`/check-stockin` |
| 证据 | `POST /api/events/{code}/evidence`、`GET /api/evidence/{id}/download` |
| 导入导出 | `POST /api/import/legacy`、`GET /api/export/xlsx`、`GET /api/report/xlsx` |

完整定义见运行中的 `/openapi.json`。

---

## 金额口径

| 字段 | 公式 | 含义 |
|---|---|---|
| 顾客已赔 `refund_amount` | — | 我们赔给买家的钱，即实际损失 |
| 仓库赔付 `claim_amount` | — | 向仓库/快递索赔的金额 |
| 实收 `received_amount` | — | 对方实际赔付到账 |
| 未收回 `gap_amount` | `索赔 − 实收` | 还没收回来的索赔款 |
| 净损失 `net_loss` | `客赔 − 实收` | 真实自担损失（客赔为 0 视为未回填，返回 0） |

两个口径基数不同：**未收回看索赔，净损失看客赔**。

「金额异常」判定（`/api/todo` 的 `金额异常` 分组）：
索赔 > 订单金额，或 客赔 > 订单金额，或 实收 > 索赔，均需人工核对。

---

## 已知限制

- NAS 未安装 git，所有 git 操作走 `scripts/git.sh`（`alpine/git` 容器）
- 前端为单文件单页，`index.html` 改动后必须过 `check_inline_js.js`，否则部署会中止
- 旺店通查询窗口上限 30 天（超过报 `code=2180` 且静默返回空）
