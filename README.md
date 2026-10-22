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
deploy/autoupdate.sh  自动同步 GitHub main（拉源码 → 构建 → 失败回滚）
deploy/update.sh    可选：拉取 GHCR 已构建镜像部署（见「镜像模式」）
deploy/override-image.yml  镜像模式的 compose 叠加层
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

### 生产：NAS 本地构建（当前使用）

```bash
sudo bash /volume1/docker/claims-system/deploy.sh
```

流程：备份到 `backups/deploy-<时间戳>/` → `node tools/check_inline_js.js` 前端语法门禁
→ `docker compose build`（镜像内还有一层门禁）→ `up -d` → 冒烟
（healthz / 首页 / 管理员登录 / page-version / me / 待办接口）。

任一步失败则中止，旧容器继续服务。**回滚**：把 `backups/deploy-<时间戳>/` 里的文件拷回原位，重跑 `deploy.sh`。

### 镜像模式（可选，当前网络不可用）

代码推送到 `main` → GitHub Actions 构建并推 GHCR（`sha-<SHA>` 与 `latest`）→ NAS 只拉镜像、不构建。

```bash
sudo bash deploy/update.sh ghcr.io/jinfenghua1990/jxf:sha-<SHA>
```

**为什么当前没启用**：实测 NAS 到 `ghcr.io` 的 Blob 下载只有 **23 KB/s**——registry 的 API 本身很快
（`/v2/` 与 token 都在 0.6 秒内返回），慢的是层数据的 CDN，66 MB 的镜像要拉一小时以上且中途会断。
试过 `ghcr.nju.edu.cn`（清单 2 秒能取到，层数据同样卡死）、`ghcr.m.daocloud.io`（`/v2/` 返回 401，未继续验证）。
换到能正常访问 ghcr 的网络时，这套脚本可以直接用。

`deploy/update.sh` 的流程：拉镜像（失败重试，不动现有容器）→ 备份数据库（`pg_dump`）与 `data/`
→ 切换 `APP_IMAGE` → 重建 app → 等健康检查 → **失败自动切回上一个镜像**。
镜像模式通过 `deploy/override-image.yml` 叠加到 `docker-compose.yml`，只覆盖 `app.image`，
数据库/备份服务配置完全共用，不维护第二份 compose。

---

## 自动同步（跟随 GitHub main）

`deploy/autoupdate.sh` 由 cron 每 30 分钟触发：

1. `git fetch origin main`（借 `alpine/git` 容器，NAS 本身没有 git）
2. 本地 `HEAD` 与 `origin/main` 相同 → 退出，什么都不做
3. 不同 → 备份 `api.py` 与静态页到 `backups/autoupdate-<时间戳>/`，`reset --hard` 到新版
4. 跑 `deploy.sh`（门禁 → 构建 → 重启 → 冒烟）
5. 失败 → 回到旧 commit 重新构建，**不留半死状态**

```bash
sudo bash deploy/autoupdate.sh            # 立即检查
sudo bash deploy/autoupdate.sh --dry-run  # 只报告有哪些新提交
```

crontab（DSM 写在 `/etc/crontab`）：

```
*/30 * * * * root bash /volume1/docker/claims-system/deploy/autoupdate.sh >/dev/null 2>&1
```

日志：`backups/autoupdate.log`（超过 2 MB 自动只保留最后 2000 行）

**设计要点**：按 commit 比对而不是按 `latest` 标签，避免无谓重启；`flock` 防重入，
上一轮构建没跑完不会叠加；`git reset --hard` 不碰 `.gitignore` 里的 `.env` / `data/` / `backups/`，
业务数据与密钥不受影响。NAS 到 GitHub 的 git 协议实测 2.5 秒完成 `ls-remote`，
所以这条路比拉镜像稳得多。

**安全前提**：仓库是公开的，能往 `main` 推代码就等于能改生产。请保护账号与分支权限。

---

## 日常运维

```bash
cd /volume1/docker/claims-system

sudo bash deploy/autoupdate.sh --dry-run   # 现在跑的是哪个版本、有没有新提交
sudo bash deploy.sh                        # 手动部署一次
tail -20 backups/autoupdate.log            # 最近几轮自动更新记录
sudo docker compose logs -f --tail=100 app # 应用日志
sudo docker compose ps                     # 容器状态
```

`backups/` 下保留最近 20 份 `autoupdate-*` 与 `deploy-*` 备份，更早的由自动更新脚本清理。

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
- NAS 上 `docker` 和 `node` 都在 `/usr/local/bin`，不在 admin 用户的默认 PATH 里；
  `deploy.sh` / `deploy/autoupdate.sh` 开头都会显式 `export PATH`，直接跑不会被「command not found」坑
- 前端为单文件单页，`index.html` 改动后必须过 `check_inline_js.js`，否则部署会中止
- 旺店通查询窗口上限 30 天（超过报 `code=2180` 且静默返回空）
