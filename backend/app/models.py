"""数据模型（2.0，按确认规则重建）。

业务链路（用户确认）：
  顾客投诉破损/丢件 → 卖方垫付退款 → 【所有理赔都经由仓库方追责】
  → 仓库方回复结果（多轮）→ 赔付 / 仲裁驳回 / 放弃。

因此模型为**单索赔线**：
  - 每事件一条索赔记录，责任方字段区分 仓库方自身责任 / 快递(经由仓库追) / 共担
  - 仓库方回复是**多轮**时间线（ClaimReply）
  - 责任认定可变更，留痕（DutyChange），不覆盖历史
  - 旺店通数据（退款/退回明细/入库）以快照落库，接口挂了也不影响台账
"""
from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

MONEY = Numeric(18, 2)


class IncidentType(str, enum.Enum):
    DAMAGE = "damage"      # 破损
    LOSS = "loss"          # 丢件
    SHORTAGE = "shortage"  # 短少/少件
    WRONG = "wrong"        # 错发
    UNDELIVERED = "undelivered"  # 未派送/派送错误
    OTHER = "other"        # 其他


class AfterSalesType(str, enum.Enum):
    ORIGINAL_RETURN = "original_return"  # 原单退回
    RETURN_ONLY = "return_only"          # 仅退回
    RETURN_REFUND = "return_refund"      # 退回退款


class DutyParty(str, enum.Enum):
    WAREHOUSE = "warehouse"  # 仓库方自身责任：漏入库/二次包装/分拣破损
    EXPRESS = "express"      # 快递责任（经由仓库方向快递追责）
    SHARED = "shared"        # 共担
    JUXIAOFAN = "juxiaofan"  # 聚小翻
    BUYER = "buyer"          # 买家
    CUSTOM = "custom"        # 人工输入
    UNDECIDED = "undecided"  # 待定


class ClaimStatus(str, enum.Enum):
    DRAFT = "draft"          # 已登记，未提交仓库方
    SUBMITTED = "submitted"  # 已提交仓库方
    REPLYING = "replying"    # 仓库方已回复，处理中（如"待补证明"）
    PAID = "paid"            # 已赔付（钱到账）
    REJECTED = "rejected"    # 仲裁驳回，无法追责
    GAVE_UP = "gave_up"      # 放弃追偿


TERMINAL_STATUSES = {ClaimStatus.PAID, ClaimStatus.REJECTED, ClaimStatus.GAVE_UP}


class ReplySource(str, enum.Enum):
    WAREHOUSE = "warehouse"  # 仓库方反馈
    OWNER = "owner"      # 货主（自己）备注


def platform_of(shop_name: str) -> str:
    """从店铺名识别平台。"""
    n = (shop_name or "").lower()
    for key, val in (
        ("抖音", "抖音"), ("快手", "快手"), ("拼多多", "拼多多"), ("pdd", "拼多多"),
        ("1688", "1688"), ("京东", "京东"), ("淘宝", "淘宝"), ("天猫", "天猫"), ("得物", "得物"),
    ):
        if key in n:
            return val
    return ""


