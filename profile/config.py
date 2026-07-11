import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TRAE_MEMORY_DIR = Path("C:/Users/17734/.trae-cn/memory/projects")
MARVIS_DATA_DIR = Path("C:/Users/17734/AppData/Roaming/Tencent/Marvis/User/2E51332FD5D7CCFEA611C89585433078/database")
OBSIDIAN_OUTPUT_DIR = Path("d:/AAAmyPrj/gitee/obsidian/我的文档/AI使用/画像产出")

DB_PATH = PROJECT_ROOT / "data" / "profile.db"
LOG_DIR = PROJECT_ROOT / "logs"


def ensure_dirs() -> None:
    """确保画像系统所需的数据目录存在。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    OBSIDIAN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


CLAIMED_DIRECTION = {"主": "Agent", "次": "Memory"}

# LLM 配置
# 优先读取 LONGCAT_API_KEY，其次是 SILICONFLOW_API_KEY
LLM_API_KEY = os.getenv("LONGCAT_API_KEY", os.getenv("SILICONFLOW_API_KEY", ""))
LLM_API_URL = "https://api.longcat.chat/openai/v1/chat/completions"
LLM_MODEL = "LongCat-2.0"
LLM_TIMEOUT = 120
LLM_MAX_TOKENS = 4096
LLM_TEMPERATURE = 0.3

# LLM 不可用时是否降级到规则分析
LLM_FALLBACK_TO_RULES = True

# 刷新频率
REFRESH_SCHEDULE = "weekly"
