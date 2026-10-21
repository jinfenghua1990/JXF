"""业务引擎：编号、撞单、统计、责任变更。

金额口径（唯一出口，前端不做二次计算）：
  gap_amount  = 申请 − 实收
  net_loss    = 顾客已赔 − 实收
"""
from __future__ import annotations

import enum
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    CHANNEL_LABEL,
    ClaimEvent,
    ClaimStatus,
    DutyChange,
    DutyParty,
    ReplySource,
    AuditLog,
    TERMINAL_STATUSES,
)

Z = Decimal("0")

# 在途超过该天数视为「超期」，需催办（已提交/回复中状态才计入在途）。
OVERDUE_DAYS = 15


def next_code(db: Session, event_date: date) -> str:
    prefix = f"CLM-{event_date.strftime('%y%m%d')}"
    existing = set(
        db.scalars(select(ClaimEvent.code).where(ClaimEvent.code.like(f"{prefix}-%"))).all()
    )
    n = 1
    while f"{prefix}-{n:03d}" in existing:
        n += 1
    return f"{prefix}-{n:03d}"


def merged_event_ids():
    """Merged source records are retained, but excluded from live totals and exports."""
    return select(AuditLog.event_id).where(AuditLog.action == "duplicate_merge",
        AuditLog.field == "合并归档", AuditLog.event_id.is_not(None))


