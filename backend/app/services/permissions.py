"""渠道 × 角色权限。

设计（用户确认）：
  - 数据不隔离：三个渠道都看全量台账，避免仓库/客服各看一半、对不上账。
  - 角色定动作上限：admin 全权 + 管账号；editor 可登记/修改/回复/导入导出；viewer 只读。
  - 渠道定字段范围：谁负责填什么就只让他改什么，防止互相覆盖口径。
      · 仓库         → 入库、责任判定、仓库工单号、实收、状态
      · 外包客服     → 售后原因/类型、售后内容、退款原因、回复
      · 运营和客服   → 登记与全部业务字段（不含账号管理）

历史数据没有渠道（channel=''），按「不受字段限制」处理，否则老账号会被锁死。
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException

from ..config import settings
from ..models import (
    ALL_FIELDS,
    CAN_CREATE_CHANNELS,
    CHANNEL_LABEL,
    CHANNEL_WRITABLE_FIELDS,
    ROLE_LABEL,
    Channel,
    Role,
    User,
)
from ..security import require_auth

# ---------------------------------------------------------------- 身份


@dataclass(frozen=True)
class Identity:
    """当前登录人的权限上下文。"""

    email: str
    role: str = Role.EDITOR.value
    channel: str = ""
    display_name: str = ""
    dev_mode: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN.value

    @property
    def can_write(self) -> bool:
        return self.role in (Role.ADMIN.value, Role.EDITOR.value)

    @property
    def can_create(self) -> bool:
        """是否可登记新售后：只看渠道（仓库方不建单），角色需为编辑及以上。"""
        if not self.can_write:
            return False
        if self.is_admin or not self.channel:
            return True
        return self.channel in CAN_CREATE_CHANNELS

    @property
    def channel_label(self) -> str:
        return CHANNEL_LABEL.get(self.channel, "")

    @property
    def role_label(self) -> str:
        return ROLE_LABEL.get(self.role, self.role)

    def writable_fields(self) -> set[str] | str:
        """本账号可写的字段集合；'*' 表示不限。"""
        if self.is_admin:
            return ALL_FIELDS
        if not self.channel:
            # 未分配渠道的历史账号：维持原有能力，不因升级而失去编辑权。
            return ALL_FIELDS
        return CHANNEL_WRITABLE_FIELDS.get(self.channel, set())

    def can_edit_field(self, field: str) -> bool:
        allowed = self.writable_fields()
        return allowed == ALL_FIELDS or field in allowed

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
            "role_label": self.role_label,
            "channel": self.channel,
            "channel_label": self.channel_label,
            "is_admin": self.is_admin,
            "can_write": self.can_write,
            "can_create": self.can_create,
            "writable_fields": (
                list(sorted(self.writable_fields()))
                if self.writable_fields() != ALL_FIELDS else ALL_FIELDS
            ),
            "dev_mode": self.dev_mode,
        }


def identity_of(user: User | None, email: str, *, dev_mode: bool = False) -> Identity:
    """把数据库账号转换成权限上下文，兼容未迁移的老账号。"""
    if user is None:
        return Identity(email=email, dev_mode=dev_mode)
    role = (user.role or "").strip()
    if not role:
        role = Role.ADMIN.value if user.is_admin else Role.EDITOR.value
    if user.is_admin:
        role = Role.ADMIN.value
    return Identity(
        email=user.email,
        role=role,
        channel=(user.channel or "").strip(),
        display_name=user.display_name or "",
        dev_mode=dev_mode,
    )


def current_identity(email: str = Depends(require_auth)) -> Identity:
    """FastAPI 依赖：当前登录人的权限上下文。"""
    if settings.disable_auth:
        # 开发模式视同管理员，但渠道留空（不受字段限制），保持调试方便。
        return Identity(email=email, role=Role.ADMIN.value, dev_mode=True)
    from ..database import SessionLocal
    from sqlalchemy import select

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
    finally:
        db.close()
    return identity_of(user, email)


# ---------------------------------------------------------------- 校验


def require_editor(ident: Identity = Depends(current_identity)) -> Identity:
    """需要「编辑」及以上角色。"""
    if not ident.can_write:
        raise HTTPException(403, "当前账号为只读权限，不能执行此操作")
    return ident


def require_admin_identity(ident: Identity = Depends(current_identity)) -> Identity:
    """需要管理员角色。"""
    if not ident.is_admin:
        raise HTTPException(403, "需要管理员权限")
    return ident


def check_fields(ident: Identity, fields: list[str] | set[str]) -> None:
    """字段级越权检查：任一字段不可写就整体拒绝，并说明是哪个渠道不让改。"""
    if not ident.can_write:
        raise HTTPException(403, "当前账号为只读权限，不能修改数据")
    allowed = ident.writable_fields()
    if allowed == ALL_FIELDS:
        return
    denied = [f for f in fields if f not in allowed]
    if not denied:
        return
    from ..services.engine import AUDIT_FIELDS

    label = {k: v for k, v in AUDIT_FIELDS}
    names = "、".join(label.get(f, f) for f in denied)
    who = ident.channel_label or "当前账号"
    raise HTTPException(
        403,
        f"{who}渠道不能修改：{names}。这些字段由对应渠道负责，请转交或联系管理员。",
    )
