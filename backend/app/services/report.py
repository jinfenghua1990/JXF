"""Excel/CSV 导出：单表明细 + 多 sheet 报表 + 回填导入解析。

导出：
  - 导出明细（Excel 单表）：字段与 CSV 一致，单号列按文本写入，
    解决「Excel 打开后长订单号变 1.10211E+17、末几位丢失」的问题；
  - 报表（多 sheet）：汇总 / 明细 / 按责任方 / 逾期在途；
  - CSV：给程序对接用。注意 Excel 直接打开 CSV 时，超 15 位的纯数字单号
    一定会被转成科学计数法并丢精度——这是 Excel 的行为，CSV 无类型信息可挡，
    要给人看请走「导出明细」。
导入（回填闭环）：
  - 读取「明细」sheet，按编号更新 实收金额 / 状态 / 仓库工单号；
  - 仅更新被改动过的行，未改的行原样回写不产生审计噪声。
"""
from __future__ import annotations

import io
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .engine import OVERDUE_DAYS, ENUM_LABELS, summarize
from .manual_import import metadata
from ..models import CHANNEL_LABEL

HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
HEAD_FONT = Font(bold=True, color="FFFFFF", size=11)
TITLE_FONT = Font(bold=True, size=14, color="1F3A5F")
SUB_FONT = Font(italic=True, size=10, color="666666")

_DUTY_LB = ENUM_LABELS["duty_party"]
# 状态中文口径与界面 /api/meta 保持一致，避免同一状态在界面和导出里叫法不同。
_STATUS_LB = ENUM_LABELS["status"]
# 回填导入时同时兼容「2.0 早期报表写出的旧状态名」与当前口径，否则旧报表导入会整批失配。
_LEGACY_STATUS_LB = {
    "draft": "待更新数据", "submitted": "已提交仓库", "replying": "仓库回复中",
    "paid": "已赔付", "rejected": "已驳回", "gave_up": "已放弃",
}
_STATUS_ALIAS = {v: k for k, v in _LEGACY_STATUS_LB.items()}
_STATUS_ALIAS.update({v: k for k, v in _STATUS_LB.items()})

# ---------- 列定义 ----------
# 取值只在 _values() 里维护一份；列顺序按用途各排一份，避免「改了值忘了列」。
# CSV / 导出明细：与历史 CSV 同列同序，不打乱既有习惯与外部脚本。
EXPORT_HEADERS = [
    "原始订单号", "登记日期（售后时间）", "店铺名称", "订单金额", "发货快递公司", "发货物流单号",
    "退货物流", "退货单号", "顾客已赔", "退款原因", "售后原因", "售后类型", "售后内容",
    "责任方", "判定依据", "仓库赔付金额", "实收金额", "未收回", "净损失", "状态",
    "入库仓库", "次品仓", "入库时间", "结案", "备注", "登记人", "登记渠道",
]
# 报表「明细」：在上面基础上补齐历史缺失字段（平台 / 退货件数 / 仓库工单号 / 在途天数 / 入库）。
DETAIL_HEADERS = [
    "原始订单号", "登记日期（售后时间）", "店铺名称", "平台", "订单金额",
    "发货快递公司", "发货物流单号", "退货物流", "退货单号", "退货件数",
    "顾客已赔", "退款原因", "售后原因", "售后类型", "售后内容",
    "责任方", "判定依据", "仓库赔付金额", "实收金额", "未收回", "净损失",
    "状态", "仓库工单号", "在途天数", "入库", "入库仓库", "次品仓", "入库时间", "结案", "备注",
    "登记人", "登记渠道",
]
# 必须按文本写入的列：纯数字单号超过 15 位时 Excel 会转科学计数法并丢末几位。
_TEXT_COLUMNS = {"原始订单号", "发货物流单号", "退货单号", "仓库工单号"}
_MONEY_COLUMNS = {"订单金额", "顾客已赔", "仓库赔付金额", "实收金额", "未收回", "净损失"}

