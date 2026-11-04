"""渠道无关的命令 / 推送层。导入时会注册企业微信和个人微信适配器。"""

from backend.app.services.bots import router
from backend.app.services.bots.adapters import WeChatChannel, WeComChannel

router.register(WeComChannel())
router.register(WeChatChannel())

__all__ = ["router"]
