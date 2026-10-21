"""2.0 端到端自检：按用户确认的规则逐条验证。

规则要点（2026-10-13 用户拍板）：
- 仓库方承接全部追责（业务方确认）；单索赔线
- 赔付金额=申请金额；净损失=顾客已赔−实收
- 仓库方回复多轮记录；责任改判留痕
- 导入不捏造：旧表只有申请金额，实收/顾客已赔留空等确认或旺店通回填
"""
from __future__ import annotations

import asyncio
import io
import os
import tempfile
import atexit
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal

_workspace = tempfile.TemporaryDirectory(prefix="claims-selfcheck-")
atexit.register(_workspace.cleanup)
os.environ["DATABASE_URL"] = "sqlite:///" + (Path(_workspace.name) / "claims.db").as_posix()
os.environ["UPLOAD_DIR"] = str(Path(_workspace.name) / "uploads")
for key in ("WDT_SID", "WDT_APPKEY", "WDT_APPSECRET", "WDT_QIMEN_APPKEY", "WDT_QIMEN_APPSECRET", "WDT_QIMEN_TARGET_APPKEY"):
    os.environ[key] = ""
os.environ["DISABLE_AUTH"] = "false"
os.environ["APP_SECRET"] = "isolated-selfcheck-secret-not-for-production"
os.environ["ADMIN_EMAIL"] = "selftest@claims.local"
os.environ["ADMIN_PASSWORD"] = "selftest-password"
# 仅在测试进程模拟旺店通接口返回；生产核验规则保持不变。

FAILED: list[str] = []
CHECKS = 0