EXPORT_WIDTHS = [16, 18, 24, 9, 16, 18, 14, 18, 9, 14, 12, 12, 40, 10, 24, 10, 10, 10, 10, 12, 16, 8, 16, 8, 24, 16, 12]
DETAIL_WIDTHS = [16, 18, 24, 8, 9, 16, 18, 14, 18, 9, 9, 14, 12, 12, 40, 10, 24, 10, 10, 10, 10, 12, 16, 9, 9, 16, 8, 16, 8, 24, 16, 12]


def _m(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _style_header(ws, row: int = 1) -> None:
    for c in ws[row]:
        c.fill = HEAD_FILL
        c.font = HEAD_FONT
        c.alignment = Alignment(horizontal="center", vertical="center")


def _values(ev, today: date) -> dict[str, Any]:
    """明细每一列的取值（单一事实来源）。列顺序由 EXPORT_HEADERS / DETAIL_HEADERS 决定。"""
    meta = metadata(ev)
    amount_missing = bool(meta.get("amount_missing"))
    try:
        shipment = json.loads(ev.shipment_snapshot or "{}")
    except (ValueError, TypeError):
        shipment = {}
    order_amount_known = (
        bool(shipment.get("source_tid")) or bool(ev.order_amount) or bool(meta.get("order_amount_known"))
    )

    stockin = "未查"
    if ev.stockin_found:
        stockin = "次品仓" if ev.stockin_is_defective else "已入库"
    age: Any = ""
    if ev.status.value in ("submitted", "replying"):
        age = (today - ev.event_date).days
    rule = ev.incident_type.value
    return {
        "原始订单号": ev.wdt_tid or "",
        "登记日期（售后时间）": (
            ev.event_time.strftime("%Y-%m-%d %H:%M") if ev.event_time else ev.event_date.isoformat()
        ),
        "店铺名称": ev.shop_name,
        "平台": ev.platform,
        "订单金额": _m(ev.order_amount) if order_amount_known else "待补",
        "发货快递公司": ev.ship_carrier,
        "发货物流单号": ev.ship_logistics_no,
        "退货物流": ev.return_carrier,
        "退货单号": ev.return_logistics_no,
        "退货件数": _m(ev.return_goods_count),
        "顾客已赔": _m(ev.refund_amount),
        "退款原因": ev.refund_reason,
        "售后原因": ENUM_LABELS["incident_type"].get(rule, rule),
        "售后类型": (
            ENUM_LABELS["after_sales_type"].get(ev.after_sales_type.value, "") if ev.after_sales_type else ""
        ),
        "售后内容": ev.customer_claim,
        "责任方": (
            ev.duty_party_custom
            if ev.duty_party.value == "custom" and ev.duty_party_custom
            else _DUTY_LB.get(ev.duty_party.value, ev.duty_party.value)
        ),
        "判定依据": ev.duty_basis,
        "仓库赔付金额": "待补" if amount_missing else _m(ev.claim_amount),
        "实收金额": _m(ev.received_amount),
        "未收回": "待补" if amount_missing else _m(ev.gap_amount),
        "净损失": _m(ev.net_loss),
        "状态": _STATUS_LB.get(ev.status.value, ev.status.value),
        "仓库工单号": ev.warehouse_ref or "",
        "在途天数": age,
        "入库": stockin,
        "入库仓库": ev.stockin_warehouse,
        "次品仓": "是" if ev.stockin_is_defective else "否",
        "入库时间": ev.stockin_time.strftime("%Y-%m-%d %H:%M") if ev.stockin_time else "",
        "结案": "是" if ev.closed else "否",
        "备注": ev.note,
        "登记人": ev.created_by or "历史导入",
        "登记渠道": CHANNEL_LABEL.get(ev.created_channel or "", "") or "未标注渠道",
    }


def _rows(evs: list, headers: list[str]) -> list[list[Any]]:
    today = date.today()
    return [[v[k] for k in headers] for v in (_values(e, today) for e in evs)]


def export_rows(evs: list) -> list[list[Any]]:
    """CSV / 导出明细的取值（按 EXPORT_HEADERS 顺序）。"""
    return _rows(evs, EXPORT_HEADERS)


def detail_rows(evs: list) -> list[list[Any]]:
    """报表「明细」sheet 的取值（按 DETAIL_HEADERS 顺序）。"""
    return _rows(evs, DETAIL_HEADERS)


def _apply_sheet_layout(ws, headers: list[str], widths: list[int]) -> None:
    """列宽 + 文本/金额格式 + 冻结首行 + 自动筛选。

    文本格式是长单号不变科学计数法的关键：单元格格式设成 @ 之后，
    即便有人在 Excel 里编辑该列，也不会被重新识别成数字。
    """
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    text_idx = [i for i, h in enumerate(headers, 1) if h in _TEXT_COLUMNS]
    money_idx = [i for i, h in enumerate(headers, 1) if h in _MONEY_COLUMNS]
    for r in range(2, ws.max_row + 1):
        for c in text_idx:
            ws.cell(r, c).number_format = "@"
        for c in money_idx:
            if isinstance(ws.cell(r, c).value, (int, float)):
                ws.cell(r, c).number_format = "#,##0.00"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(ws.max_row, 1)}"


