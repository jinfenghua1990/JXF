"""旺店通开放平台客户端。

所有接口均已在生产凭证上实测（2026-10-13）：
- refund_query.php            按 tid / logistics_no 精确查退款单 ✅
- stockin_order_query_refund  退货入库单（含次品仓识别+效期字段）✅
- shop.php                    店铺档案 ✅

已知坑（全部实测踩出，代码里硬编码防御）：
1. start_time/end_time 跨度 >30天 → code=2180 且静默返回空，不报错
2. total_count 在末页会返回 0，判停必须用"返回条数 < page_size"
3. 退款金额用 actual_refund_amount；goods_amount 实测恒为 0 不可用
4. is_goods_received 实测905单全为0，"是否收货"以入库单为准
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..config import settings


class WdtError(RuntimeError):
    """旺店通接口业务失败。"""


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _dec(v: Any) -> Decimal:
    try:
        s = _s(v).strip()
        return Decimal(s) if s else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


def _dt(v: Any) -> datetime | None:
    t = _s(v).strip()
    if not t or t.startswith("0000"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def sign_params(params: dict[str, Any], appsecret: str) -> str:
    """旺店通标准签名：参数名排序 + 字节长度前缀 + 末尾拼 appsecret + MD5。
    已用官方测试向量校验（ad4e6fe037ea6e3ba4768317be9d1309）。"""
    chunks: list[str] = []
    for key in sorted(params):
        value = _s(params[key])
        chunks.append(
            f"{len(key.encode('utf-8')):02d}-{key}:{len(value.encode('utf-8')):04d}-{value}"
        )
    return hashlib.md5((";".join(chunks) + appsecret).encode("utf-8")).hexdigest()


def sign_qimen(params: dict[str, Any], appsecret: str) -> str:
    """旺店通官方奇门 SDK 的 TOP MD5 签名。"""
    text = appsecret + "".join(k + _s(params[k]) for k in sorted(params)) + appsecret
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


class WangdianClient:
    def __init__(self) -> None:
        self.sid = settings.wdt_sid
        self.appkey = settings.wdt_appkey
        self.appsecret = settings.wdt_appsecret
        self.base_url = settings.wdt_base_url.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.sid and ((self.appkey and self.appsecret) or self.qimen_configured))

    @property
    def qimen_configured(self) -> bool:
        return bool(self.sid and settings.wdt_qimen_appkey and settings.wdt_qimen_appsecret
                    and settings.wdt_qimen_target_appkey)

    def _request_qimen(self, endpoint: str, business: dict[str, Any]) -> dict[str, Any]:
        methods = {
            "shop.php": "wdt.shop.query",
            "trade_query.php": "wdt.trade.query",
            "stockout_order_query_trade.php": "wdt.stockout.order.query.trade",
            "refund_query.php": "wdt.refund.query",
            "stockin_order_query_refund.php": "wdt.stockin.order.query.refund",
        }
        if endpoint not in methods:
            raise WdtError(f"奇门查询尚未接入此接口：{endpoint}")
        system = {
            "app_key": settings.wdt_qimen_appkey,
            "target_app_key": settings.wdt_qimen_target_appkey,
            "method": methods[endpoint], "v": "2.0", "format": "json", "sign_method": "md5",
            "timestamp": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        }
        body = {"sid": self.sid, **{k: _s(v) for k, v in business.items()}}
        system["sign"] = sign_qimen({**system, **body}, settings.wdt_qimen_appsecret)
        with httpx.Client(timeout=settings.wdt_timeout_seconds) as client:
            response = client.post(settings.wdt_qimen_base_url, params=system, data=body)
            response.raise_for_status()
            payload = response.json()
        # 官方 SDK 的 JSON 响应带一个外层 response 节点。
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key.endswith("response") and isinstance(value, dict):
                    payload = value
                    break
        if not isinstance(payload, dict):
            raise WdtError("旺店通奇门接口返回格式不正确")
        code = int(payload.get("errorcode", payload.get("code", -1)))
        if code != 0 or payload.get("flag") == "failure":
            message = payload.get("sub_message") or payload.get("sub_msg") or payload.get("message") or payload.get("msg") or "未知错误"
            raise WdtError(f"旺店通奇门接口失败 code={code}: {message}")
        payload["code"] = 0
        return payload

    def _request(self, endpoint: str, business: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise WdtError("旺店通凭证未配置（WDT_SID / WDT_APPKEY / WDT_APPSECRET）")
        if self.qimen_configured:
            return self._request_qimen(endpoint, business)
        params: dict[str, Any] = {
            "sid": self.sid,
            "appkey": self.appkey,
            "timestamp": int(time.time()),
            **business,
        }
        params["sign"] = sign_params(params, self.appsecret)
        url = f"{self.base_url}/{endpoint}"
        with httpx.Client(timeout=settings.wdt_timeout_seconds) as client:
            resp = client.post(url, data={k: _s(v) for k, v in params.items()})
            resp.raise_for_status()
            payload = resp.json()
        code = int(payload.get("code", -1))
        if code != 0:
            raise WdtError(f"旺店通接口失败 code={code}: {payload.get('message', '')}")
        return payload

    @staticmethod
    def _window(start: datetime | None = None, end: datetime | None = None) -> dict[str, str]:
        """构造查询窗口，自动防 30 天超限（坑1）。"""
        now = datetime.now()
        end = end or (now - timedelta(minutes=3))
        start = start or (end - timedelta(days=settings.wdt_query_window_days))
        if (end - start).days > settings.wdt_query_window_days:
            start = end - timedelta(days=settings.wdt_query_window_days)
        if end > now:
            end = now - timedelta(minutes=3)
        return {
            "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ------------------------------------------------------- 退款单

    def find_refunds(
        self,
        *,
        tid: str = "",
        logistics_no: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """按订单号或物流单号查退款单。

        实测：tid=台账订单号（抖音19位/快手16位）；logistics_no 亦可反查。
        """
        if not tid and not logistics_no:
            raise ValueError("tid 与 logistics_no 至少填一个")
        biz: dict[str, Any] = {**self._window(start, end), "page_no": 0, "page_size": 100}
        if tid:
            biz["tid"] = tid
        if logistics_no:
            biz["logistics_no"] = logistics_no
        payload = self._request("refund_query.php", biz)
        return payload.get("refunds") or []

    @staticmethod
    def parse_refund(row: dict[str, Any]) -> dict[str, Any]:
        """退款单 → 台账字段快照。金额一律取 actual_refund_amount（坑3）。"""
        return {
            "wdt_refund_no": _s(row.get("refund_no")),
            "wdt_tid": _s(row.get("tid")),
            "shop_name": _s(row.get("shop_name")),
            "platform": _s(row.get("platform_id")),
            "refund_amount": _dec(row.get("actual_refund_amount") or row.get("refund_amount")),
            "refund_reason": _s(row.get("refund_reason")),
            "return_carrier": _s(row.get("return_logistics_name")),
            "return_logistics_no": _s(row.get("return_logistics_no") or row.get("logistics_no")),
            "return_goods_count": _dec(row.get("return_goods_count")),
            "refund_status": _s(row.get("status")),
            "refund_time": _s(row.get("refund_time")),
            "raw": row,
        }

    @staticmethod
    def parse_refund_items(row: dict[str, Any]) -> list[dict[str, Any]]:
        """客户退回明细（商品级）。"""
        out = []
        for d in row.get("refund_order_list") or []:
            out.append(
                {
                    "spec_no": _s(d.get("spec_no")),
                    "barcode": _s(d.get("barcode")),
                    "goods_name": _s(d.get("goods_name")),
                    "spec_name": _s(d.get("spec_name")),
                    "order_num": _dec(d.get("order_num")),
                    "refund_num": _dec(d.get("refund_num")),
                    "stockin_num": _dec(d.get("stockin_num")),
                    "cost_price": _dec(d.get("cost_price")),
                }
            )
        return out

    # ------------------------------------------------------- 退货入库

    def search_refund_stockins(
        self, *, order_no: str = "", logistics_no: str = "",
        start: datetime | None = None, end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """查退货入库单，按入库时间窗口扫，再在结果里按单号/运单过滤。

        接口本身不支持按订单号过滤（实测），所以窗口内分页拉取后本地匹配。
        """
        ono, lno = _s(order_no).strip(), _s(logistics_no).strip()
        if not ono and not lno:
            return []
        window = self._window(start, end)
        rows: list[dict[str, Any]] = []
        page = 0
        while True:  # 坑2：判停用条数，不用 total_count
            payload = self._request(
                "stockin_order_query_refund.php",
                {**window, "page_no": page, "page_size": 100},
            )
            batch = payload.get("stockin_list") or []
            rows.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            if page > 30:  # 3000单保护
                break

        ono, lno = _s(order_no), _s(logistics_no)
        hits = []
        def exact_token(value, text):
            return bool(value and re.search(r"(?<![A-Za-z0-9_-])" + re.escape(value) + r"(?![A-Za-z0-9_-])", text))
        for r in rows:
            src = _s(r.get("src_order_no"))
            remark = _s(r.get("remark"))
            if ono and (exact_token(ono, src) or exact_token(ono, remark)):
                hits.append(r)
            elif lno and exact_token(lno, remark):
                hits.append(r)
        return hits

    @staticmethod
    def parse_stockin(row: dict[str, Any]) -> dict[str, Any]:
        """入库单 → 检测快照。次品仓识别 + 效期。"""
        wh = _s(row.get("warehouse_name"))
        return {
            "stockin_no": _s(row.get("order_no")),
            "stockin_status": _s(row.get("status")),
            "stockin_warehouse": wh,
            "stockin_is_defective": ("次品" in wh) or ("不良" in wh),
            "stockin_time": _dt(row.get("stockin_time")),
            "raw": row,
        }

    @staticmethod
    def parse_stockin_items(row: dict[str, Any]) -> list[dict[str, Any]]:
        """入库商品档案+效期（实测80/80明细带效期字段）。"""
        out = []
        for d in row.get("details_list") or []:
            out.append(
                {
                    "spec_no": _s(d.get("spec_no")),
                    "goods_count": _dec(d.get("goods_count")),
                    "cost_price": _dec(d.get("cost_price")),
                    "production_date": _s(d.get("production_date")),
                    "validity_days": _dec(d.get("validity_days")),
                    "expire_date": _s(d.get("expire_date")),
                }
            )
        return out

    # ------------------------------------------------------- 销售出库单

    def find_sales_stockouts(
        self,
        *,
        src_tid: str = "",
        logistics_no: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """按原始订单号或发货物流号查发货信息。

        旺店通 stockout_order_query_trade 只支持按 src_tid 查询，不能按物流号查。
        logistics_no 使用 trade_query.php 的精确查询，且必须省略时间窗口（该接口
        带时间时按最后修改时间查询，最大跨度仅 60 分钟）。
        """
        if not src_tid and not logistics_no:
            raise ValueError("src_tid 与 logistics_no 至少填一个")
        if logistics_no:
            # 官方接口规定 logistics_no 精确查询时其余筛选项不起效，时间范围也不传。
            payload = self._request(
                "trade_query.php",
                {"page_no": 0, "page_size": 100, "logistics_no": logistics_no},
            )
            hits = []
            for trade in payload.get("trades") or []:
                if _s(trade.get("logistics_no")) != logistics_no:
                    continue
                if src_tid and src_tid not in [x.strip() for x in _s(trade.get("src_tids")).replace(";", ",").split(",")]:
                    continue
                hits.append({
                    **trade,
                    "order_no": _s(trade.get("stockout_no")),
                    "src_order_no": _s(trade.get("trade_no")),
                    "src_trade_no": _s(trade.get("src_tids")),
                    "consign_time": _s(trade.get("consign_time") or trade.get("to_deliver_time")),
                    "goods_total_amount": _s(trade.get("goods_amount")),
                })
            if hits or not src_tid:
                return hits

        # 原始平台订单号可单独精确查销售出库单，不需要时间窗口或分页扫描。
        payload = self._request(
            "stockout_order_query_trade.php",
            {"src_tid": src_tid, "page_no": 0, "page_size": 100},
        )
        rows = payload.get("stockout_list") or payload.get("order_list") or []
        if logistics_no:
            rows = [row for row in rows if _s(row.get("logistics_no")) == logistics_no
                    or any(_s(item.get("logistics_no")) == logistics_no
                           for item in row.get("logistics_list") or [])]
        return rows

    @staticmethod
    def parse_sales_stockout(row: dict[str, Any]) -> dict[str, Any]:
        """销售出库单 → 发货快照。证明「货已发出」，理赔举证用。"""
        # 旺店通出库单同时返回 logistics_name / logistics_code；旧实现只取了
        # 运单号，导致页面上的“发货快递公司”一直为空。
        # 不同旺店通接口版本的商品明细字段名略有差异，统一成页面使用的快照结构。
        details: list[dict[str, Any]] = []
        for key in ("details_list", "goods_list", "goods_details", "order_list", "orders"):
            value = row.get(key)
            if isinstance(value, list):
                details = [x for x in value if isinstance(x, dict)]
                if details:
                    break
        goods_items = []
        for item in details:
            qty = _dec(item.get("goods_count") or item.get("order_num") or item.get("goods_num") or item.get("num"))
            amount = _dec(item.get("goods_amount") or item.get("total_amount") or item.get("amount"))
            if not amount and item.get("price") is not None:
                amount = _dec(item.get("price")) * qty
            goods_items.append({
                "barcode": _s(item.get("barcode") or item.get("spec_no") or item.get("goods_no")),
                "name": _s(item.get("goods_name") or item.get("name") or item.get("goods_name_spec")),
                "quantity": qty,
                "amount": amount,
            })
        total_amount = sum((x["amount"] for x in goods_items), _dec("0"))
        if not total_amount:
            total_amount = _dec(row.get("goods_total_amount") or row.get("goods_amount"))
        return {
            "stockout_no": _s(row.get("order_no")),
            "src_order_no": _s(row.get("src_order_no")),  # 旺店通系统单号 JY 开头
            # 原始平台订单号：物流号反查时，列表需要展示这个号码，而不是 JY 出库关联号。
            "source_tid": _s(row.get("src_trade_no") or row.get("src_tids") or row.get("src_tid")),
            "shop_name": _s(row.get("shop_name")),
            "warehouse_name": _s(row.get("warehouse_name")),
            "logistics_no": _s(row.get("logistics_no")),
            "carrier": _s(row.get("logistics_name") or row.get("logistics_type_name")),
            "carrier_code": _s(row.get("logistics_code")),
            "consign_time": _s(row.get("consign_time")),
            "status": _s(row.get("status")),
            "goods_count": _dec(row.get("goods_count")),
            "goods_amount": total_amount,
            "goods_items": goods_items,
            "goods_total_amount": total_amount,
            "raw": row,
        }

    # ------------------------------------------------------- 基础

    def test_connection(self) -> dict[str, Any]:
        payload = self._request("shop.php", {})
        shops = payload.get("shoplist") or payload.get("shop_list") or []
        return {"shop_count": len(shops), "shops": shops[:50]}
