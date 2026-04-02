"""
配置管理
统一从项目根目录的 .env 文件加载配置
"""

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


def _env_first(*keys: str, default=None):
    """Return the first non-empty environment variable."""
    for key in keys:
        value = os.environ.get(key)
        if value is not None and value != "":
            return value
    return default


class Config:
    """Flask配置类"""
    
    # Flask配置
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mirofish-secret-key')
    DEBUG = os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'
    
    # JSON配置 - 禁用ASCII转义，让中文直接显示（而不是 \uXXXX 格式）
    JSON_AS_ASCII = False
    
    # LLM配置（统一使用OpenAI格式）
    # 兼容项目原有变量名和OpenAI常见变量名，便于直接切换到官方OpenAI API
    LLM_API_KEY = _env_first('LLM_API_KEY', 'OPENAI_API_KEY')
    LLM_BASE_URL = _env_first(
        'LLM_BASE_URL',
        'OPENAI_BASE_URL',
        'OPENAI_API_BASE',
        'OPENAI_API_BASE_URL',
        default='https://api.openai.com/v1',
    )
    LLM_MODEL_NAME = _env_first('LLM_MODEL_NAME', 'OPENAI_MODEL_NAME', default='gpt-4o-mini')
    LLM_PREMIUM_API_KEY = _env_first('LLM_PREMIUM_API_KEY', default=LLM_API_KEY)
    LLM_PREMIUM_BASE_URL = _env_first(
        'LLM_PREMIUM_BASE_URL',
        default=LLM_BASE_URL,
    )
    LLM_PREMIUM_MODEL_NAME = _env_first('LLM_PREMIUM_MODEL_NAME', default='')
    LLM_ROUTING_PROFILE = os.environ.get('LLM_ROUTING_PROFILE', 'balanced').strip().lower()
    
    # Neo4j配置（本地图数据库）
    NEO4J_URI = os.environ.get('NEO4J_URI', 'bolt://localhost:7687')
    NEO4J_USER = os.environ.get('NEO4J_USER', 'neo4j')
    NEO4J_PASSWORD = os.environ.get('NEO4J_PASSWORD')  # Required - no default for security
    
    # 文件上传配置
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}
    WEB_EVIDENCE_MAX_SOURCES = int(os.environ.get('WEB_EVIDENCE_MAX_SOURCES', '8'))
    WEB_EVIDENCE_FETCH_TIMEOUT = int(os.environ.get('WEB_EVIDENCE_FETCH_TIMEOUT', '20'))
    WEB_EVIDENCE_MAX_SOURCE_CHARS = int(os.environ.get('WEB_EVIDENCE_MAX_SOURCE_CHARS', '3000'))
    WEB_EVIDENCE_USER_AGENT = os.environ.get(
        'WEB_EVIDENCE_USER_AGENT',
        'MiroFishEvidenceBot/0.1 (+https://github.com/666ghj/MiroFish)'
    )
    
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

    # Timeout配置 - 防止网络请求无限挂起
    # LLM API超时（秒）
    LLM_TIMEOUT_CONNECT = int(os.environ.get('LLM_TIMEOUT_CONNECT', '30'))
    LLM_TIMEOUT_READ = int(os.environ.get('LLM_TIMEOUT_READ', '120'))
    LLM_TIMEOUT_WRITE = int(os.environ.get('LLM_TIMEOUT_WRITE', '30'))
    LLM_TIMEOUT_POOL = int(os.environ.get('LLM_TIMEOUT_POOL', '10'))
    LLM_MAX_RETRIES = int(os.environ.get('LLM_MAX_RETRIES', '3'))

    # OASIS模拟env.step()超时（秒）
    OASIS_ENV_STEP_TIMEOUT = int(os.environ.get('OASIS_ENV_STEP_TIMEOUT', '300'))
    OASIS_ENV_STEP_MAX_RETRIES = int(os.environ.get('OASIS_ENV_STEP_MAX_RETRIES', '3'))

    # Neo4j连接超时（秒）
    NEO4J_CONNECTION_TIMEOUT = int(os.environ.get('NEO4J_CONNECTION_TIMEOUT', '30'))
    NEO4J_MAX_CONNECTION_LIFETIME = int(os.environ.get('NEO4J_MAX_CONNECTION_LIFETIME', '3600'))
    NEO4J_CONNECTION_ACQUISITION_TIMEOUT = int(os.environ.get('NEO4J_CONNECTION_ACQUISITION_TIMEOUT', '60'))

    # Hugging Face下载超时（秒）
    HF_HUB_DOWNLOAD_TIMEOUT = int(os.environ.get('HF_HUB_DOWNLOAD_TIMEOUT', '120'))
    
    @classmethod
    def validate(cls):
        """验证必要配置"""
        errors = []
        if not cls.LLM_API_KEY:
            errors.append("LLM_API_KEY 未配置")
        if not cls.NEO4J_URI:
            errors.append("NEO4J_URI 未配置")
        if not cls.NEO4J_PASSWORD:
            errors.append("NEO4J_PASSWORD 未配置")
        return errors
