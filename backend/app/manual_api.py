import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, or_
from .api import require_auth, _get, _dec, _lookup_shipment, _apply_shipment, _validate_amounts
from .database import get_db
from .security import require_admin
from .models import ClaimEvent, platform_of
from .services.engine import serialize_event, audit_action, audit_diff, AUDIT_FIELDS, merged_event_ids
from .services.manual_import import metadata, missing_fields

router = APIRouter(prefix='/api/manual-import', dependencies=[Depends(require_admin)])


@router.get('/pending')
def pending(month: str = '', db=Depends(get_db)):
    rows = []
    for ev in db.scalars(select(ClaimEvent).where(~ClaimEvent.id.in_(merged_event_ids())).order_by(ClaimEvent.id)):
        if month and ev.event_date.strftime('%Y-%m') != month:
            continue
        if missing_fields(ev):
            rows.append(serialize_event(ev))
    return {'rows': rows, 'total': len(rows)}


@router.post('/{code}/complete')
def complete(code: str, payload: dict, actor=Depends(require_auth), db=Depends(get_db)):
    ev = _get(db, code)
    meta = metadata(ev)
    if not meta:
        raise HTTPException(409, '仅人工补录记录可使用此入口')
    if not str(payload.get('reason') or '').strip():
        raise HTTPException(400, '请填写补齐依据，便于追溯')
    before = {a: getattr(ev, a) for a, _ in AUDIT_FIELDS}
    # Association changes on already verified orders still require normal verification.
    tid = str(payload.get('wdt_tid', ev.wdt_tid) or '').strip()
    lno = str(payload.get('ship_logistics_no', ev.ship_logistics_no) or '').strip()
    if not lno:
        raise HTTPException(400, '发货物流单号不能为空')
    keys = [ClaimEvent.ship_logistics_no == lno]
    if tid:
        keys.append(ClaimEvent.wdt_tid == tid)
    if db.scalar(select(ClaimEvent.id).where(ClaimEvent.id != ev.id,
            ~ClaimEvent.id.in_(merged_event_ids()), or_(*keys)).limit(1)):
        raise HTTPException(409, '该订单或物流单号已登记，请先核对重复记录')
    shipment = None
    if payload.get('verify') or (meta.get('wdt_verified') and (tid != ev.wdt_tid or lno != ev.ship_logistics_no)):
        result = _lookup_shipment(lno, tid)
        if not result.get('ok'):
            raise HTTPException(409, result.get('message') or '旺店通暂未查到，请保留人工待核验状态')
        shipment = result['shipment']
        if tid and shipment.get('source_tid') != tid:
            raise HTTPException(409, '原始订单号与旺店通不一致')
        tid = shipment['source_tid']
        lno = shipment.get('logistics_no') or lno
        if db.scalar(select(ClaimEvent.id).where(ClaimEvent.id != ev.id,
                ~ClaimEvent.id.in_(merged_event_ids()), or_(ClaimEvent.wdt_tid == tid,
                ClaimEvent.ship_logistics_no == lno)).limit(1)):
            raise HTTPException(409, '旺店通匹配到的订单已登记，请核对重复')
    ev.wdt_tid, ev.ship_logistics_no = tid, lno
    for field, maximum in [('shop_name',128),('ship_carrier',64),('wdt_tid',64),('ship_logistics_no',64)]:
        value = str(payload.get(field, getattr(ev, field)) or '').strip()
        if shipment and field in ('wdt_tid','ship_logistics_no'):
            value = getattr(ev, field)
        if len(value) > maximum:
            raise HTTPException(400, '字段长度超过上限')
        setattr(ev, field, value)
    if payload.get('claim_amount') not in (None, ''):
        ev.claim_amount = _dec(payload['claim_amount'], '仓库赔付金额')
        meta['amount_missing'] = False
    if payload.get('order_amount') not in (None, ''):
        ev.order_amount = _dec(payload['order_amount'], '订单金额')
        meta['order_amount_known'] = True
    if shipment:
        _apply_shipment(ev, shipment)
        meta['wdt_verified'] = True
    ev.platform = platform_of(ev.shop_name)
    snap = json.loads(ev.shipment_snapshot or '{}')
    snap['_manual_import'] = meta
    ev.shipment_snapshot = json.dumps(snap,ensure_ascii=False,default=str)
    missing = missing_fields(ev)
    # get_db rolls the transaction back on rejection: never persist partial completion.
    if missing:
        raise HTTPException(400, '未完整补齐，不能保存：' + '、'.join(missing))
    if 'claim_amount' in payload and (payload['claim_amount'] is None or str(payload['claim_amount']).strip() == ''):
        raise HTTPException(400, '仓库赔付金额必填，不能留空')
    meta['manual_completed'] = True
    meta['completion_basis'] = str(payload['reason']).strip()
    snap['_manual_import'] = meta
    ev.shipment_snapshot = json.dumps(snap,ensure_ascii=False,default=str)
    _validate_amounts(ev)
    db.flush()
    audit_diff(db, ev, actor, before)
    audit_action(db, ev, actor, 'manual_completion', field='人工补齐依据', new_value=payload['reason'])
    db.commit()
    return {'ok': True, 'completed': True, 'event': serialize_event(ev)}
