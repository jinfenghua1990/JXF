"""运行配置。"""
from __future__ import annotations

import hmac

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _version() -> str:
    for p in (Path("/app/VERSION"), Path(__file__).resolve().parents[2] / "VERSION"):
        try:
            v = p.read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            continue
    return "0.0.0"


class Settings(BaseSettings):
    app_name: str = "售后登记表"
    app_version: str = _version()
    app_port: int = 8021

    database_url: str = "sqlite:///./claims.db"
    upload_dir: str = "/app/data/uploads"
    max_upload_mb: int = 20

    # 旺店通开放平台（凭证从环境变量/ .env 读，不进代码）
    wdt_sid: str = ""
    wdt_appkey: str = ""
    wdt_appsecret: str = ""
    wdt_base_url: str = "https://api.wangdian.cn/openapi2"
    wdt_timeout_seconds: int = 30

    # 淘宝/天猫订单需经旺店通授权的奇门自定义通道查询。
    wdt_qimen_appkey: str = ""
    wdt_qimen_appsecret: str = ""
    wdt_qimen_target_appkey: str = ""
    wdt_qimen_base_url: str = "https://hu3cgwt0tc.api.taobao.com/router/qm"

    # 旺店通查询窗口上限（实测超过30天报 code=2180 且不报错地返回空）
    wdt_query_window_days: int = 30

    # ===== 登录鉴权（外网暴露前必改）=====
    app_secret: str = ""        # token 签名密钥（必改！留空则启动报警并用临时默认串）
    admin_email: str = ""       # 初始管理员邮箱；首次启动自动建号，留空默认 admin@claims.local
    admin_password: str = ""    # 初始管理员密码（可选；留空则管理员需用邮箱验证码登录）
    app_password: str = ""      # 兼容旧配置：未设 admin_password 时作为初始密码兜底

    # 本系统仅支持「账号 + 密码」登录，不依赖邮件 / 验证码，无需 SMTP / Resend 配置。

    # 开发期免登录开关：设为 true 时跳过全部登录鉴权（任何人可直接进系统）。
    # 仅供本地开发调试使用；生产 / 外网部署务必保持 false（或删除本行）。
    disable_auth: bool = False

    # 机器凭证：机器人调用售后登记接口用（不需要账号+密码）。
    # 格式：渠道=密钥，多个用逗号分隔，例如 qqbot=xxxxxxxx
    bot_api_keys: str = ""

    # 企业微信 bot 调用建单接口用的共享令牌（与 bot 侧 WECOM_BOT_TOKEN 一致）。
    # 留空时若 disable_auth=true（开发期）则放行；生产必须为非空，否则拒绝建单。
    wecom_bot_token: str = ""

    def bot_channel_for_key(self, key: str) -> str:
        """按机器凭证反查所属渠道；不匹配返回空串。"""
        want = (key or "").strip()
        if not want:
            return ""
        for item in (self.bot_api_keys or "").split(","):
            if "=" not in item:
                continue
            ch, k = item.split("=", 1)
            k = k.strip()
            if k and hmac.compare_digest(k, want):
                return ch.strip()
        return ""

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()