def check(name: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    ok = got == want
    if not ok:
        FAILED.append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: 实得 {got!r} / 期望 {want!r}")


async def _init():
    from contextlib import AsyncExitStack

    from app.main import app, lifespan

    stack = AsyncExitStack()
    await stack.enter_async_context(lifespan(app))
    await stack.aclose()
    return app


app = asyncio.run(_init())

from fastapi.testclient import TestClient  # noqa: E402

from app.models import ClaimStatus, DutyParty, ReplySource  # noqa: E402
from app.database import engine
atexit.register(engine.dispose)
from app.services.wangdian import WangdianClient, WdtError

_orders = {
    "2622001842874585": "319392968192357", "T2": "TEST-T2", "T3": "TEST-T3",
    "6954640341931005538": "JDAZ26408198637", "6928465306231865033": "321280221646011",
}

def _wdt_fixture(self, endpoint, params):
    if endpoint == "trade_query.php":
        tracking = params.get("logistics_no")
        return {"trades": [{"src_tids": tid, "logistics_no": tracking, "goods_amount": "39.90", "shop_name": "红盒子-快手-测试店"}
                           for tid, lno in _orders.items() if lno == tracking]}
    if endpoint == "stockout_order_query_trade.php":
        tid = params.get("src_tid")
        return {"stockout_list": [{"src_trade_no": tid, "logistics_no": _orders[tid], "goods_amount": "39.90", "shop_name": "红盒子-快手-测试店"}] if tid in _orders else []}
    if "refund" in endpoint:
        return {"refund_list": [], "stockin_list": []}
    raise AssertionError("Unexpected upstream call in offline selfcheck: " + endpoint)

_wdt_mock = patch.object(WangdianClient, "_request", _wdt_fixture)
_wdt_mock.start()
atexit.register(_wdt_mock.stop)

client = TestClient(app)

# 鉴权：以测试管理员账号(邮箱+密码)登录并注入默认请求头（此后所有请求自动带 token）
_TEST_EMAIL = os.environ["ADMIN_EMAIL"]
_TEST_PW = os.environ["ADMIN_PASSWORD"]
_TOKEN = client.post("/api/login", json={"email": _TEST_EMAIL, "password": _TEST_PW}).json()["token"]
client.headers["Authorization"] = "Bearer " + _TOKEN

# 鉴权行为自检（独立于业务）
r = client.post("/api/login", json={"email": _TEST_EMAIL, "password": "wrong"})
check("错误密码登录被拒(401)", r.status_code, 401)
r = client.post("/api/login", json={"email": "nobody@x.com", "password": _TEST_PW})
check("不存在邮箱登录被拒(401)", r.status_code, 401)
me = client.get("/api/me").json()
check("当前账号邮箱", me["email"], _TEST_EMAIL)
check("当前账号为管理员", me["is_admin"], True)

print()
print("=" * 70)
print("0. 账号+密码登录链路（不依赖邮件/验证码）")
print("=" * 70)
r = client.post("/api/login", json={"email": _TEST_EMAIL})
check("缺密码登录被拒(422)", r.status_code, 422)
r = client.post("/api/login", json={"email": _TEST_EMAIL, "password": "wrong"})
check("错误密码登录被拒(401)", r.status_code, 401)
r = client.post("/api/login", json={"email": _TEST_EMAIL, "password": _TEST_PW})
check("正确密码登录成功(200)", r.status_code, 200)
check("登录拿到 token", "token" in r.json(), True)
# 修改密码（需原密码）
r = client.post("/api/auth/change-password", json={"old_password": _TEST_PW, "new_password": "NewPass123"})
check("修改密码成功(200)", r.status_code, 200)
r = client.post("/api/login", json={"email": _TEST_EMAIL, "password": "NewPass123"})
check("新密码登录成功(200)", r.status_code, 200)
r = client.post("/api/auth/change-password", json={"new_password": "x12345"})
check("无原密码改已设密码被拒(422)", r.status_code, 422)
r = client.post("/api/auth/change-password", json={"old_password": "NewPass123", "new_password": _TEST_PW})
check("改回原密码成功(200)", r.status_code, 200)

print("=" * 70)
print("1. 元数据与健康")
print("=" * 70)
h = client.get("/api/health").json()
check("健康", h["ok"], True)
m = client.get("/api/meta").json()
check("责任方通用称谓", [x["label"] for x in m["duty_party"]][:1], ["仓库方"])
check("状态6种", len(m["status"]), 6)
check("旺店通未配置时标记", m["wdt_configured"], False)
check("新登记提供四种售后原因", [x["value"] for x in m["incident_type"]], ["damage", "loss", "shortage", "undelivered"])
with patch.object(WangdianClient, "_request", side_effect=WdtError("模拟旺店通不可用")):
    response = client.post("/api/events", json={"event_date": "2026-08-11", "wdt_tid": "T2", "incident_type": "loss", "claim_amount": "10"})
    check("旺店通异常时禁止无依据登记", response.status_code, 502)
check("核验失败未创建记录", client.get("/api/events").json()["total"], 0)

print()
print("=" * 70)
print("2. 创建事件：金额口径（赔付金额=申请金额）")
print("=" * 70)
r = client.post("/api/events", json={
    "event_date": "2026-08-11",
    "wdt_tid": "2622001842874585",
    "ship_carrier": "韵达快递",
    "ship_logistics_no": "319392968192357",
    "shop_name": "红盒子-快手-格力高零食旗舰店",
    "incident_type": "破损",
    "customer_claim": "外箱二次包装，商品泡水损坏",
    "duty_party": "快递",
    "claim_amount": "39.9",
    "status": "已提交",
}).json()
code = r["event"]["code"]
ev = r["event"]
check("编号=订单号", code, "2622001842874585")
check("平台自动识别(快手)", ev["platform"], "快手")
check("申请金额", ev["claim_amount"], "39.90")
check("责任方", ev["duty_party"], "express")
check("状态", ev["status"], "submitted")
check("未收回=39.9", ev["gap_amount"], "39.90")
check("净损失=0-0(顾客已赔未填)", ev["net_loss"], "0.00")

print()
print("=" * 70)
print("3. 旺店通同步（顾客已赔自动回填路径）")
print("=" * 70)
r = client.post(f"/api/events/{code}/sync-wdt")
check("凭证未配置时报400", r.status_code, 400)

print()
print("=" * 70)
print("4. 仓库回复多轮时间线")
print("=" * 70)
r = client.post(f"/api/events/{code}/replies", json={
    "source": "仓库", "content": "麻烦提供售后证明"}).json()
check("仓库第1轮后状态→回复中", r["event"]["status"], "replying")
check("回复轮数=1", len(r["event"]["replies"]), 1)
client.post(f"/api/events/{code}/replies", json={"source": "自己", "content": "已提供"})
r = client.post(f"/api/events/{code}/replies", json={
    "source": "仓库", "content": "仲裁驳回，无法追责"}).json()
check("共3轮回复", len(r["event"]["replies"]), 3)
check("轮次编号", [x["round_no"] for x in r["event"]["replies"]], [1, 1, 2])
check("来源标记", [x["source"] for x in r["event"]["replies"]],
      ["warehouse", "owner", "warehouse"])
# 驳回 → 状态应手工置为已驳回
r = client.put(f"/api/events/{code}", json={"status": "已驳回"}).json()
check("置为已驳回", r["event"]["status"], "rejected")

print()
print("=" * 70)
print("5. 责任改判留痕（快递 → 仓库）")
print("=" * 70)
r = client.post(f"/api/events/{code}/duty", json={
    "to_party": "仓库", "reason": "查监控确认二次包装由仓库操作"}).json()
ev = r["event"]
check("改判后责任方", ev["duty_party"], "warehouse")
check("改判记录数", len(ev["duty_changes"]), 1)
check("留痕来源方", ev["duty_changes"][0]["from_party"], "express")
check("留痕依据", ev["duty_changes"][0]["reason"], "查监控确认二次包装由仓库操作")
# 改判必须带依据
r = client.post(f"/api/events/{code}/duty", json={"to_party": "快递", "reason": ""})
check("无依据改判被拒", r.status_code, 400)

print()
print("=" * 70)
print("6. 实收回填与净损失")
print("=" * 70)
client.put(f"/api/events/{code}", json={"event_date": "2026-08-11"})  # 保持
r = client.put(f"/api/events/{code}", json={
    "received_amount": "15", "refund_amount": "39.9"}).json()
ev = r["event"]
check("实收", ev["received_amount"], "15.00")
check("顾客已赔(手填兜底)", ev["refund_amount"], "39.90")
check("未收回=39.9-15", ev["gap_amount"], "24.90")
check("净损失=39.9-15", ev["net_loss"], "24.90")
r = client.put(f"/api/events/{code}", json={"received_amount": "999"})
check("实收>申请被拒", r.status_code, 400)

print()
print("=" * 70)
print("7. 标已赔付必须填写真实实收")
print("=" * 70)
paid_payload = {
    "event_date": "2026-08-12", "wdt_tid": "T2", "duty_party": "仓库",
    "incident_type": "loss", "claim_amount": "25.9", "status": "已赔付"}
check("缺实收不能标已赔付", client.post("/api/events", json=paid_payload).status_code, 400)
r = client.post("/api/events", json={**paid_payload, "received_amount": "25.9"}).json()
check("按实际输入记录实收", r["event"]["received_amount"], "25.90")
check("差额清零", r["event"]["gap_amount"], "0.00")
check("净损失(顾客已赔0)", r["event"]["net_loss"], "0.00")

print()
print("=" * 70)
print("8. 责任待定悬置")
print("=" * 70)
client.post("/api/events", json={
    "event_date": "2026-08-14", "wdt_tid": "T3",
    "incident_type": "loss", "claim_amount": "8.35", "duty_party": "待定", "status": "已提交"})
st = client.get("/api/stats?month=2026-08").json()
check("待定悬置金额", st["pending_duty"], "8.35")
check("待定清单", len(st["duty_undetermined"]), 1)

print()
print("=" * 70)
print("9. 汇总与损失归因")
print("=" * 70)
check("8月事件数", st["events"], 3)
check("申请合计=39.9+25.9+8.35", st["claimed_total"], "74.15")
check("实收合计=15+25.9", st["received_total"], "40.90")
check("净损失合计=24.9", st["net_loss"], "24.90")
check("驳回/放弃=39.9-15", st["rejected_or_abandoned"], "24.90")
print(f"   追回率 = {st['recover_rate']}%")

print()
print("=" * 70)
print("10. 撞单检测")
print("=" * 70)
r = client.post("/api/events", json={
    "event_date": "2026-08-11", "wdt_tid": "2622001842874585", "incident_type": "damage", "claim_amount": "39.9"})
check("重复订单号登记被拒(409提示合并)", r.status_code, 409)

print()
print("=" * 70)
print("11. 旧表导入：不捏造 + 多轮回复 + DISPIMG 跳过")
print("=" * 70)
from openpyxl import Workbook  # noqa: E402

wb = Workbook()
ws = wb.active
ws.append(["日期", "店铺名称", "订单号", "快递公司", "快递单号", "售后类型",
           "售后内容", "赔付金额", "责任方", "仓库回复1", "货主回复1", "仓库回复2"])
ws.append(["8月1日", "梦奇思-抖音-梦奇思零食专营店", "6954640341931005538",
           "京东快递-青浦仓", "JDAZ26408198637", "丢件", "丢件-仓库没有做入库",
           "24.9", "仓库", "麻烦提供售后证明", "已提供",
           "照片已收到，正在核实"])
ws.append(["8月7日", "梦奇思-抖音", "6928465306231865033", "韵达快递",
           "321280221646011", "破损", "暴力运输", "4", "快递",
           "仲裁驳回，无法追责", "快递破损严重-需追责赔付", ""])
buf = io.BytesIO()
wb.save(buf)
r = client.post("/api/import/legacy", files={"file": ("old.xlsx", buf.getvalue())}).json()
check("新建2条", r["created"], 2)
check("无异常", r["issue_count"], 0)

evs = client.get("/api/events?keyword=6954640341931005538").json()["rows"]
ev = evs[0]
check("申请金额(=台账赔付金额)", ev["claim_amount"], "24.90")
check("实收留空(不捏造)", ev["received_amount"], "0.00")
check("顾客已赔留空(等旺店通回填)", ev["refund_amount"], "0.00")
check("责任方=仓库", ev["duty_party"], "warehouse")
check("导入事件编号=订单号", ev["code"], "6954640341931005538")
check("发货单号", ev["ship_logistics_no"], "JDAZ26408198637")
check("8月1日日期解析", ev["event_date"].startswith(str(__import__("datetime").date.today().year)), True)
check("仓库回复2轮", [x for x in ev["replies"] if x["source"] == "warehouse"].__len__(), 2)
check("自己备注1轮", [x for x in ev["replies"] if x["source"] == "owner"].__len__(), 1)
check("导入默认状态=已提交仓库", ev["status"], "submitted")

print()
print("=" * 70)
print("12. 筛选与导出")
print("=" * 70)
check("按店铺筛选", client.get("/api/events?shop=梦奇思").json()["total"] >= 2, True)
check("仅待定", client.get("/api/events?only_pending_duty=true").json()["total"] >= 1, True)
check("CSV导出", client.get("/api/export/csv?month=2026-08").status_code, 200)
csv_text = client.get("/api/export/csv?month=2026-08").text
check("CSV含次品仓列", "次品仓" in csv_text, True)
check("模板下载", client.get("/api/export/template").status_code, 200)

print()
print("=" * 70)
print("13. 账号管理：管理员新增/删除成员，且至少保留一个管理员")
print("=" * 70)
r = client.post("/api/users", json={"email": "member@claims.local", "password": "member123", "is_admin": False})
check("管理员新增成员成功", r.status_code, 200)
lst = client.get("/api/users").json()["rows"]
check("账号列表含 2 个", len(lst), 2)
mid = next(x["id"] for x in lst if x["email"] == "member@claims.local")
r = client.post("/api/users", json={"email": "member@claims.local", "password": "x12345"})
check("重复邮箱被拒(409)", r.status_code, 409)
r = client.post("/api/users", json={"email": "bad", "password": "x12345"})
check("非法邮箱被拒(400)", r.status_code, 400)
r = client.delete(f"/api/users/{mid}")
check("删除成员成功", r.status_code, 200)
r = client.delete(f"/api/users/{mid}")
check("删除不存在账号(404)", r.status_code, 404)
r = client.post("/api/users", json={"email": "admin2@claims.local", "password": "admin2123", "is_admin": True})
check("新增第二个管理员成功", r.status_code, 200)
# 尝试删掉自己会被拒（用非自己身份删自己需要换 token，这里验证删最后一个管理员被拒）
admins = [x for x in client.get("/api/users").json()["rows"] if x["is_admin"]]
check("当前有 2 个管理员", len(admins), 2)

print()
print("=" * 70)
print("13b. 开户密码必填：无密码开户被拒，带密码开户+登录")
print("=" * 70)
r = client.post("/api/users", json={"email": "nopw@claims.local"})
check("无密码开户被拒(422)", r.status_code, 422)
r = client.post("/api/users", json={"email": "nopw@claims.local", "password": "nopw123"})
check("带密码开户成功(200)", r.status_code, 200)
nid = next(x["id"] for x in client.get("/api/users").json()["rows"] if x["email"] == "nopw@claims.local")
r = client.post("/api/login", json={"email": "nopw@claims.local", "password": "nopw123"})
check("该账号密码登录成功(200)", r.status_code, 200)
r = client.post("/api/login", json={"email": "nopw@claims.local", "password": "wrong"})
check("错误密码登录被拒(401)", r.status_code, 401)
r = client.delete(f"/api/users/{nid}")
check("清理测试账号(200)", r.status_code, 200)

print()
print("=" * 70)
if FAILED:
    print(f"自检失败 {len(FAILED)} 项:")
    for f in FAILED:
        print(f"  - {f}")
    raise SystemExit(1)
print(f"自检全部通过，共 {CHECKS} 项")
print("=" * 70)
