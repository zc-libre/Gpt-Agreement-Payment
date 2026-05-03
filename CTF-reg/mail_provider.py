"""邮箱服务（cloudflare_temp_email Admin API 路径）。

历史上这个模块走 IMAP 拉 QQ 邮箱，随后迁到 CF Email Worker → KV。
现在使用 cloudflare_temp_email Admin API：

    create_mailbox  → POST /admin/new_address
    wait_for_otp    → GET /admin/mails?address=... → 本地解析 raw MIME

只支持 Admin API，不使用地址 JWT 或用户 API。Cloudflare 平台凭证
（api_token / zone_names）走 SQLite runtime_meta[secrets].cloudflare 或
环境变量；Admin API 凭证（admin_auth/custom_auth）放 MailConfig。
"""
from __future__ import annotations

import logging
import random
import string
from typing import Optional

from temp_mail_admin_provider import TempMailAdminProvider

logger = logging.getLogger(__name__)


class MailProvider:
    """生成根域名邮箱 + 委托 temp-mail Admin API 取 OTP。"""

    def __init__(self, mail_config):
        self.mail_config = mail_config
        self.catch_all_domain = mail_config.catch_all_domain
        self._provider: Optional[TempMailAdminProvider] = None
        self._reuse_email: Optional[str] = None  # 兼容 register-only resume

    @staticmethod
    def _random_name() -> str:
        letters1 = "".join(random.choices(string.ascii_lowercase, k=5))
        numbers = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        letters2 = "".join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))
        return letters1 + numbers + letters2

    def create_mailbox(self) -> str:
        """通过 Admin API 创建邮箱（也可复用 _reuse_email）。"""
        if self._reuse_email:
            addr = self._reuse_email
            self._reuse_email = None
            logger.info(f"复用邮箱: {addr}")
            return addr
        if not self.catch_all_domain:
            raise RuntimeError(
                "MailProvider.create_mailbox: catch_all_domain 未配置；"
                "temp-mail Admin API 需要基础 domain"
            )
        addr = self._get_provider().create_address(
            self._random_name(),
            self.catch_all_domain,
        )
        logger.info(f"邮箱已创建: {addr} (路径: cloudflare_temp_email Admin API)")
        return addr

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """阻塞等 OTP。只走 temp-mail Admin API，不做静默 fallback。"""
        logger.info(
            f"[mail] 走 temp-mail Admin API 取 OTP -> {email_addr} (timeout={timeout}s)"
        )
        return self._get_provider().wait_for_otp(
            email_addr, timeout=timeout, issued_after=issued_after
        )

    def _get_provider(self) -> TempMailAdminProvider:
        if self._provider is None:
            self._provider = TempMailAdminProvider.from_mail_config(self.mail_config)
        return self._provider
