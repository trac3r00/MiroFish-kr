"""\n配置管理\n统一从项目根目录的 .env 文件加载配置\n"""

import os
from dotenv import load_dotenv

# 加载项目根目录的 .env 文件
# 路径: MiroFish/.env (相对于 backend/app/config.py)
project_root_env = os.path.join(os.path.dirname(__file__), '../../.env')

if os.path.exists(project_root_env):
    load_dotenv(project_root_env, override=True)
else:
    # 如果根目录没有 .env，尝试加载环境变量（用于生产环境）
    load_dotenv(override=True)


class Config:
    """Flask配置类"""
    
    # Flask配置
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mirofish-secret-key')
    DEBUG = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    # JSON配置 - 禁用ASCII转义，让中文直接显示
    JSON_AS_ASCII = False
    
    # LLM配置（统一使用OpenAI格式）
    LLM_API_KEY = os.environ.get('LLM_API_KEY')
    LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1')
    LLM_MODEL_NAME = os.environ.get('LLM_MODEL_NAME', 'gpt-4o-mini')
    # 可选：多 provider profile / stage 路由 / 模拟 agent 模型池（解析见 app/llm/profiles.py）
    LLM_PROFILES = os.environ.get('LLM_PROFILES')
    LLM_ROUTES = os.environ.get('LLM_ROUTES')
    LLM_AGENT_POOL = os.environ.get('LLM_AGENT_POOL')
    
    # Zep配置
    ZEP_API_KEY = os.environ.get('ZEP_API_KEY')
    GRAPH_BACKEND = os.environ.get('GRAPH_BACKEND')
    GRAPH_LOCAL_DB = os.environ.get('GRAPH_LOCAL_DB')
    GRAPH_LOCAL_EMBEDDINGS = os.environ.get('GRAPH_LOCAL_EMBEDDINGS')
    
    # 文件上传配置
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}
    
    # 文本处理配置
    DEFAULT_CHUNK_SIZE = 500  # 默认切块大小
    DEFAULT_CHUNK_OVERLAP = 50  # 默认重叠大小
    
    # OASIS模拟配置
    OASIS_DEFAULT_MAX_ROUNDS = int(os.environ.get('OASIS_DEFAULT_MAX_ROUNDS', '10'))
    OASIS_SIMULATION_DATA_DIR = os.path.join(os.path.dirname(__file__), '../uploads/simulations')
    
    # OASIS平台可用动作配置
    OASIS_TWITTER_ACTIONS = [
        'CREATE_POST', 'LIKE_POST', 'REPOST', 'FOLLOW', 'DO_NOTHING', 'QUOTE_POST'
    ]
    OASIS_REDDIT_ACTIONS = [
        'LIKE_POST', 'DISLIKE_POST', 'CREATE_POST', 'CREATE_COMMENT',
        'LIKE_COMMENT', 'DISLIKE_COMMENT', 'SEARCH_POSTS', 'SEARCH_USER',
        'TREND', 'REFRESH', 'DO_NOTHING', 'FOLLOW', 'MUTE'
    ]
    
    # Report Agent配置
    REPORT_AGENT_MAX_TOOL_CALLS = int(os.environ.get('REPORT_AGENT_MAX_TOOL_CALLS', '5'))
    REPORT_AGENT_MAX_REFLECTION_ROUNDS = int(os.environ.get('REPORT_AGENT_MAX_REFLECTION_ROUNDS', '2'))
    REPORT_AGENT_TEMPERATURE = float(os.environ.get('REPORT_AGENT_TEMPERATURE', '0.5'))
    REPORT_TOOL_RESULT_MAX_CHARS = int(os.environ.get('REPORT_TOOL_RESULT_MAX_CHARS', '6000'))
    REPORT_MAX_PARALLEL_TOOLS = int(os.environ.get('REPORT_MAX_PARALLEL_TOOLS', '3'))
    REPORT_PREV_SECTIONS_MAX_CHARS = int(os.environ.get('REPORT_PREV_SECTIONS_MAX_CHARS', '12000'))
    REPORT_CHAT_HISTORY_MAX_CHARS = int(os.environ.get('REPORT_CHAT_HISTORY_MAX_CHARS', '8000'))
    
    @classmethod
    def validate(cls) -> list[str]:
        """验证必要配置"""
        errors: list[str] = []
        from .llm.profiles import load_settings
        from .utils.locale import t

        try:
            if not load_settings().profiles:
                errors.append(t('config.llm_api_key_missing'))
        except ValueError as error:
            errors.append(t('config.llm_config_invalid', error=error))
        backend = (os.environ.get("GRAPH_BACKEND") or cls.GRAPH_BACKEND or "").strip().lower()
        if not backend:
            backend = "zep_cloud" if (os.environ.get("ZEP_API_KEY") or cls.ZEP_API_KEY or "").strip() else "local"
        if backend not in {"local", "zep_cloud"}:
            errors.append(t("config.graph_backend_invalid", value=backend))
        if backend == "zep_cloud" and not (os.environ.get("ZEP_API_KEY") or cls.ZEP_API_KEY):
            errors.append(t('config.zep_api_key_missing'))
        if backend == "zep_cloud" and os.environ.get("ZEP_API_URL"):
            errors.append(t('config.zep_api_url_unsupported'))
        if cls.DEBUG:
            import warnings
            warnings.warn("Flask DEBUG mode is enabled. Do not use in production.", RuntimeWarning)
        return errors