def build_export_workbook(evs: list) -> bytes:
    """「导出明细」：单 sheet Excel，字段与 CSV 一致，但单号列写为文本。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "售后登记表"
    ws.append(EXPORT_HEADERS)
    _style_header(ws, 1)
    for row in export_rows(evs):
        ws.append(row)
    _apply_sheet_layout(ws, EXPORT_HEADERS, EXPORT_WIDTHS)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_report_workbook(evs: list, summary: dict[str, Any]) -> bytes:
    wb = Workbook()

    # ---------- 汇总 ----------
    ws = wb.active
    ws.title = "汇总"
    ws["A1"] = "售后登记表"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"生成时间 {datetime.now():%Y-%m-%d %H:%M}　统计区间事件数 {summary['events']}；仓库赔付金额待补 {summary.get('claim_amount_missing_count', 0)} 条，汇总仅含已知金额"
    ws["A2"].font = SUB_FONT
    rows = [
        ("顾客已赔合计", summary["customer_refund"]),
        ("仓库赔付金额合计", summary["claimed_total"]),
        ("实收金额合计", summary["received_total"]),
        ("追回率", f"{summary['recover_rate']}%"),
        ("未收回合计", summary["gap_total"]),
        ("净损失合计", summary["net_loss"]),
        ("责任待定悬置", summary["pending_duty"]),
        ("在途(已提交/回复中)", summary["in_flight"]),
        ("超期未结(≥%d天)" % OVERDUE_DAYS, summary["overdue_count"]),
        ("次品入库线索", summary["defective_stockins"]),
    ]
    r = 4
    ws.cell(r, 1, "指标").font = Font(bold=True)
    ws.cell(r, 2, "数值").font = Font(bold=True)
    _style_header(ws, r)
    for k, v in rows:
        r += 1
        ws.cell(r, 1, k)
        ws.cell(r, 2, v)
    r += 2
    ws.cell(r, 1, "状态分布").font = Font(bold=True)
    _style_header(ws, r)
    for st, n in (summary.get("by_status") or {}).items():
        r += 1
        ws.cell(r, 1, _STATUS_LB.get(st, st))
        ws.cell(r, 2, n)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 16

    # ---------- 明细 ----------
    wd = wb.create_sheet("明细")
    wd.append(DETAIL_HEADERS)
    _style_header(wd, 1)
    for row in detail_rows(evs):
        wd.append(row)
    _apply_sheet_layout(wd, DETAIL_HEADERS, DETAIL_WIDTHS)

    # ---------- 按责任方 ----------
    wz = wb.create_sheet("按责任方")
    wz.append(["责任方", "单数", "仓库赔付金额", "实收金额", "未收回"])
    _style_header(wz, 1)
    for k, x in (summary.get("by_duty") or {}).items():
        wz.append([_DUTY_LB.get(k, k), x["n"], _m(x["claimed"]), _m(x["received"]), _m(x["loss"])])
    for i, w in enumerate([12, 8, 14, 14, 14], 1):
        wz.column_dimensions[get_column_letter(i)].width = w

    # ---------- 按渠道 ----------
    wc = wb.create_sheet("按渠道")
    wc.append(["登记渠道", "单数", "仓库赔付金额", "实收金额", "未收回"])
    _style_header(wc, 1)
    for k, x in (summary.get("by_channel") or {}).items():
        wc.append([k, x["n"], _m(x["claimed"]), _m(x["received"]), _m(x["loss"])])
    for i, w in enumerate([14, 8, 14, 14, 14], 1):
        wc.column_dimensions[get_column_letter(i)].width = w

    # ---------- 按登记人 ----------
    wo = wb.create_sheet("按登记人")
    wo.append(["登记人", "单数", "仓库赔付金额", "实收金额", "未收回"])
    _style_header(wo, 1)
    for k, x in (summary.get("by_creator") or {}).items():
        wo.append([k, x["n"], _m(x["claimed"]), _m(x["received"]), _m(x["loss"])])
    for i, w in enumerate([20, 8, 14, 14, 14], 1):
        wo.column_dimensions[get_column_letter(i)].width = w

    # ---------- 逾期在途 ----------
    wv = wb.create_sheet("逾期在途")
    wv.append(["编号", "日期", "店铺", "责任方", "在途天数", "未收回", "状态"])
    _style_header(wv, 1)
    for o in summary.get("overdue_list") or []:
        wv.append([o["code"], o["event_date"], o["shop_name"],
                   _DUTY_LB.get(o["duty_party"], o["duty_party"]), o["age"],
                   _m(o["gap"]), _STATUS_LB.get(o["status"], o["status"])])
    for i, w in enumerate([16, 12, 20, 10, 10, 12, 12], 1):
        wv.column_dimensions[get_column_letter(i)].width = w
    wv.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_report_import(content: bytes) -> list[dict[str, Any]]:
    """读取「明细」sheet，提取需更新的字段（仅非空项）。返回按编号的更新字典列表。"""
    wb = load_workbook(io.BytesIO(content), data_only=True)
    if "明细" not in wb.sheetnames:
        raise ValueError("报表缺少「明细」工作表")
    ws = wb["明细"]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    if not rows:
        return []
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    idx = {name: i for i, name in enumerate(header)}
    identity = "原始订单号" if "原始订单号" in idx else "编号"
    missing = [h for h in (identity, "实收金额", "状态") if h not in idx]
    if missing:
        raise ValueError("明细表头缺少列：" + "、".join(missing))

    out: list[dict[str, Any]] = []
    for r in rows[1:]:
        if not r or len(r) <= idx[identity]:
            continue
        code = str(r[idx[identity]]).strip() if r[idx[identity]] is not None else ""
        if not code:
            continue
        upd: dict[str, Any] = {"code": code}
        if identity == "原始订单号":
            upd["wdt_tid"] = code
        if idx["实收金额"] < len(r) and r[idx["实收金额"]] not in (None, ""):
            upd["received_amount"] = str(r[idx["实收金额"]])
        if idx["状态"] < len(r) and r[idx["状态"]] not in (None, ""):
            raw = str(r[idx["状态"]]).strip()
            upd["status"] = _STATUS_ALIAS.get(raw, raw)
        if "仓库工单号" in idx and idx["仓库工单号"] < len(r) and r[idx["仓库工单号"]] not in (None, ""):
            upd["warehouse_ref"] = str(r[idx["仓库工单号"]]).strip()
        if any(field in upd for field in ("received_amount", "status", "warehouse_ref")):
            out.append(upd)
    return out
