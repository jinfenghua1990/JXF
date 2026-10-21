"""Explicit, audited exceptions for historical imports; normal creation remains verified."""
import json


def metadata(ev):
    try:
        return json.loads(ev.shipment_snapshot or '{}').get('_manual_import', {})
    except (ValueError, TypeError):
        return {}


def missing_fields(ev):
    m = metadata(ev)
    if not m:
        return []
    fields = []
    for key, label in [('wdt_tid', '原始订单号'), ('ship_logistics_no', '发货物流单号'), ('shop_name', '店铺名称'),
                       ('ship_carrier', '发货快递公司')]:
        if not (getattr(ev, key) or '').strip():
            fields.append(label)
    if m.get('amount_missing'):
        fields.append('仓库赔付金额（原表存在金额冲突，需确认）' if m.get('amount_conflict') else '仓库赔付金额')
    return fields