class ClaimEvent(Base):
    """一单售后理赔事件。"""

    __tablename__ = "claim_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    event_date: Mapped[date] = mapped_column(Date, index=True)
    # 精确到分钟的售后登记时间；event_date 保留用于兼容历史数据和月份索引。
    event_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # ---- 关联键：旺店通订单号 / 台账快递单号（二选一填，都能自动关联）----
    wdt_tid: Mapped[str] = mapped_column(String(64), default="", index=True)   # 旺店通订单号(tid)
    ship_carrier: Mapped[str] = mapped_column(String(64), default="")          # 发货快递公司
    ship_logistics_no: Mapped[str] = mapped_column(String(64), default="", index=True)  # 发货物流单号

    # ---- 旺店通自动带出（快照）----
    shop_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    platform: Mapped[str] = mapped_column(String(32), default="", index=True)
    order_amount: Mapped[Decimal] = mapped_column(MONEY, default=0)        # 旺店通订单金额
    shipment_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    refund_amount: Mapped[Decimal] = mapped_column(MONEY, default=0)           # 顾客已赔(实际退款)
    refund_reason: Mapped[str] = mapped_column(String(255), default="")
    return_carrier: Mapped[str] = mapped_column(String(64), default="")        # 退货物流(自动)
    return_logistics_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    return_goods_count: Mapped[Decimal] = mapped_column(MONEY, default=0)      # 退货件数
    wdt_refund_no: Mapped[str] = mapped_column(String(64), default="")         # 旺店通退款单号
    wdt_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    wdt_raw: Mapped[str] = mapped_column(Text, default="{}")                   # 退款单原始JSON

    # ---- 手工登记 ----
    # 显式 length：SQLAlchemy Enum(native_enum=False) 默认按「建表时的枚举值」定宽，
    # 之后新增更长枚举值（如 undelivered=11）不会自动扩列，会触发 StringDataRightTruncation。
    incident_type: Mapped[IncidentType] = mapped_column(
        Enum(IncidentType, native_enum=False, length=32), default=IncidentType.DAMAGE, index=True
    )
    after_sales_type: Mapped[AfterSalesType | None] = mapped_column(
        Enum(AfterSalesType, native_enum=False, length=32), nullable=True, index=True
    )
    customer_claim: Mapped[str] = mapped_column(Text, default="")              # 售后内容/事故描述

    # ---- 责任与索赔（单线，经由仓库方）----
    duty_party: Mapped[DutyParty] = mapped_column(
        Enum(DutyParty, native_enum=False), default=DutyParty.UNDECIDED, index=True
    )
    duty_party_custom: Mapped[str] = mapped_column(Text, default="")
    duty_basis: Mapped[str] = mapped_column(Text, default="")
    claim_amount: Mapped[Decimal] = mapped_column(MONEY, default=0)            # 申请金额(=台账"赔付金额"，界面/报表显示为"仓库赔付金额")
    received_amount: Mapped[Decimal] = mapped_column(MONEY, default=0)         # 实收金额
    status: Mapped[ClaimStatus] = mapped_column(
        Enum(ClaimStatus, native_enum=False), default=ClaimStatus.DRAFT, index=True
    )
    warehouse_ref: Mapped[str] = mapped_column(String(64), default="")  # 仓库方工单号/受理号

    # ---- 入库检测快照（一键查旺店通入库）----
    stockin_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stockin_found: Mapped[bool] = mapped_column(Boolean, default=False)        # 是否查到入库
    stockin_status: Mapped[str] = mapped_column(String(16), default="")        # 入库单状态
    stockin_warehouse: Mapped[str] = mapped_column(String(128), default="")    # 入库仓库全名
    stockin_is_defective: Mapped[bool] = mapped_column(Boolean, default=False) # 是否次品仓
    stockin_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stockin_raw: Mapped[str] = mapped_column(Text, default="{}")

    closed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    note: Mapped[str] = mapped_column(Text, default="")

    # ---- 登记人（报表按账号/渠道维度的依据；历史数据为空串，展示为「历史导入」）----
    created_by: Mapped[str] = mapped_column(String(64), default="", index=True)      # 登记人登录名
    created_channel: Mapped[str] = mapped_column(String(32), default="", index=True)  # 登记时所属渠道

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    replies: Mapped[list["ClaimReply"]] = relationship(
        back_populates="event", cascade="all, delete-orphan",
        order_by="ClaimReply.id", lazy="selectin",
    )
    refund_items: Mapped[list["RefundItem"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    stockin_items: Mapped[list["StockinItem"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    duty_changes: Mapped[list["DutyChange"]] = relationship(
        back_populates="event", cascade="all, delete-orphan",
        order_by="DutyChange.id", lazy="selectin",
    )
    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    refund_records: Mapped[list["RefundRecord"]] = relationship(
        back_populates="event", cascade="all, delete-orphan",
        order_by="RefundRecord.refund_no", lazy="selectin",
    )

    # ---- 派生金额（不落库，口径统一在业务层）----
    @property
    def gap_amount(self) -> Decimal:
        """未收回 = 申请 − 实收。"""
        return self.claim_amount - self.received_amount

    @property
    def net_loss(self) -> Decimal:
        """净损失 = 顾客已赔 − 实收。

        顾客已赔为 0 视为「未回填」（真实场景几乎不存在 0 元赔付），
        此时净损失不可计算，返回 0 并由前端标注待回填，避免出现误导性负数。
        """
        if self.refund_amount <= 0:
            return Decimal("0")
        return self.refund_amount - self.received_amount


class ClaimReply(Base):
    """多轮回复时间线。仓库方反馈 / 货主备注，各轮独立，永不覆盖。"""

    __tablename__ = "claim_replies"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("claim_events.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[ReplySource] = mapped_column(Enum(ReplySource, native_enum=False))
    round_no: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    event: Mapped[ClaimEvent] = relationship(back_populates="replies")


class DutyChange(Base):
    """责任变更留痕。例：初判快递责任，后查明仓库二次包装 → 改判并留原因。"""

    __tablename__ = "duty_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("claim_events.id", ondelete="CASCADE"), index=True
    )
    from_party: Mapped[DutyParty] = mapped_column(Enum(DutyParty, native_enum=False))
    to_party: Mapped[DutyParty] = mapped_column(Enum(DutyParty, native_enum=False))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    event: Mapped[ClaimEvent] = relationship(back_populates="duty_changes")


class RefundItem(Base):
    """客户退回明细快照（商品级），来自旺店通退款单 refund_order_list。"""

    __tablename__ = "refund_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("claim_events.id", ondelete="CASCADE"), index=True
    )
    spec_no: Mapped[str] = mapped_column(String(64), default="")
    barcode: Mapped[str] = mapped_column(String(64), default="")
    goods_name: Mapped[str] = mapped_column(String(255), default="")
    spec_name: Mapped[str] = mapped_column(String(128), default="")
    order_num: Mapped[Decimal] = mapped_column(MONEY, default=0)   # 购买数
    refund_num: Mapped[Decimal] = mapped_column(MONEY, default=0)  # 退货数
    stockin_num: Mapped[Decimal] = mapped_column(MONEY, default=0) # 旺店通侧已入库数
    cost_price: Mapped[Decimal] = mapped_column(MONEY, default=0)

    event: Mapped[ClaimEvent] = relationship(back_populates="refund_items")


class RefundRecord(Base):
    """一笔退款单记录。同一订单可能有多笔退款（部分退后再退），
    全部挂在同一张登记表单下，逐笔展示。"""
    __tablename__ = "refund_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("claim_events.id", ondelete="CASCADE"), index=True
    )
    refund_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    refund_amount: Mapped[Decimal] = mapped_column(MONEY, default=0)
    refund_reason: Mapped[str] = mapped_column(String(255), default="")
    refund_status: Mapped[str] = mapped_column(String(16), default="")
    return_carrier: Mapped[str] = mapped_column(String(64), default="")
    return_logistics_no: Mapped[str] = mapped_column(String(64), default="")
    return_goods_count: Mapped[Decimal] = mapped_column(MONEY, default=0)
    refund_time: Mapped[str] = mapped_column(String(32), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    event: Mapped[ClaimEvent] = relationship(back_populates="refund_records")


class StockinItem(Base):
    """退货入库明细快照（商品档案+效期），来自旺店通退货入库单 details_list。"""

    __tablename__ = "stockin_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("claim_events.id", ondelete="CASCADE"), index=True
    )
    spec_no: Mapped[str] = mapped_column(String(64), default="")
    goods_count: Mapped[Decimal] = mapped_column(MONEY, default=0)
    cost_price: Mapped[Decimal] = mapped_column(MONEY, default=0)
    production_date: Mapped[str] = mapped_column(String(32), default="")
    validity_days: Mapped[Decimal] = mapped_column(MONEY, default=0)
    expire_date: Mapped[str] = mapped_column(String(32), default="")

    event: Mapped[ClaimEvent] = relationship(back_populates="stockin_items")


class Evidence(Base):
    """凭证附件（取代 Excel 的 =DISPIMG 贴图）。可挂在某轮回复上。"""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("claim_events.id", ondelete="CASCADE"), index=True
    )
    reply_id: Mapped[int | None] = mapped_column(
        ForeignKey("claim_replies.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="other", index=True)
    source_party: Mapped[str] = mapped_column(String(32), default="unclassified")
    filename: Mapped[str] = mapped_column(String(255), default="")
    stored_path: Mapped[str] = mapped_column(String(512), default="")
    content_type: Mapped[str] = mapped_column(String(128), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    caption: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    event: Mapped[ClaimEvent] = relationship(back_populates="evidence")


# 撞单检测用的复合索引：同订单号或同快递单号多次登记
Index("ix_events_tid_date", ClaimEvent.wdt_tid, ClaimEvent.event_date)
Index("ix_events_shipno_date", ClaimEvent.ship_logistics_no, ClaimEvent.event_date)


class AuditLog(Base):
    """操作审计：谁、何时、把哪个事件的哪个字段从什么改成什么。

    金额（顾客已赔/申请/实收）、状态、责任方等核心字段改动必留痕，
    满足对账追责时的「钱账可追溯」要求。事件删除后 event_id 置空，
    但 event_code 快照保留，记录仍可查。
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("claim_events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    actor: Mapped[str] = mapped_column(String(128), default="")        # 操作人（邮箱/开发身份）
    action: Mapped[str] = mapped_column(String(32), default="update")  # create/update/delete/sync/stockin/duty/reply
    field: Mapped[str] = mapped_column(String(64), default="-")        # 改动字段（中文标签）/- 表示整单动作
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def as_dict(self) -> dict:
        return {
            "id": self.id, "event_code": self.event_code, "actor": self.actor,
            "action": self.action, "field": self.field,
            "old_value": self.old_value, "new_value": self.new_value,
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }


class Channel(str, enum.Enum):
    """渠道方：决定这个账号「能改哪些字段」，不决定能看哪些数据。

    各渠道都看全量台账（仓库要核对入库、客服要跟进回复），
    区别只在「谁负责填什么」，避免互相覆盖对方的口径。
    """

    WAREHOUSE = "warehouse"      # 仓库：入库检测、责任判定、仓库工单号、实收
    OUTSOURCE = "outsource"      # 客服：售后原因/类型、售后内容、回复
    OPERATIONS = "operations"    # 运营：登记、金额、状态、店铺信息
    QQBOT = "qqbot"              # QQ群机器人：代客登记、凭证上传、沟通回复


class Role(str, enum.Enum):
    """角色：决定操作上限，与渠道正交（渠道管字段，角色管动作）。"""

    ADMIN = "admin"              # 全部权限 + 账号管理
    EDITOR = "editor"            # 可登记、修改、回复、导入导出
    VIEWER = "viewer"            # 只读 + 导出


CHANNEL_LABEL = {"warehouse": "仓库", "outsource": "客服", "operations": "运营",
                 "qqbot": "QQ群机器人"}
# 机器凭证登录时展示的登记人名（后台「登记人」列）
BOT_DISPLAY_NAME = {"qqbot": "QQ机器人"}
ROLE_LABEL = {"admin": "管理员", "editor": "编辑", "viewer": "只读"}

# 各渠道可写的字段。未列出的字段对该渠道只读，后端强制拦截，前端置灰。
# 「登记人」由登录身份自动写入，任何渠道都不能改。
CHANNEL_WRITABLE_FIELDS: dict[str, set[str]] = {
    "warehouse": {
        "warehouse_ref", "duty_party", "duty_party_custom", "duty_basis",
        "received_amount", "stockin", "status", "closed", "note",
    },
    # 客服：登记 + 售后原因/类型 + 内容 + 退款原因 + 客户索赔金额 + 沟通回复
    # 不含：责任判定（仓库口径）、实收/未收回（结算口径）、入库检测
    "outsource": {
        "event_date", "wdt_tid", "ship_carrier", "ship_logistics_no", "shop_name",
        "incident_type", "after_sales_type", "customer_claim", "refund_reason",
        "claim_amount", "note", "status",
    },
    # 运营：登记新单 + 全部业务字段（不含账号管理）
    "operations": {
        "event_date", "wdt_tid", "ship_carrier", "ship_logistics_no", "shop_name",
        "refund_amount", "refund_reason", "incident_type", "after_sales_type",
        "customer_claim", "duty_party", "duty_party_custom", "duty_basis",
        "claim_amount", "received_amount", "status", "warehouse_ref", "note", "closed",
    },
    # QQ群机器人：代客登记 + 上传凭证 + 沟通回复 + 客户口述的责任方
    # 不碰结算与核验口径（实收/未收回/净损失/入库/仓库工单号/关闭/判定依据）
    "qqbot": {
        "event_date", "wdt_tid", "ship_carrier", "ship_logistics_no", "shop_name",
        "incident_type", "after_sales_type", "customer_claim", "duty_party",
        "refund_reason", "claim_amount", "note", "status",
    },
}
# 管理员不受渠道字段限制（但仍受角色限制：只有 admin 才能管账号）。
ALL_FIELDS = "*"

# 允许「登记新售后」的渠道：运营和客服负责建单，外包客服可代客登记，
# 仓库方只在已有登记上回复/补入库，不建单。空串（历史账号）按不受限处理。
CAN_CREATE_CHANNELS = {"operations", "outsource", "qqbot"}

# 需要「编辑」及以上角色才能执行的动作。
WRITE_ACTIONS = {"create", "update", "delete", "reply", "duty", "sync", "import", "evidence"}


class User(Base):
    """登录账号（用户名或邮箱 + 密码）。首次启动按 ADMIN 配置自动建一个管理员。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 登录名：允许用户名（wh01 / kf01）或邮箱；统一小写存储，唯一。
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 渠道：warehouse / outsource / operations；空串表示未分配（按最小权限处理）。
    channel: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 角色：admin / editor / viewer；与 is_admin 保持同步（is_admin=True 即 admin）。
    role: Mapped[str] = mapped_column(String(16), default="editor")
    display_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------- 工单（企业微信 @ 触发 + 群聊扫描追溯）
#
# 工单是独立实体，不等同理赔事件（claim_events）：
#   - 群聊工单可能是「还没订单号的咨询/投诉」，生命周期不同；
#   - 但可软引用 claim_events（当确属某个订单时），related_order_no 不建硬外键，
#     避免「客户没给订单号」的咨询无法登记。
# 设计要点（见工单看板设计稿）：建单权只交给 @ 触发，群聊扫描只做增量归集。

class WorkOrderCategory(str, enum.Enum):
    RETURN = "return"        # 退货
    EXCHANGE = "exchange"    # 换货
    MISSING = "missing"      # 漏发
    DAMAGE = "damage"        # 破损
    WRONG = "wrong"          # 错发
    OTHER = "other"          # 其他


class WorkOrderPriority(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class WorkOrderStatus(str, enum.Enum):
    PENDING = "pending"            # 待处理
    PROCESSING = "processing"      # 处理中
    WAITING_CUSTOMER = "waiting"   # 待客户
    RESOLVED = "resolved"          # 已解决
    CLOSED = "closed"              # 已关闭
    OVERDUE = "overdue"            # 超时


class WorkOrderSource(str, enum.Enum):
    WECOM_GROUP = "wecom_group"    # 企业微信 @ 触发
    MANUAL = "manual"              # 后台手工
    SCAN = "scan"                  # 群聊扫描自动归集


class TimelineSource(str, enum.Enum):
    BOT_CREATED = "bot_created"    # 机器人建单（@售后助手）
    WECOM_SCAN = "wecom_scan"      # 群聊扫描自动归集
    AI_INFER = "ai_infer"          # AI 从群聊推断状态
    MANUAL = "manual"             # 人工记录


WOCAT_LABEL = {"return": "退货", "exchange": "换货", "missing": "漏发",
               "damage": "破损", "wrong": "错发", "other": "其他"}
WOPRI_LABEL = {"high": "高", "medium": "中", "low": "低"}
WOSTAT_LABEL = {"pending": "待处理", "processing": "处理中", "waiting": "待客户",
                "resolved": "已解决", "closed": "已关闭", "overdue": "超时"}
TL_SRC_LABEL = {"bot_created": "机器人建单", "wecom_scan": "群聊扫描",
                "ai_infer": "AI推断", "manual": "人工"}


class WorkOrder(Base):
    """一条售后工单。来源是企业微信 @ 触发，或由会话存档扫描增量归集。"""

    __tablename__ = "work_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # WO-YYYYMMDD-NNN
    source: Mapped[WorkOrderSource] = mapped_column(
        Enum(WorkOrderSource, native_enum=False), default=WorkOrderSource.WECOM_GROUP, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(128), default="")             # 客户微信昵称
    customer_wecom_id: Mapped[str] = mapped_column(String(128), default="", index=True)  # external_userid
    channel: Mapped[str] = mapped_column(String(255), default="")                   # 来源群/渠道
    related_order_no: Mapped[str] = mapped_column(String(64), default="", index=True)    # 软引用订单号
    claim_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("claim_events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category: Mapped[WorkOrderCategory] = mapped_column(
        Enum(WorkOrderCategory, native_enum=False, length=16), default=WorkOrderCategory.OTHER, index=True
    )
    priority: Mapped[WorkOrderPriority] = mapped_column(
        Enum(WorkOrderPriority, native_enum=False, length=8), default=WorkOrderPriority.MEDIUM
    )
    status: Mapped[WorkOrderStatus] = mapped_column(
        Enum(WorkOrderStatus, native_enum=False, length=16), default=WorkOrderStatus.PENDING, index=True
    )
    owner: Mapped[str] = mapped_column(String(64), default="")   # 负责人（客服名）
    summary: Mapped[str] = mapped_column(Text, default="")        # 问题摘要
    wecom_msg_id: Mapped[str] = mapped_column(String(128), default="", index=True)  # 原始消息id（幂等）
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    timelines: Mapped[list["WorkOrderTimeline"]] = relationship(
        back_populates="order", cascade="all, delete-orphan",
        order_by="WorkOrderTimeline.id", lazy="selectin",
    )


class WorkOrderTimeline(Base):
    """工单对话时间线：@ 建单 + 群聊扫描自动归集 + AI 推断状态，逐条留痕。"""

    __tablename__ = "work_order_timeline"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_order_id: Mapped[int] = mapped_column(
        ForeignKey("work_orders.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[TimelineSource] = mapped_column(
        Enum(TimelineSource, native_enum=False, length=16), default=TimelineSource.BOT_CREATED
    )
    author: Mapped[str] = mapped_column(String(128), default="")   # 发言者/操作者
    content: Mapped[str] = mapped_column(Text, default="")
    inferred_status: Mapped[str] = mapped_column(String(16), default="")  # AI 推断的状态值（若有）
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    order: Mapped[WorkOrder] = relationship(back_populates="timelines")