def find_duplicates(db: Session, *, include_reviewed: bool = False) -> list[dict[str, Any]]:
    """同订单号/同发货单号多次登记 → 疑似重复申报。"""
    out: list[dict[str, Any]] = []
    reviewed = set(db.scalars(select(AuditLog.old_value).where(
        AuditLog.action == "duplicate_review", AuditLog.field == "确认独立申报"
    )).all()) if not include_reviewed else set()
    for col, label in (
        (ClaimEvent.wdt_tid, "订单号"),
        (ClaimEvent.ship_logistics_no, "发货单号"),
    ):
        normalized = func.trim(col)
        vals = db.execute(
            select(normalized).where(normalized != "", ~ClaimEvent.id.in_(merged_event_ids()))
            .group_by(normalized).having(func.count() > 1)
        ).scalars().all()
        for v in vals:
            evs = db.scalars(select(ClaimEvent).where(normalized == v,
                ~ClaimEvent.id.in_(merged_event_ids())).order_by(ClaimEvent.id)).all()
            if len(evs) < 2:
                continue
            # A changed member, association, type or amount requires a fresh review.
            snapshot = [[e.id, e.code, str(e.event_date), e.wdt_tid, e.ship_logistics_no,
                         e.incident_type.value, e.customer_claim, str(e.claim_amount),
                         str(e.received_amount), str(e.refund_amount)] for e in evs]
            fingerprint = hashlib.sha256(json.dumps(
                [label, v, snapshot], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            if fingerprint in reviewed:
                continue
            out.append(
                {
                    "fingerprint": fingerprint,
                    "field": label,
                    "value": v,
                    "count": len(evs),
                    "codes": [e.code for e in evs],
                    "claimed_total": f"{sum((e.claim_amount for e in evs), Z):.2f}",
                    "received_total": f"{sum((e.received_amount for e in evs), Z):.2f}",
                    "records": [{"code": e.code, "event_date": str(e.event_date),
                                 "shop_name": e.shop_name, "wdt_tid": e.wdt_tid,
                                 "ship_logistics_no": e.ship_logistics_no,
                                 "incident_type": e.incident_type.value,
                                 "customer_claim": e.customer_claim,
                                 "claim_amount": f"{e.claim_amount:.2f}",
                                 "received_amount": f"{e.received_amount:.2f}"} for e in evs],
                }
            )
    return out


def change_duty(
    db: Session, ev: ClaimEvent, to_party: DutyParty, reason: str
) -> ClaimEvent:
    """改判责任并留痕。金额不动（申请金额是向仓库方要的总额，与责任方无关）。"""
    if to_party == ev.duty_party:
        return ev
    if not reason.strip():
        raise ValueError("改判责任必须填写依据")
    db.add(
        DutyChange(
            event_id=ev.id, from_party=ev.duty_party, to_party=to_party, reason=reason
        )
    )
    ev.duty_party = to_party
    ev.duty_basis = reason
    return ev


# ---------------------------------------------------------------- 审计

# 需要留痕的字段（属性名, 中文标签）。金额/状态/责任方等核心字段改动必记录。
AUDIT_FIELDS: list[tuple[str, str]] = [
    ("event_date", "登记日期（售后时间）"), ("event_time", "登记时间（精确到分钟）"), ("shop_name", "店铺"), ("wdt_tid", "订单号"),
    ("ship_carrier", "快递公司"), ("ship_logistics_no", "物流单号"),
    ("incident_type", "售后原因"), ("after_sales_type", "售后类型"), ("refund_amount", "顾客已赔"),
    ("claim_amount", "仓库赔付金额"), ("received_amount", "实收金额"),
    ("duty_party", "责任方"), ("duty_party_custom", "自定义责任方"),
    ("status", "状态"), ("warehouse_ref", "仓库工单号"),
    ("customer_claim", "售后内容"), ("duty_basis", "判定依据"), ("closed", "结案"),
]


# 枚举字段的中文标签：审计留痕直接写中文，运营看账时不必再翻译。
ENUM_LABELS: dict[str, dict[str, str]] = {
    "status": {
        "draft": "已登记未提交", "submitted": "已提交仓库方",
        "replying": "仓库方回复处理中", "paid": "已赔付到账",
        "rejected": "仲裁驳回", "gave_up": "放弃追偿",
    },
    "duty_party": {
        "warehouse": "仓库方", "express": "快递方",
        "shared": "共担：快递+仓库", "juxiaofan": "聚小翻", "buyer": "买家",
        "custom": "人工输入", "undecided": "待定",
    },
    "incident_type": {
        "damage": "破损", "loss": "丢件", "shortage": "短少",
        "wrong": "错发", "undelivered": "未派送/派送错误", "other": "其他",
    },
    "after_sales_type": {
        "original_return": "原单退回", "return_only": "仅退回", "return_refund": "退回退款",
    },
}


def _val(v, attr: str = "") -> str:
    """把字段值转成可读文本；枚举取 .value 再翻成中文标签。

    注意：直接 str(枚举) 会得到 'ClaimStatus.DRAFT' 这类无意义文本，
    审计日志必须是人能看懂的，所以这里统一走 .value + 标签映射。
    """
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return f"{v:.2f}"
    if isinstance(v, enum.Enum):
        raw = str(v.value)
        return ENUM_LABELS.get(attr, {}).get(raw, raw)
    if isinstance(v, bool):
        return "是" if v else "否"
    return str(v)


def audit_diff(db: Session, ev: ClaimEvent, actor: str, before: dict[str, Any]) -> None:
    """比较 before 快照与新值，逐字段写入审计。"""
    for attr, label in AUDIT_FIELDS:
        ov = _val(before.get(attr), attr)
        nv = _val(getattr(ev, attr), attr)
        if ov != nv:
            db.add(AuditLog(event_id=ev.id, event_code=ev.code, actor=actor,
                            action="update", field=label, old_value=ov, new_value=nv))


def audit_action(db: Session, ev: ClaimEvent, actor: str, action: str,
                 field: str = "-", old_value: str = "", new_value: str = "") -> None:
    db.add(AuditLog(event_id=ev.id, event_code=ev.code, actor=actor,
                    action=action, field=field, old_value=old_value, new_value=new_value))


def get_audit(db: Session, code: str, limit: int = 200) -> list[dict]:
    rows = (
        db.scalars(
            select(AuditLog)
            .where(AuditLog.event_code == code)
            .order_by(AuditLog.id.desc())
            .limit(limit)
        )
        .all()
    )
    return [r.as_dict() for r in rows]


def add_reply(db: Session, ev: ClaimEvent, *, source: ReplySource,
              content: str, created_by: str = "") -> ClaimEvent:
    """追加一轮回复，并把事件状态推进到 replying（若尚未终态）。"""
    last_round = max((r.round_no for r in ev.replies), default=0)
    from ..models import ClaimReply

    db.add(
        ClaimReply(
            event_id=ev.id, source=source,
            round_no=last_round + (1 if source == ReplySource.WAREHOUSE else 0) or 1,
            content=content, created_by=created_by,
        )
    )
    if ev.status not in TERMINAL_STATUSES and source == ReplySource.WAREHOUSE:
        ev.status = ClaimStatus.REPLYING
    return ev


def summarize(events: list[ClaimEvent]) -> dict[str, Any]:
    """全量统计。页面上所有数字出自这里，口径唯一。"""
    today = date.today()
    agg: dict[str, Any] = {
        "events": len(events),
        "customer_refund": Z, "claimed_total": Z, "received_total": Z,
        "gap_total": Z, "net_loss": Z, "in_flight": Z,
        "pending_duty": Z, "rejected_or_abandoned": Z,
        "confirmed_bad_debt": Z,      # 已确定坏账 = 驳回/放弃 的未收回
        "expected_final_loss": Z,     # 预计最终亏损 = 已赔 - 索赔
        "defective_stockins": 0,
        "aging": {"d7": 0, "d15": 0, "d30": 0, "d30p": 0},
        "overdue_count": 0,
        "overdue_list": [],
    }
    by_status: dict[str, int] = {}
    by_carrier: dict[str, dict[str, Any]] = {}
    by_shop: dict[str, dict[str, Any]] = {}
    by_type: dict[str, dict[str, Any]] = {}
    by_duty: dict[str, dict[str, Any]] = {}
    by_channel: dict[str, dict[str, Any]] = {}
    by_creator: dict[str, dict[str, Any]] = {}
    undet: list[dict[str, Any]] = []

    for ev in events:
        from .manual_import import metadata, missing_fields
        if missing_fields(ev):
            agg['manual_pending_count'] = agg.get('manual_pending_count', 0) + 1
        if metadata(ev).get('amount_missing'):
            agg['claim_amount_missing_count'] = agg.get('claim_amount_missing_count', 0) + 1
        agg["customer_refund"] += ev.refund_amount
        agg["claimed_total"] += ev.claim_amount
        agg["received_total"] += ev.received_amount
        agg["gap_total"] += ev.gap_amount
        # 净损失只累计「顾客已赔已回填」的事件（0 = 未回填，不可计算）
        if ev.refund_amount > 0:
            agg["net_loss"] += ev.net_loss
        if ev.status == ClaimStatus.SUBMITTED or ev.status == ClaimStatus.REPLYING:
            agg["in_flight"] += ev.gap_amount
            age = (today - ev.event_date).days
            if age <= 7:
                agg["aging"]["d7"] += 1
            elif age <= 15:
                agg["aging"]["d15"] += 1
            elif age <= 30:
                agg["aging"]["d30"] += 1
            else:
                agg["aging"]["d30p"] += 1
            if age >= OVERDUE_DAYS:
                agg["overdue_count"] += 1
                agg["overdue_list"].append({
                    "code": ev.code, "event_date": ev.event_date.isoformat(),
                    "shop_name": ev.shop_name, "duty_party": ev.duty_party.value,
                    "age": age, "gap": f"{ev.gap_amount:.2f}",
                    "status": ev.status.value,
                })
        if ev.status in (ClaimStatus.REJECTED, ClaimStatus.GAVE_UP):
            agg["rejected_or_abandoned"] += ev.gap_amount
            agg["confirmed_bad_debt"] += ev.gap_amount
        if ev.duty_party == DutyParty.UNDECIDED:
            agg["pending_duty"] += ev.gap_amount
            undet.append(
                {"code": ev.code, "event_date": ev.event_date.isoformat(),
                 "shop_name": ev.shop_name, "wdt_tid": ev.wdt_tid,
                 "pending": f"{ev.gap_amount:.2f}"}
            )
        if ev.stockin_is_defective:
            agg["defective_stockins"] += 1

        by_status[ev.status.value] = by_status.get(ev.status.value, 0) + 1

        def _b(d: dict, key: str) -> None:
            b = d.setdefault(key or "未填", {"n": 0, "claimed": Z, "received": Z, "loss": Z})
            b["n"] += 1
            b["claimed"] += ev.claim_amount
            b["received"] += ev.received_amount
            b["loss"] += ev.gap_amount

        _b(by_carrier, ev.ship_carrier)
        _b(by_shop, ev.shop_name)
        _b(by_type, ev.incident_type.value)
        _b(by_duty, ev.duty_party.value)
        # 账号维度：按「登记渠道」和「登记人」拆分，历史数据（空）单列。
        _b(by_channel, CHANNEL_LABEL.get(ev.created_channel or "", "") or "未标注渠道")
        _b(by_creator, ev.created_by or "历史导入")

    def _fmt(d: dict[str, Any]) -> None:
        for row in d.values():
            for k in ("claimed", "received", "loss"):
                row[k] = f"{row[k]:.2f}"

    _fmt(by_carrier)
    _fmt(by_shop)
    _fmt(by_type)
    _fmt(by_duty)
    _fmt(by_channel)
    _fmt(by_creator)
    undet.sort(key=lambda x: -Decimal(x["pending"]))
    agg["overdue_list"].sort(key=lambda x: -x["age"])

    agg["by_status"] = by_status
    agg["by_carrier"] = by_carrier
    agg["by_shop"] = by_shop
    agg["by_type"] = by_type
    agg["by_duty"] = by_duty
    agg["by_channel"] = by_channel
    agg["by_creator"] = by_creator
    agg["duty_undetermined"] = undet
    agg["duplicates"] = []
    agg["expected_final_loss"] = agg["customer_refund"] - agg["claimed_total"]
    agg["recover_rate"] = (
        f"{(agg['received_total'] / agg['claimed_total'] * 100):.2f}"
        if agg["claimed_total"] > Z else "0.00"
    )
    for k in (
        "customer_refund", "claimed_total", "received_total", "gap_total",
        "net_loss", "in_flight", "pending_duty", "rejected_or_abandoned",
        "confirmed_bad_debt", "expected_final_loss",
    ):
        agg[k] = f"{agg[k]:.2f}"
    return agg


def detect_original_return(ev: ClaimEvent) -> dict[str, Any]:
    """根据旺店通退款/退货物流/入库快照判断原单退回状态。

    只在有可追溯证据时显示，避免把普通仅退款误判成原单退回。
    """
    confirmed = bool(ev.after_sales_type and ev.after_sales_type.value == "original_return")
    prefix = "原单退回" if confirmed else "退货"
    if ev.stockin_found:
        return {"state": "received", "label": prefix + "·已入库",
                "evidence": "旺店通退货入库记录；入库本身不代表原单退回", "suggested_type": "original_return" if confirmed else ""}
    if (ev.return_logistics_no or "").strip() or ev.return_goods_count > 0:
        suggested = "return_refund" if ev.refund_amount > 0 or ev.refund_records else "return_only"
        return {"state": "shipping", "label": prefix + "中·待入库",
                "evidence": "退款单含退货物流或退回数量", "suggested_type": suggested}
    keywords = ("退货", "退回", "换货", "拒收")
    refund_text = " ".join(
        [ev.refund_reason or ""]
        + [f"{r.refund_reason} {r.refund_status}" for r in ev.refund_records]
    )
    if ev.refund_records and any(k in refund_text for k in keywords):
        return {"state": "suspected", "label": "疑似退货·待核实",
                "evidence": "退款原因/状态含退货关键词", "suggested_type": "return_refund"}
    if confirmed:
        return {"state": "suspected", "label": "原单退回·待核验", "evidence": "人工登记为原单退回，尚无退货或入库快照", "suggested_type": "original_return"}
    return {"state": "none", "label": "", "evidence": "", "suggested_type": ""}


def serialize_event(ev: ClaimEvent) -> dict[str, Any]:
    from .manual_import import metadata, missing_fields
    manual = metadata(ev)
    event_stamp = ev.event_time.isoformat(timespec="minutes") if ev.event_time else ev.event_date.isoformat()
    try:
        shipment = json.loads(ev.shipment_snapshot or "{}")
    except (ValueError, TypeError):
        shipment = {}
    return {
        "id": ev.id,
        "code": ev.code,
        "event_date": event_stamp,
        "wdt_tid": ev.wdt_tid,
        "ship_carrier": ev.ship_carrier,
        "ship_logistics_no": ev.ship_logistics_no,
        "shop_name": ev.shop_name,
        "platform": ev.platform,
        "order_amount": f"{ev.order_amount:.2f}",
        "manual_import": manual,
        "missing_fields": missing_fields(ev),
        "claim_amount_known": not manual.get('amount_missing', False),
        "order_amount_known": bool(shipment.get('source_tid')) or bool(ev.order_amount) or manual.get('order_amount_known', False),
        "goods_items": shipment.get("goods_items", []),
        "warehouse_name": shipment.get("warehouse_name", ""),
        "refund_amount": f"{ev.refund_amount:.2f}",
        "refund_reason": ev.refund_reason,
        "return_carrier": ev.return_carrier,
        "return_logistics_no": ev.return_logistics_no,
        "return_goods_count": f"{ev.return_goods_count}",
        "return_detection": detect_original_return(ev),
        "wdt_refund_no": ev.wdt_refund_no,
        "wdt_synced_at": ev.wdt_synced_at.isoformat() if ev.wdt_synced_at else "",
        "incident_type": ev.incident_type.value,
        "after_sales_type": ev.after_sales_type.value if ev.after_sales_type else "",
        "customer_claim": ev.customer_claim,
        "duty_party": ev.duty_party.value,
        "duty_party_custom": ev.duty_party_custom,
        "duty_basis": ev.duty_basis,
        "claim_amount": f"{ev.claim_amount:.2f}",
        "received_amount": f"{ev.received_amount:.2f}",
        "gap_amount": f"{ev.gap_amount:.2f}",
        "net_loss": f"{ev.net_loss:.2f}",
        "status": ev.status.value,
        "warehouse_ref": ev.warehouse_ref,
        "stockin": {
            "checked_at": ev.stockin_checked_at.isoformat() if ev.stockin_checked_at else "",
            "found": ev.stockin_found,
            "status": ev.stockin_status,
            "warehouse": ev.stockin_warehouse,
            "is_defective": ev.stockin_is_defective,
            "time": ev.stockin_time.isoformat() if ev.stockin_time else "",
        },
        "closed": ev.closed,
        "note": ev.note,
        "created_by": ev.created_by or "",
        "created_channel": ev.created_channel or "",
        "created_at": ev.created_at.isoformat() if ev.created_at else "",
        "updated_at": ev.updated_at.isoformat() if ev.updated_at else "",
        "replies": [
            {
                "id": r.id, "source": r.source.value, "round_no": r.round_no,
                "content": r.content, "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in ev.replies
        ],
        "refund_records": [
            {
                "id": r.id, "refund_no": r.refund_no,
                "refund_amount": f"{r.refund_amount:.2f}",
                "refund_reason": r.refund_reason, "refund_status": r.refund_status,
                "return_carrier": r.return_carrier, "return_logistics_no": r.return_logistics_no,
                "return_goods_count": f"{r.return_goods_count}", "refund_time": r.refund_time,
            }
            for r in ev.refund_records
        ],
        "refund_count": len(ev.refund_records),
        "refund_items": [
            {
                "spec_no": i.spec_no, "barcode": i.barcode, "goods_name": i.goods_name,
                "spec_name": i.spec_name, "order_num": f"{i.order_num}",
                "refund_num": f"{i.refund_num}", "stockin_num": f"{i.stockin_num}",
                "cost_price": f"{i.cost_price:.2f}",
            }
            for i in ev.refund_items
        ],
        "stockin_items": [
            {
                "spec_no": i.spec_no, "goods_count": f"{i.goods_count}",
                "cost_price": f"{i.cost_price:.2f}", "production_date": i.production_date,
                "validity_days": f"{i.validity_days}", "expire_date": i.expire_date,
            }
            for i in ev.stockin_items
        ],
        "duty_changes": [
            {
                "id": c.id, "from_party": c.from_party.value, "to_party": c.to_party.value,
                "reason": c.reason,
                "created_at": c.created_at.isoformat() if c.created_at else "",
            }
            for c in ev.duty_changes
        ],
        "evidence": [
            {"id": e.id, "kind": e.kind, "filename": e.filename,
             "source_party": e.source_party or "unclassified", "content_type": e.content_type,
             "caption": e.caption, "reply_id": e.reply_id}
            for e in ev.evidence
        ],
    }


def parse_datetime(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(v)[:19], fmt)
        except ValueError:
            continue
    return None
