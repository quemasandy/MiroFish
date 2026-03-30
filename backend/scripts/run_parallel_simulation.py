"""
OASIS 双平台并行模拟预设脚本
同时运行Twitter和Reddit模拟，读取相同的配置文件

功能特性:
- 双平台（Twitter + Reddit）并行模拟
- 完成模拟后不立即关闭环境，进入等待命令模式
- 支持通过IPC接收Interview命令
- 支持单个Agent采访和批量采访
- 支持远程关闭环境命令

使用方式:
    python run_parallel_simulation.py --config simulation_config.json
    python run_parallel_simulation.py --config simulation_config.json --no-wait  # 完成后立即关闭
    python run_parallel_simulation.py --config simulation_config.json --twitter-only
    python run_parallel_simulation.py --config simulation_config.json --reddit-only

日志结构:
    sim_xxx/
    ├── twitter/
    │   └── actions.jsonl    # Twitter 平台动作日志
    ├── reddit/
    │   └── actions.jsonl    # Reddit 平台动作日志
    ├── simulation.log       # 主模拟进程日志
    └── run_state.json       # 运行状态（API 查询用）
"""

# ============================================================
# 解决 Windows 编码问题：在所有 import 之前设置 UTF-8 编码
# 这是为了修复 OASIS 第三方库读取文件时未指定编码的问题
# ============================================================
import sys
import os

# ====== FIX MAC MPS DEADLOCKS ======
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
# ===================================

# ====== TORCH/BLAS THREAD LIMITS (减少TwHIN推理卡死和内存峰值) ======
_DEFAULT_TORCH_THREADS = max(1, int(os.environ.get("OASIS_TORCH_THREADS", "2")))
for _thread_env in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_thread_env, str(_DEFAULT_TORCH_THREADS))

_DEFAULT_RECSYS_EMBED_BATCH_SIZE = max(
    1, int(os.environ.get("OASIS_RECSYS_EMBED_BATCH_SIZE", "32"))
)
_DEFAULT_TWHIN_POST_SAMPLE_SIZE = max(
    100, int(os.environ.get("OASIS_TWHIN_POST_SAMPLE_SIZE", "1000"))
)
# ====================================================================

# ====== HUGGINGFACE TIMEOUT (防止下载卡死) ======
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
# ===============================================

# ====== GLOBAL SOCKET TIMEOUT (防止网络请求无限挂起) ======
# 这是最后一道防线：设置全局socket超时
# 影响所有使用socket的库（包括transformers/huggingface/httpx等）
import socket
import logging

_DEFAULT_SOCKET_TIMEOUT = int(os.environ.get('SOCKET_TIMEOUT', '60'))  # 降低到60秒
socket.setdefaulttimeout(_DEFAULT_SOCKET_TIMEOUT)
logging.info(f"[CONFIG] Global socket timeout set to {_DEFAULT_SOCKET_TIMEOUT}s")

# 配置urllib3（requests/huggingface_hub底层库）的超时
import urllib3
urllib3.util.timeout.Timeout.DEFAULT_TIMEOUT = _DEFAULT_SOCKET_TIMEOUT

# 尝试patch requests库的默认超时
try:
    import requests
    from requests.adapters import HTTPAdapter
    # Monkey-patch默认超时
    _original_send = HTTPAdapter.send
    def _patched_send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if timeout is None:
            timeout = _DEFAULT_SOCKET_TIMEOUT
        return _original_send(self, request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)
    HTTPAdapter.send = _patched_send
    logging.info(f"[CONFIG] Patched requests.HTTPAdapter with {_DEFAULT_SOCKET_TIMEOUT}s timeout")
except Exception as e:
    logging.warning(f"[CONFIG] Could not patch requests: {e}")

# HuggingFace相关配置 - 优先使用本地缓存
os.environ.setdefault("HF_HUB_OFFLINE", "0")  # 允许在线但优先用缓存
os.environ.setdefault("TRANSFORMERS_OFFLINE", "0")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
# 设置transformers使用本地缓存优先
os.environ.setdefault("TRANSFORMERS_CACHE", os.path.expanduser("~/.cache/huggingface/hub"))
logging.info(f"[CONFIG] HuggingFace cache: {os.environ.get('TRANSFORMERS_CACHE', 'default')}")
# =========================================================

if sys.platform == 'win32':
    # 设置 Python 默认 I/O 编码为 UTF-8
    # 这会影响所有未指定编码的 open() 调用
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    
    # 重新配置标准输出流为 UTF-8（解决控制台中文乱码）
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    
    # 强制设置默认编码（影响 open() 函数的默认编码）
    # 注意：这需要在 Python 启动时就设置，运行时设置可能不生效
    # 所以我们还需要 monkey-patch 内置的 open 函数
    import builtins
    _original_open = builtins.open
    
    def _utf8_open(file, mode='r', buffering=-1, encoding=None, errors=None, 
                   newline=None, closefd=True, opener=None):
        """
        包装 open() 函数，对于文本模式默认使用 UTF-8 编码
        这可以修复第三方库（如 OASIS）读取文件时未指定编码的问题
        """
        # 只对文本模式（非二进制）且未指定编码的情况设置默认编码
        if encoding is None and 'b' not in mode:
            encoding = 'utf-8'
        return _original_open(file, mode, buffering, encoding, errors, 
                              newline, closefd, opener)
    
    builtins.open = _utf8_open

import argparse
import asyncio
import json
import logging
import multiprocessing
import random
import signal
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

# 模块级日志器 - 用于脚本运行时的日志记录
logger = logging.getLogger(__name__)


# 全局变量：用于信号处理
_shutdown_event = None
_cleanup_done = False

# 添加 backend 目录到路径
# 脚本固定位于 backend/scripts/ 目录
_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.abspath(os.path.join(_scripts_dir, '..'))
_project_root = os.path.abspath(os.path.join(_backend_dir, '..'))
sys.path.insert(0, _scripts_dir)
sys.path.insert(0, _backend_dir)

# 加载项目根目录的 .env 文件（包含 LLM_API_KEY 等配置）
from dotenv import load_dotenv
_env_file = os.path.join(_project_root, '.env')
if os.path.exists(_env_file):
    load_dotenv(_env_file)
    logging.info(f"已加载环境配置: {_env_file}")
else:
    # 尝试加载 backend/.env
    _backend_env = os.path.join(_backend_dir, '.env')
    if os.path.exists(_backend_env):
        load_dotenv(_backend_env)
        logging.info(f"已加载环境配置: {_backend_env}")


class MaxTokensWarningFilter(logging.Filter):
    """过滤掉 camel-ai 关于 max_tokens 的警告（我们故意不设置 max_tokens，让模型自行决定）"""
    
    def filter(self, record):
        # 过滤掉包含 max_tokens 警告的日志
        if "max_tokens" in record.getMessage() and "Invalid or missing" in record.getMessage():
            return False
        return True


# 在模块加载时立即添加过滤器，确保在 camel 代码执行前生效
logging.getLogger().addFilter(MaxTokensWarningFilter())


def disable_oasis_logging():
    """
    禁用 OASIS 库的详细日志输出
    OASIS 的日志太冗余（记录每个 agent 的观察和动作），我们使用自己的 action_logger
    """
    # 禁用 OASIS 的所有日志器
    oasis_loggers = [
        "social.agent",
        "social.twitter", 
        "social.rec",
        "oasis.env",
        "table",
    ]
    
    for logger_name in oasis_loggers:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.CRITICAL)  # 只记录严重错误
        logger.handlers.clear()
        logger.propagate = False


def init_logging_for_simulation(simulation_dir: str, clear_old_logs: bool = True):
    """
    初始化模拟的日志配置
    
    Args:
        simulation_dir: 模拟目录路径
        clear_old_logs: 是否清理旧的 OASIS 内部日志目录
    """
    # 禁用 OASIS 的详细日志
    disable_oasis_logging()
    
    # 清理旧的 log 目录（如果存在）
    old_log_dir = os.path.join(simulation_dir, "log")
    if clear_old_logs and os.path.exists(old_log_dir):
        import shutil
        shutil.rmtree(old_log_dir, ignore_errors=True)


from action_logger import SimulationLogManager, PlatformActionLogger

try:
    from camel.models import ModelFactory
    from camel.types import ModelPlatformType
    import oasis
    from oasis import (
        ActionType,
        LLMAction,
        ManualAction,
        generate_twitter_agent_graph,
        generate_reddit_agent_graph
    )
except ImportError as e:
    logging.error(f"错误: 缺少依赖 {e}")
    logging.error("请先安装: pip install oasis-ai camel-ai")
    sys.exit(1)


def patch_oasis_runtime_for_stability():
    """
    Reduce TwHIN recommendation spikes that can wedge the simulation process.

    Notes:
    - OASIS recalculates recommendation embeddings inside env.step().
    - The upstream implementation uses very large embedding batches.
    - On large simulations this can push torch/BLAS into long stalls.
    """
    try:
        import torch
        import oasis.social_platform.process_recsys_posts as oasis_recsys_posts
        import oasis.social_platform.recsys as oasis_recsys
        from oasis.social_platform.platform import Platform
        from oasis.social_platform.typing import RecsysType

        torch_threads = max(
            1, int(os.environ.get("OASIS_TORCH_THREADS", str(_DEFAULT_TORCH_THREADS)))
        )
        torch_interop_threads = max(
            1, int(os.environ.get("OASIS_TORCH_INTEROP_THREADS", "1"))
        )
        embed_batch_size = max(
            1,
            int(
                os.environ.get(
                    "OASIS_RECSYS_EMBED_BATCH_SIZE",
                    str(_DEFAULT_RECSYS_EMBED_BATCH_SIZE),
                )
            ),
        )
        twhin_post_sample_size = max(
            100,
            int(
                os.environ.get(
                    "OASIS_TWHIN_POST_SAMPLE_SIZE",
                    str(_DEFAULT_TWHIN_POST_SAMPLE_SIZE),
                )
            ),
        )

        try:
            torch.set_num_threads(torch_threads)
        except RuntimeError:
            pass

        try:
            torch.set_num_interop_threads(torch_interop_threads)
        except RuntimeError:
            pass

        if getattr(oasis_recsys, "_mirofish_stability_patched", False):
            return

        original_generate_post_vector = oasis_recsys_posts.generate_post_vector
        original_generate_post_vector_openai = (
            oasis_recsys_posts.generate_post_vector_openai
        )
        original_coarse_filtering = oasis_recsys.coarse_filtering
        original_update_rec_table = Platform.update_rec_table

        def _safe_generate_post_vector(model, tokenizer, texts, batch_size):
            effective_batch_size = min(batch_size, embed_batch_size)
            return original_generate_post_vector(
                model, tokenizer, texts, effective_batch_size
            )

        def _safe_generate_post_vector_openai(texts, batch_size=100):
            effective_batch_size = min(batch_size, embed_batch_size)
            return original_generate_post_vector_openai(
                texts, batch_size=effective_batch_size
            )

        def _safe_coarse_filtering(input_list, scale):
            effective_scale = min(scale, twhin_post_sample_size)
            return original_coarse_filtering(input_list, effective_scale)

        async def _stable_update_rec_table(self):
            if self.recsys_type == RecsysType.TWHIN:
                post_count = self.db.execute("SELECT COUNT(*) FROM post").fetchone()[0]
                last_post_count = getattr(self, "_mirofish_last_rec_post_count", None)
                if last_post_count == post_count:
                    logger.info(
                        "[RECSYS] Skip TwHIN refresh because post count is unchanged: %s",
                        post_count,
                    )
                    return
                self._mirofish_last_rec_post_count = post_count
            return await original_update_rec_table(self)

        oasis_recsys_posts.generate_post_vector = _safe_generate_post_vector
        oasis_recsys.generate_post_vector = _safe_generate_post_vector
        oasis_recsys_posts.generate_post_vector_openai = (
            _safe_generate_post_vector_openai
        )
        oasis_recsys.generate_post_vector_openai = _safe_generate_post_vector_openai
        oasis_recsys.coarse_filtering = _safe_coarse_filtering
        Platform.update_rec_table = _stable_update_rec_table
        oasis_recsys._mirofish_stability_patched = True

        logger.info(
            "[CONFIG] OASIS stability patch enabled: torch_threads=%s, "
            "torch_interop_threads=%s, recsys_embed_batch=%s, "
            "twhin_post_sample_size=%s",
            torch_threads,
            torch_interop_threads,
            embed_batch_size,
            twhin_post_sample_size,
        )
    except Exception as e:
        logger.warning(f"[CONFIG] Failed to patch OASIS runtime stability: {e}")


patch_oasis_runtime_for_stability()


# Twitter可用动作（不包含INTERVIEW，INTERVIEW只能通过ManualAction手动触发）
TWITTER_ACTIONS = [
    ActionType.CREATE_POST,
    ActionType.LIKE_POST,
    ActionType.REPOST,
    ActionType.FOLLOW,
    ActionType.DO_NOTHING,
    ActionType.QUOTE_POST,
]

# Reddit可用动作（不包含INTERVIEW，INTERVIEW只能通过ManualAction手动触发）
REDDIT_ACTIONS = [
    ActionType.LIKE_POST,
    ActionType.DISLIKE_POST,
    ActionType.CREATE_POST,
    ActionType.CREATE_COMMENT,
    ActionType.LIKE_COMMENT,
    ActionType.DISLIKE_COMMENT,
    ActionType.SEARCH_POSTS,
    ActionType.SEARCH_USER,
    ActionType.TREND,
    ActionType.REFRESH,
    ActionType.DO_NOTHING,
    ActionType.FOLLOW,
    ActionType.MUTE,
]


# ====== ENV.STEP() TIMEOUT WRAPPER ======
# 防止OASIS模拟在网络问题时无限挂起

# 默认超时配置（可通过环境变量覆盖）
ENV_STEP_TIMEOUT = int(os.environ.get('OASIS_ENV_STEP_TIMEOUT', '300'))
ENV_STEP_MAX_RETRIES = int(os.environ.get('OASIS_ENV_STEP_MAX_RETRIES', '1'))


async def env_step_with_timeout(
    env,
    actions,
    timeout: int = ENV_STEP_TIMEOUT,
    max_retries: int = ENV_STEP_MAX_RETRIES,
    step_description: str = "env.step()"
):
    """
    带超时的 env.step() 包装器

    防止以下情况导致的无限挂起：
    - BERT/Hugging Face模型下载卡住（CloudFront问题）
    - LLM API无响应
    - Neo4j连接断开

    注意：
    env.step() 是有状态操作。超时后重试同一个 step 可能造成重复执行、
    日志错乱或环境状态不一致。因此这里默认不重试；如果调用方传入了
    max_retries > 1，也会在首次超时后直接失败。

    Args:
        env: OASIS环境对象
        actions: 要执行的动作字典
        timeout: 超时秒数（默认300秒=5分钟）
        max_retries: 保留兼容参数；超时后不会安全重试
        step_description: 日志描述（用于调试）

    Returns:
        env.step()的返回值

    Raises:
        TimeoutError: step 超时
    """
    try:
        step_result = env.step(actions)

        if asyncio.iscoroutine(step_result):
            return await asyncio.wait_for(step_result, timeout=timeout)
        return step_result
    except asyncio.TimeoutError as e:
        logger.warning(
            "[TIMEOUT] %s 超时, timeout=%ss. "
            "已停止重试以避免对同一 env.step 重入。",
            step_description,
            timeout,
        )
        if max_retries > 1:
            logger.warning(
                "[TIMEOUT] 已忽略 max_retries=%s，因为 env.step 超时后重试不安全。",
                max_retries,
            )
        raise TimeoutError(f"{step_description} 超时 (timeout={timeout}s)") from e
    except asyncio.CancelledError:
        logger.warning(f"[CANCELLED] {step_description} 被取消")
        raise
    except Exception as e:
        logger.error(f"[ERROR] {step_description} 发生异常: {e}")
        raise

# ==========================================


# IPC相关常量
IPC_COMMANDS_DIR = "ipc_commands"
IPC_RESPONSES_DIR = "ipc_responses"
ENV_STATUS_FILE = "env_status.json"

class CommandType:
    """命令类型常量"""
    INTERVIEW = "interview"
    BATCH_INTERVIEW = "batch_interview"
    CLOSE_ENV = "close_env"


class ParallelIPCHandler:
    """
    双平台IPC命令处理器
    
    管理两个平台的环境，处理Interview命令
    """
    
    def __init__(
        self,
        simulation_dir: str,
        twitter_env=None,
        twitter_agent_graph=None,
        reddit_env=None,
        reddit_agent_graph=None
    ):
        self.simulation_dir = simulation_dir
        self.twitter_env = twitter_env
        self.twitter_agent_graph = twitter_agent_graph
        self.reddit_env = reddit_env
        self.reddit_agent_graph = reddit_agent_graph
        
        self.commands_dir = os.path.join(simulation_dir, IPC_COMMANDS_DIR)
        self.responses_dir = os.path.join(simulation_dir, IPC_RESPONSES_DIR)
        self.status_file = os.path.join(simulation_dir, ENV_STATUS_FILE)
        
        # 确保目录存在
        os.makedirs(self.commands_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)
    
    def update_status(self, status: str):
        """更新环境状态"""
        with open(self.status_file, 'w', encoding='utf-8') as f:
            json.dump({
                "status": status,
                "twitter_available": self.twitter_env is not None,
                "reddit_available": self.reddit_env is not None,
                "timestamp": datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)
    
    def poll_command(self) -> Optional[Dict[str, Any]]:
        """轮询获取待处理命令"""
        if not os.path.exists(self.commands_dir):
            return None
        
        # 获取命令文件（按时间排序）
        command_files = []
        for filename in os.listdir(self.commands_dir):
            if filename.endswith('.json'):
                filepath = os.path.join(self.commands_dir, filename)
                command_files.append((filepath, os.path.getmtime(filepath)))
        
        command_files.sort(key=lambda x: x[1])
        
        for filepath, _ in command_files:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
        
        return None
    
    def send_response(self, command_id: str, status: str, result: Dict = None, error: str = None):
        """发送响应"""
        response = {
            "command_id": command_id,
            "status": status,
            "result": result,
            "error": error,
            "timestamp": datetime.now().isoformat()
        }
        
        response_file = os.path.join(self.responses_dir, f"{command_id}.json")
        tmp_file = response_file + ".tmp"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(response, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, response_file)
        
        # 删除命令文件
        command_file = os.path.join(self.commands_dir, f"{command_id}.json")
        try:
            os.remove(command_file)
        except OSError:
            pass
    
    def _get_env_and_graph(self, platform: str):
        """
        获取指定平台的环境和agent_graph
        
        Args:
            platform: 平台名称 ("twitter" 或 "reddit")
            
        Returns:
            (env, agent_graph, platform_name) 或 (None, None, None)
        """
        if platform == "twitter" and self.twitter_env:
            return self.twitter_env, self.twitter_agent_graph, "twitter"
        elif platform == "reddit" and self.reddit_env:
            return self.reddit_env, self.reddit_agent_graph, "reddit"
        else:
            return None, None, None
    
    async def _interview_single_platform(self, agent_id: int, prompt: str, platform: str) -> Dict[str, Any]:
        """
        在单个平台上执行Interview
        
        Returns:
            包含结果的字典，或包含error的字典
        """
        env, agent_graph, actual_platform = self._get_env_and_graph(platform)
        
        if not env or not agent_graph:
            return {"platform": platform, "error": f"{platform}平台不可用"}
        
        try:
            agent = agent_graph.get_agent(agent_id)
            interview_action = ManualAction(
                action_type=ActionType.INTERVIEW,
                action_args={"prompt": prompt}
            )
            actions = {agent: interview_action}
            await env_step_with_timeout(
                env, actions,
                step_description=f"Interview agent_id={agent_id} on {platform}"
            )
            
            result = self._get_interview_result(agent_id, actual_platform)
            result["platform"] = actual_platform
            return result
            
        except Exception as e:
            return {"platform": platform, "error": str(e)}
    
    async def handle_interview(self, command_id: str, agent_id: int, prompt: str, platform: str = None) -> bool:
        """
        处理单个Agent采访命令
        
        Args:
            command_id: 命令ID
            agent_id: Agent ID
            prompt: 采访问题
            platform: 指定平台（可选）
                - "twitter": 只采访Twitter平台
                - "reddit": 只采访Reddit平台
                - None/不指定: 同时采访两个平台，返回整合结果
            
        Returns:
            True 表示成功，False 表示失败
        """
        # 如果指定了平台，只采访该平台
        if platform in ("twitter", "reddit"):
            result = await self._interview_single_platform(agent_id, prompt, platform)
            
            if "error" in result:
                self.send_response(command_id, "failed", error=result["error"])
                logger.warning(f"  Interview失败: agent_id={agent_id}, platform={platform}, error={result['error']}")
                return False
            else:
                self.send_response(command_id, "completed", result=result)
                logger.info(f"  Interview完成: agent_id={agent_id}, platform={platform}")
                return True
        
        # 未指定平台：同时采访两个平台
        if not self.twitter_env and not self.reddit_env:
            self.send_response(command_id, "failed", error="没有可用的模拟环境")
            return False
        
        results = {
            "agent_id": agent_id,
            "prompt": prompt,
            "platforms": {}
        }
        success_count = 0
        
        # 并行采访两个平台
        tasks = []
        platforms_to_interview = []
        
        if self.twitter_env:
            tasks.append(self._interview_single_platform(agent_id, prompt, "twitter"))
            platforms_to_interview.append("twitter")
        
        if self.reddit_env:
            tasks.append(self._interview_single_platform(agent_id, prompt, "reddit"))
            platforms_to_interview.append("reddit")
        
        # 并行执行
        platform_results = await asyncio.gather(*tasks)
        
        for platform_name, platform_result in zip(platforms_to_interview, platform_results):
            results["platforms"][platform_name] = platform_result
            if "error" not in platform_result:
                success_count += 1
        
        if success_count > 0:
            self.send_response(command_id, "completed", result=results)
            logger.info(f"  Interview完成: agent_id={agent_id}, 成功平台数={success_count}/{len(platforms_to_interview)}")
            return True
        else:
            errors = [f"{p}: {r.get('error', '未知错误')}" for p, r in results["platforms"].items()]
            self.send_response(command_id, "failed", error="; ".join(errors))
            logger.warning(f"  Interview失败: agent_id={agent_id}, 所有平台都失败")
            return False
    
    async def handle_batch_interview(self, command_id: str, interviews: List[Dict], platform: str = None) -> bool:
        """
        处理批量采访命令
        
        Args:
            command_id: 命令ID
            interviews: [{"agent_id": int, "prompt": str, "platform": str(optional)}, ...]
            platform: 默认平台（可被每个interview项覆盖）
                - "twitter": 只采访Twitter平台
                - "reddit": 只采访Reddit平台
                - None/不指定: 每个Agent同时采访两个平台
        """
        # 按平台分组
        twitter_interviews = []
        reddit_interviews = []
        both_platforms_interviews = []  # 需要同时采访两个平台的
        
        for interview in interviews:
            item_platform = interview.get("platform", platform)
            if item_platform == "twitter":
                twitter_interviews.append(interview)
            elif item_platform == "reddit":
                reddit_interviews.append(interview)
            else:
                # 未指定平台：两个平台都采访
                both_platforms_interviews.append(interview)
        
        # 把 both_platforms_interviews 拆分到两个平台
        if both_platforms_interviews:
            if self.twitter_env:
                twitter_interviews.extend(both_platforms_interviews)
            if self.reddit_env:
                reddit_interviews.extend(both_platforms_interviews)
        
        results = {}
        
        # 处理Twitter平台的采访
        if twitter_interviews and self.twitter_env:
            try:
                twitter_actions = {}
                for interview in twitter_interviews:
                    agent_id = interview.get("agent_id")
                    prompt = interview.get("prompt", "")
                    try:
                        agent = self.twitter_agent_graph.get_agent(agent_id)
                        twitter_actions[agent] = ManualAction(
                            action_type=ActionType.INTERVIEW,
                            action_args={"prompt": prompt}
                        )
                    except Exception as e:
                        logger.warning(f"  警告: 无法获取Twitter Agent {agent_id}: {e}")
                
                if twitter_actions:
                    await env_step_with_timeout(
                        self.twitter_env, twitter_actions,
                        step_description=f"Twitter batch interview ({len(twitter_actions)} agents)"
                    )
                    
                    for interview in twitter_interviews:
                        agent_id = interview.get("agent_id")
                        result = self._get_interview_result(agent_id, "twitter")
                        result["platform"] = "twitter"
                        results[f"twitter_{agent_id}"] = result
            except Exception as e:
                logger.error(f"  Twitter批量Interview失败: {e}")
        
        # 处理Reddit平台的采访
        if reddit_interviews and self.reddit_env:
            try:
                reddit_actions = {}
                for interview in reddit_interviews:
                    agent_id = interview.get("agent_id")
                    prompt = interview.get("prompt", "")
                    try:
                        agent = self.reddit_agent_graph.get_agent(agent_id)
                        reddit_actions[agent] = ManualAction(
                            action_type=ActionType.INTERVIEW,
                            action_args={"prompt": prompt}
                        )
                    except Exception as e:
                        logger.warning(f"  警告: 无法获取Reddit Agent {agent_id}: {e}")
                
                if reddit_actions:
                    await env_step_with_timeout(
                        self.reddit_env, reddit_actions,
                        step_description=f"Reddit batch interview ({len(reddit_actions)} agents)"
                    )
                    
                    for interview in reddit_interviews:
                        agent_id = interview.get("agent_id")
                        result = self._get_interview_result(agent_id, "reddit")
                        result["platform"] = "reddit"
                        results[f"reddit_{agent_id}"] = result
            except Exception as e:
                logger.error(f"  Reddit批量Interview失败: {e}")
        
        if results:
            self.send_response(command_id, "completed", result={
                "interviews_count": len(results),
                "results": results
            })
            logger.info(f"  批量Interview完成: {len(results)} 个Agent")
            return True
        else:
            self.send_response(command_id, "failed", error="没有成功的采访")
            return False
    
    def _get_interview_result(self, agent_id: int, platform: str) -> Dict[str, Any]:
        """从数据库获取最新的Interview结果"""
        db_path = os.path.join(self.simulation_dir, f"{platform}_simulation.db")
        
        result = {
            "agent_id": agent_id,
            "response": None,
            "timestamp": None
        }
        
        if not os.path.exists(db_path):
            return result
        
        try:
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()

                cursor.execute("""
                    SELECT user_id, info, created_at
                    FROM trace
                    WHERE action = ? AND user_id = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (ActionType.INTERVIEW.value, agent_id))

                row = cursor.fetchone()
                if row:
                    user_id, info_json, created_at = row
                    try:
                        info = json.loads(info_json) if info_json else {}
                        result["response"] = info.get("response", info)
                        result["timestamp"] = created_at
                    except json.JSONDecodeError:
                        result["response"] = info_json
            finally:
                conn.close()

        except Exception as e:
            logger.warning(f"  读取Interview结果失败: {e}")
        
        return result
    
    async def process_commands(self) -> bool:
        """
        处理所有待处理命令
        
        Returns:
            True 表示继续运行，False 表示应该退出
        """
        command = self.poll_command()
        if not command:
            return True
        
        command_id = command.get("command_id")
        command_type = command.get("command_type")
        args = command.get("args", {})
        
        logger.info(f"收到IPC命令: {command_type}, id={command_id}")
        
        if command_type == CommandType.INTERVIEW:
            await self.handle_interview(
                command_id,
                args.get("agent_id", 0),
                args.get("prompt", ""),
                args.get("platform")
            )
            return True
            
        elif command_type == CommandType.BATCH_INTERVIEW:
            await self.handle_batch_interview(
                command_id,
                args.get("interviews", []),
                args.get("platform")
            )
            return True
            
        elif command_type == CommandType.CLOSE_ENV:
            logger.info("收到关闭环境命令")
            self.send_response(command_id, "completed", result={"message": "环境即将关闭"})
            return False
        
        else:
            self.send_response(command_id, "failed", error=f"未知命令类型: {command_type}")
            return True


def load_config(config_path: str) -> Dict[str, Any]:
    """加载配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


# 需要过滤掉的非核心动作类型（这些动作对分析价值较低）
FILTERED_ACTIONS = {'refresh', 'sign_up'}

# 动作类型映射表（数据库中的名称 -> 标准名称）
ACTION_TYPE_MAP = {
    'create_post': 'CREATE_POST',
    'like_post': 'LIKE_POST',
    'dislike_post': 'DISLIKE_POST',
    'repost': 'REPOST',
    'quote_post': 'QUOTE_POST',
    'follow': 'FOLLOW',
    'mute': 'MUTE',
    'create_comment': 'CREATE_COMMENT',
    'like_comment': 'LIKE_COMMENT',
    'dislike_comment': 'DISLIKE_COMMENT',
    'search_posts': 'SEARCH_POSTS',
    'search_user': 'SEARCH_USER',
    'trend': 'TREND',
    'do_nothing': 'DO_NOTHING',
    'interview': 'INTERVIEW',
}


def get_agent_names_from_config(config: Dict[str, Any]) -> Dict[int, str]:
    """
    从 simulation_config 中获取 agent_id -> entity_name 的映射
    
    这样可以在 actions.jsonl 中显示真实的实体名称，而不是 "Agent_0" 这样的代号
    
    Args:
        config: simulation_config.json 的内容
        
    Returns:
        agent_id -> entity_name 的映射字典
    """
    agent_names = {}
    agent_configs = config.get("agent_configs", [])
    
    for agent_config in agent_configs:
        agent_id = agent_config.get("agent_id")
        entity_name = agent_config.get("entity_name", f"Agent_{agent_id}")
        if agent_id is not None:
            agent_names[agent_id] = entity_name
    
    return agent_names


def fetch_new_actions_from_db(
    db_path: str,
    last_rowid: int,
    agent_names: Dict[int, str]
) -> Tuple[List[Dict[str, Any]], int]:
    """
    从数据库中获取新的动作记录，并补充完整的上下文信息
    
    Args:
        db_path: 数据库文件路径
        last_rowid: 上次读取的最大 rowid 值（使用 rowid 而不是 created_at，因为不同平台的 created_at 格式不同）
        agent_names: agent_id -> agent_name 映射
        
    Returns:
        (actions_list, new_last_rowid)
        - actions_list: 动作列表，每个元素包含 agent_id, agent_name, action_type, action_args（含上下文信息）
        - new_last_rowid: 新的最大 rowid 值
    """
    actions = []
    new_last_rowid = last_rowid
    
    if not os.path.exists(db_path):
        return actions, new_last_rowid
    
    try:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT rowid, user_id, action, info
                FROM trace
                WHERE rowid > ?
                ORDER BY rowid ASC
            """, (last_rowid,))

            for rowid, user_id, action, info_json in cursor.fetchall():
                new_last_rowid = rowid

                if action in FILTERED_ACTIONS:
                    continue

                try:
                    action_args = json.loads(info_json) if info_json else {}
                except json.JSONDecodeError:
                    action_args = {}

                simplified_args = {}
                if 'content' in action_args:
                    simplified_args['content'] = action_args['content']
                if 'post_id' in action_args:
                    simplified_args['post_id'] = action_args['post_id']
                if 'comment_id' in action_args:
                    simplified_args['comment_id'] = action_args['comment_id']
                if 'quoted_id' in action_args:
                    simplified_args['quoted_id'] = action_args['quoted_id']
                if 'new_post_id' in action_args:
                    simplified_args['new_post_id'] = action_args['new_post_id']
                if 'follow_id' in action_args:
                    simplified_args['follow_id'] = action_args['follow_id']
                if 'query' in action_args:
                    simplified_args['query'] = action_args['query']
                if 'like_id' in action_args:
                    simplified_args['like_id'] = action_args['like_id']
                if 'dislike_id' in action_args:
                    simplified_args['dislike_id'] = action_args['dislike_id']

                action_type = ACTION_TYPE_MAP.get(action, action.upper())

                _enrich_action_context(cursor, action_type, simplified_args, agent_names)

                actions.append({
                    'agent_id': user_id,
                    'agent_name': agent_names.get(user_id, f'Agent_{user_id}'),
                    'action_type': action_type,
                    'action_args': simplified_args,
                })
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"读取数据库动作失败: {e}")
    
    return actions, new_last_rowid


def _enrich_action_context(
    cursor,
    action_type: str,
    action_args: Dict[str, Any],
    agent_names: Dict[int, str]
) -> None:
    """
    为动作补充上下文信息（帖子内容、用户名等）
    
    Args:
        cursor: 数据库游标
        action_type: 动作类型
        action_args: 动作参数（会被修改）
        agent_names: agent_id -> agent_name 映射
    """
    try:
        # 点赞/踩帖子：补充帖子内容和作者
        if action_type in ('LIKE_POST', 'DISLIKE_POST'):
            post_id = action_args.get('post_id')
            if post_id:
                post_info = _get_post_info(cursor, post_id, agent_names)
                if post_info:
                    action_args['post_content'] = post_info.get('content', '')
                    action_args['post_author_name'] = post_info.get('author_name', '')
        
        # 转发帖子：补充原帖内容和作者
        elif action_type == 'REPOST':
            new_post_id = action_args.get('new_post_id')
            if new_post_id:
                # 转发帖子的 original_post_id 指向原帖
                cursor.execute("""
                    SELECT original_post_id FROM post WHERE post_id = ?
                """, (new_post_id,))
                row = cursor.fetchone()
                if row and row[0]:
                    original_post_id = row[0]
                    original_info = _get_post_info(cursor, original_post_id, agent_names)
                    if original_info:
                        action_args['original_content'] = original_info.get('content', '')
                        action_args['original_author_name'] = original_info.get('author_name', '')
        
        # 引用帖子：补充原帖内容、作者和引用评论
        elif action_type == 'QUOTE_POST':
            quoted_id = action_args.get('quoted_id')
            new_post_id = action_args.get('new_post_id')
            
            if quoted_id:
                original_info = _get_post_info(cursor, quoted_id, agent_names)
                if original_info:
                    action_args['original_content'] = original_info.get('content', '')
                    action_args['original_author_name'] = original_info.get('author_name', '')
            
            # 获取引用帖子的评论内容（quote_content）
            if new_post_id:
                cursor.execute("""
                    SELECT quote_content FROM post WHERE post_id = ?
                """, (new_post_id,))
                row = cursor.fetchone()
                if row and row[0]:
                    action_args['quote_content'] = row[0]
        
        # 关注用户：补充被关注用户的名称
        elif action_type == 'FOLLOW':
            follow_id = action_args.get('follow_id')
            if follow_id:
                # 从 follow 表获取 followee_id
                cursor.execute("""
                    SELECT followee_id FROM follow WHERE follow_id = ?
                """, (follow_id,))
                row = cursor.fetchone()
                if row:
                    followee_id = row[0]
                    target_name = _get_user_name(cursor, followee_id, agent_names)
                    if target_name:
                        action_args['target_user_name'] = target_name
        
        # 屏蔽用户：补充被屏蔽用户的名称
        elif action_type == 'MUTE':
            # 从 action_args 中获取 user_id 或 target_id
            target_id = action_args.get('user_id') or action_args.get('target_id')
            if target_id:
                target_name = _get_user_name(cursor, target_id, agent_names)
                if target_name:
                    action_args['target_user_name'] = target_name
        
        # 点赞/踩评论：补充评论内容和作者
        elif action_type in ('LIKE_COMMENT', 'DISLIKE_COMMENT'):
            comment_id = action_args.get('comment_id')
            if comment_id:
                comment_info = _get_comment_info(cursor, comment_id, agent_names)
                if comment_info:
                    action_args['comment_content'] = comment_info.get('content', '')
                    action_args['comment_author_name'] = comment_info.get('author_name', '')
        
        # 发表评论：补充所评论的帖子信息
        elif action_type == 'CREATE_COMMENT':
            post_id = action_args.get('post_id')
            if post_id:
                post_info = _get_post_info(cursor, post_id, agent_names)
                if post_info:
                    action_args['post_content'] = post_info.get('content', '')
                    action_args['post_author_name'] = post_info.get('author_name', '')
    
    except Exception as e:
        logger.warning(f"补充动作上下文失败 (action={action_type}): {e}")


def _get_post_info(
    cursor,
    post_id: int,
    agent_names: Dict[int, str]
) -> Optional[Dict[str, str]]:
    """
    获取帖子信息
    
    Args:
        cursor: 数据库游标
        post_id: 帖子ID
        agent_names: agent_id -> agent_name 映射
        
    Returns:
        包含 content 和 author_name 的字典，或 None
    """
    try:
        cursor.execute("""
            SELECT p.content, p.user_id, u.agent_id
            FROM post p
            LEFT JOIN user u ON p.user_id = u.user_id
            WHERE p.post_id = ?
        """, (post_id,))
        row = cursor.fetchone()
        if row:
            content = row[0] or ''
            user_id = row[1]
            agent_id = row[2]
            
            # 优先使用 agent_names 中的名称
            author_name = ''
            if agent_id is not None and agent_id in agent_names:
                author_name = agent_names[agent_id]
            elif user_id:
                # 从 user 表获取名称
                cursor.execute("SELECT name, user_name FROM user WHERE user_id = ?", (user_id,))
                user_row = cursor.fetchone()
                if user_row:
                    author_name = user_row[0] or user_row[1] or ''
            
            return {'content': content, 'author_name': author_name}
    except Exception as e:
        logger.debug(f"获取帖子信息失败 (post_id={post_id}): {e}")
    return None


def _get_user_name(
    cursor,
    user_id: int,
    agent_names: Dict[int, str]
) -> Optional[str]:
    """
    获取用户名称
    
    Args:
        cursor: 数据库游标
        user_id: 用户ID
        agent_names: agent_id -> agent_name 映射
        
    Returns:
        用户名称，或 None
    """
    try:
        cursor.execute("""
            SELECT agent_id, name, user_name FROM user WHERE user_id = ?
        """, (user_id,))
        row = cursor.fetchone()
        if row:
            agent_id = row[0]
            name = row[1]
            user_name = row[2]
            
            # 优先使用 agent_names 中的名称
            if agent_id is not None and agent_id in agent_names:
                return agent_names[agent_id]
            return name or user_name or ''
    except Exception as e:
        logger.debug(f"获取用户名称失败 (user_id={user_id}): {e}")
    return None


def _get_comment_info(
    cursor,
    comment_id: int,
    agent_names: Dict[int, str]
) -> Optional[Dict[str, str]]:
    """
    获取评论信息
    
    Args:
        cursor: 数据库游标
        comment_id: 评论ID
        agent_names: agent_id -> agent_name 映射
        
    Returns:
        包含 content 和 author_name 的字典，或 None
    """
    try:
        cursor.execute("""
            SELECT c.content, c.user_id, u.agent_id
            FROM comment c
            LEFT JOIN user u ON c.user_id = u.user_id
            WHERE c.comment_id = ?
        """, (comment_id,))
        row = cursor.fetchone()
        if row:
            content = row[0] or ''
            user_id = row[1]
            agent_id = row[2]
            
            # 优先使用 agent_names 中的名称
            author_name = ''
            if agent_id is not None and agent_id in agent_names:
                author_name = agent_names[agent_id]
            elif user_id:
                # 从 user 表获取名称
                cursor.execute("SELECT name, user_name FROM user WHERE user_id = ?", (user_id,))
                user_row = cursor.fetchone()
                if user_row:
                    author_name = user_row[0] or user_row[1] or ''
            
            return {'content': content, 'author_name': author_name}
    except Exception as e:
        logger.debug(f"获取评论信息失败 (comment_id={comment_id}): {e}")
    return None


def create_model(config: Dict[str, Any], use_boost: bool = False):
    """
    创建LLM模型
    
    支持双 LLM 配置，用于并行模拟时提速：
    - 通用配置：LLM_API_KEY, LLM_BASE_URL, LLM_MODEL_NAME
    - 加速配置（可选）：LLM_BOOST_API_KEY, LLM_BOOST_BASE_URL, LLM_BOOST_MODEL_NAME
    
    如果配置了加速 LLM，并行模拟时可以让不同平台使用不同的 API 服务商，提高并发能力。
    
    Args:
        config: 模拟配置字典
        use_boost: 是否使用加速 LLM 配置（如果可用）
    """
    # 检查是否有加速配置
    boost_api_key = os.environ.get("LLM_BOOST_API_KEY", "")
    boost_base_url = os.environ.get("LLM_BOOST_BASE_URL", "")
    boost_model = os.environ.get("LLM_BOOST_MODEL_NAME", "")
    has_boost_config = bool(boost_api_key)
    
    # 根据参数和配置情况选择使用哪个 LLM
    if use_boost and has_boost_config:
        # 使用加速配置
        llm_api_key = boost_api_key
        llm_base_url = boost_base_url
        llm_model = boost_model or os.environ.get("LLM_MODEL_NAME", "")
        config_label = "[加速LLM]"
    else:
        # 使用通用配置
        llm_api_key = os.environ.get("LLM_API_KEY", "")
        llm_base_url = os.environ.get("LLM_BASE_URL", "")
        llm_model = os.environ.get("LLM_MODEL_NAME", "")
        config_label = "[通用LLM]"
    
    # 如果 .env 中没有模型名，则使用 config 作为备用
    if not llm_model:
        llm_model = config.get("llm_model", "gpt-4o-mini")
    
    # 设置 camel-ai 所需的环境变量
    if llm_api_key:
        os.environ["OPENAI_API_KEY"] = llm_api_key
    
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("缺少 API Key 配置，请在项目根目录 .env 文件中设置 LLM_API_KEY")
    
    if llm_base_url:
        os.environ["OPENAI_API_BASE_URL"] = llm_base_url
    
    logger.info(f"{config_label} model={llm_model}, base_url={llm_base_url[:40] if llm_base_url else '默认'}...")
    
    return ModelFactory.create(
        model_platform=ModelPlatformType.OPENAI,
        model_type=llm_model,
    )


def get_active_agents_for_round(
    env,
    config: Dict[str, Any],
    current_hour: int,
    round_num: int
) -> List:
    """根据时间和配置决定本轮激活哪些Agent"""
    time_config = config.get("time_config", {})
    agent_configs = config.get("agent_configs", [])
    
    base_min = time_config.get("agents_per_hour_min", 5)
    base_max = time_config.get("agents_per_hour_max", 20)
    
    peak_hours = time_config.get("peak_hours", [9, 10, 11, 14, 15, 20, 21, 22])
    off_peak_hours = time_config.get("off_peak_hours", [0, 1, 2, 3, 4, 5])
    
    if current_hour in peak_hours:
        multiplier = time_config.get("peak_activity_multiplier", 1.5)
    elif current_hour in off_peak_hours:
        multiplier = time_config.get("off_peak_activity_multiplier", 0.3)
    else:
        multiplier = 1.0
    
    target_count = int(random.uniform(base_min, base_max) * multiplier)
    
    candidates = []
    for cfg in agent_configs:
        agent_id = cfg.get("agent_id", 0)
        active_hours = cfg.get("active_hours", list(range(8, 23)))
        activity_level = cfg.get("activity_level", 0.5)
        
        if current_hour not in active_hours:
            continue
        
        if random.random() < activity_level:
            candidates.append(agent_id)
    
    selected_ids = random.sample(
        candidates, 
        min(target_count, len(candidates))
    ) if candidates else []
    
    active_agents = []
    for agent_id in selected_ids:
        try:
            agent = env.agent_graph.get_agent(agent_id)
            active_agents.append((agent_id, agent))
        except Exception as e:
            logger.debug(f"无法获取 Agent (agent_id={agent_id}): {e}")

    return active_agents


@dataclass
class ReplayActionSpec:
    agent_id: int
    action_type: ActionType
    action_args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplayRoundSpec:
    round_num: int
    actions: List[ReplayActionSpec] = field(default_factory=list)


@dataclass
class PlatformResumePlan:
    platform: str
    artifacts_found: bool = False
    has_checkpoint: bool = False
    last_completed_round: int = 0
    total_actions: int = 0
    rounds: List[ReplayRoundSpec] = field(default_factory=list)


def _get_effective_total_rounds(
    config: Dict[str, Any],
    max_rounds: Optional[int] = None
) -> int:
    """根据配置和 max_rounds 计算实际执行轮数。"""
    time_config = config.get("time_config", {})
    total_hours = time_config.get("total_simulation_hours", 72)
    minutes_per_round = time_config.get("minutes_per_round", 30)
    total_rounds = (total_hours * 60) // minutes_per_round

    if max_rounds is not None and max_rounds > 0:
        total_rounds = min(total_rounds, max_rounds)

    return total_rounds


def _collect_completed_round_entries(
    log_path: str,
    total_rounds: int
) -> Tuple[bool, List[Tuple[int, List[Dict[str, Any]]]]]:
    """
    从 actions.jsonl 中提取已完整结束轮次及其动作条数。

    只信任 round_end；未完成轮次的动作不会参与恢复。
    """
    artifacts_found = os.path.exists(log_path) and os.path.getsize(log_path) > 0
    if not os.path.exists(log_path):
        return artifacts_found, []

    actions_by_round: Dict[int, List[Dict[str, Any]]] = {}
    completed_rounds = set()

    with open(log_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            round_num = data.get("round")
            if not isinstance(round_num, int) or round_num < 0:
                continue
            if total_rounds > 0 and round_num > total_rounds:
                continue

            if data.get("event_type") == "round_end":
                completed_rounds.add(round_num)
            elif "event_type" not in data:
                actions_by_round.setdefault(round_num, []).append(data)

    ordered_rounds = sorted(completed_rounds)
    return artifacts_found, [
        (round_num, actions_by_round.get(round_num, []))
        for round_num in ordered_rounds
    ]


def _truncate_action_log_to_last_checkpoint(
    log_path: str,
    total_rounds: int
) -> Dict[str, Any]:
    """
    将 actions.jsonl 截断到最后一个有效的 round_end。

    这样可以在 resume 前移除崩溃留下的脏尾巴，例如：
    - 已写入的下一轮 round_start 但没有 round_end
    - 超出本次 max_rounds 的历史残留
    - 旧 simulation_end / round_end 混入新的恢复目标
    """
    if not os.path.exists(log_path):
        return {
            "exists": False,
            "truncated": False,
            "kept_round": 0,
            "dropped_lines": 0,
        }

    with open(log_path, 'r', encoding='utf-8') as f:
        raw_lines = f.readlines()

    completed_rounds = set()
    parsed_lines = []

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            parsed_lines.append((raw_line, None))
            continue

        parsed_lines.append((raw_line, data))

        round_num = data.get("round")
        if (
            data.get("event_type") == "round_end"
            and isinstance(round_num, int)
            and round_num >= 0
            and (total_rounds <= 0 or round_num <= total_rounds)
        ):
            completed_rounds.add(round_num)

    if not completed_rounds:
        return {
            "exists": True,
            "truncated": False,
            "kept_round": 0,
            "dropped_lines": 0,
        }

    last_checkpoint_round = max(completed_rounds)
    kept_lines = []
    kept_simulation_start = False

    for raw_line, data in parsed_lines:
        if data is None:
            continue

        event_type = data.get("event_type")
        round_num = data.get("round")

        if event_type == "simulation_start":
            if not kept_simulation_start:
                kept_lines.append(raw_line)
                kept_simulation_start = True
            continue

        if event_type == "simulation_end":
            continue

        if not isinstance(round_num, int) or round_num < 0:
            continue
        if round_num not in completed_rounds:
            continue
        if total_rounds > 0 and round_num > total_rounds:
            continue
        if round_num > last_checkpoint_round:
            continue

        kept_lines.append(raw_line)

    dropped_lines = len(raw_lines) - len(kept_lines)

    if dropped_lines > 0:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.writelines(kept_lines)

    return {
        "exists": True,
        "truncated": dropped_lines > 0,
        "kept_round": last_checkpoint_round,
        "dropped_lines": dropped_lines,
    }


def _sanitize_resume_action_logs(
    simulation_dir: str,
    total_rounds: int,
    log_info=logger.info
):
    """在 resume 前清掉超出最后有效检查点的日志尾巴。"""
    for platform in ("twitter", "reddit"):
        log_path = os.path.join(simulation_dir, platform, "actions.jsonl")
        result = _truncate_action_log_to_last_checkpoint(log_path, total_rounds)
        if result["truncated"]:
            log_info(
                f"[{platform.capitalize()}] 已清理恢复日志尾部: "
                f"kept_round={result['kept_round']}, dropped_lines={result['dropped_lines']}"
            )


def _build_replay_action_from_log_entry(
    action_entry: Dict[str, Any]
) -> Optional[ReplayActionSpec]:
    """优先使用 actions.jsonl 自身内容构建可重放动作。"""
    agent_id = action_entry.get("agent_id")
    action_type = action_entry.get("action_type")
    action_args = action_entry.get("action_args") or {}

    if agent_id is None or not action_type:
        return None

    if action_type == "CREATE_POST" and action_args.get("content") is not None:
        return ReplayActionSpec(agent_id, ActionType.CREATE_POST, {"content": action_args["content"]})

    if action_type == "LIKE_POST" and action_args.get("post_id") is not None:
        return ReplayActionSpec(agent_id, ActionType.LIKE_POST, {"post_id": action_args["post_id"]})

    if action_type == "DISLIKE_POST" and action_args.get("post_id") is not None:
        return ReplayActionSpec(agent_id, ActionType.DISLIKE_POST, {"post_id": action_args["post_id"]})

    if action_type == "SEARCH_POSTS" and action_args.get("query") is not None:
        return ReplayActionSpec(agent_id, ActionType.SEARCH_POSTS, {"query": action_args["query"]})

    if action_type == "SEARCH_USER" and action_args.get("query") is not None:
        return ReplayActionSpec(agent_id, ActionType.SEARCH_USER, {"query": action_args["query"]})

    if action_type == "LIKE_COMMENT" and action_args.get("comment_id") is not None:
        return ReplayActionSpec(agent_id, ActionType.LIKE_COMMENT, {"comment_id": action_args["comment_id"]})

    if action_type == "DISLIKE_COMMENT" and action_args.get("comment_id") is not None:
        return ReplayActionSpec(agent_id, ActionType.DISLIKE_COMMENT, {"comment_id": action_args["comment_id"]})

    if action_type == "QUOTE_POST":
        quoted_id = action_args.get("quoted_id")
        quote_content = action_args.get("quote_content")
        if quoted_id is not None and quote_content is not None:
            return ReplayActionSpec(
                agent_id,
                ActionType.QUOTE_POST,
                {"quote_message": (quoted_id, quote_content)}
            )

    if action_type == "TREND":
        return ReplayActionSpec(agent_id, ActionType.TREND, {})

    if action_type == "DO_NOTHING":
        return ReplayActionSpec(agent_id, ActionType.DO_NOTHING, {})

    return None


def _lookup_followee_id(cursor, follow_id: int) -> Optional[int]:
    cursor.execute(
        "SELECT followee_id FROM follow WHERE follow_id = ?",
        (follow_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _lookup_reposted_id(cursor, new_post_id: int) -> Optional[int]:
    cursor.execute(
        "SELECT original_post_id FROM post WHERE post_id = ?",
        (new_post_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _lookup_quote_content(cursor, new_post_id: int) -> Optional[str]:
    cursor.execute(
        "SELECT quote_content FROM post WHERE post_id = ?",
        (new_post_id,),
    )
    row = cursor.fetchone()
    return row[0] if row and row[0] is not None else None


def _lookup_comment_post_id(cursor, comment_id: int) -> Optional[int]:
    cursor.execute(
        "SELECT post_id FROM comment WHERE comment_id = ?",
        (comment_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _build_replay_action_spec(
    cursor,
    user_id: int,
    action_name: str,
    info_json: str
) -> Optional[ReplayActionSpec]:
    """把旧 trace 记录翻译成可顺序重放的 ManualAction 规格。"""
    try:
        action_info = json.loads(info_json) if info_json else {}
    except json.JSONDecodeError:
        action_info = {}

    if action_name == "create_post":
        content = action_info.get("content")
        if content is None:
            return None
        return ReplayActionSpec(user_id, ActionType.CREATE_POST, {"content": content})

    if action_name == "like_post":
        post_id = action_info.get("post_id")
        if post_id is None:
            return None
        return ReplayActionSpec(user_id, ActionType.LIKE_POST, {"post_id": post_id})

    if action_name == "dislike_post":
        post_id = action_info.get("post_id")
        if post_id is None:
            return None
        return ReplayActionSpec(user_id, ActionType.DISLIKE_POST, {"post_id": post_id})

    if action_name == "repost":
        post_id = action_info.get("reposted_id")
        if post_id is None:
            post_id = _lookup_reposted_id(cursor, action_info.get("new_post_id"))
        if post_id is None:
            return None
        return ReplayActionSpec(user_id, ActionType.REPOST, {"post_id": post_id})

    if action_name == "quote_post":
        quoted_id = action_info.get("quoted_id")
        quote_content = _lookup_quote_content(cursor, action_info.get("new_post_id"))
        if quoted_id is None or quote_content is None:
            return None
        return ReplayActionSpec(
            user_id,
            ActionType.QUOTE_POST,
            {"quote_message": (quoted_id, quote_content)}
        )

    if action_name == "follow":
        followee_id = action_info.get("followee_id")
        if followee_id is None:
            followee_id = _lookup_followee_id(cursor, action_info.get("follow_id"))
        if followee_id is None:
            return None
        return ReplayActionSpec(
            user_id,
            ActionType.FOLLOW,
            {"followee_id": followee_id}
        )

    if action_name == "mute":
        mutee_id = action_info.get("mutee_id")
        if mutee_id is None:
            return None
        return ReplayActionSpec(
            user_id,
            ActionType.MUTE,
            {"mutee_id": mutee_id}
        )

    if action_name == "create_comment":
        post_id = _lookup_comment_post_id(cursor, action_info.get("comment_id"))
        content = action_info.get("content")
        if post_id is None or content is None:
            return None
        return ReplayActionSpec(
            user_id,
            ActionType.CREATE_COMMENT,
            {"comment_message": (post_id, content)}
        )

    if action_name == "like_comment":
        comment_id = action_info.get("comment_id")
        if comment_id is None:
            return None
        return ReplayActionSpec(
            user_id,
            ActionType.LIKE_COMMENT,
            {"comment_id": comment_id}
        )

    if action_name == "dislike_comment":
        comment_id = action_info.get("comment_id")
        if comment_id is None:
            return None
        return ReplayActionSpec(
            user_id,
            ActionType.DISLIKE_COMMENT,
            {"comment_id": comment_id}
        )

    if action_name == "search_posts":
        query = action_info.get("query")
        if query is None:
            return None
        return ReplayActionSpec(
            user_id,
            ActionType.SEARCH_POSTS,
            {"query": query}
        )

    if action_name == "search_user":
        query = action_info.get("query")
        if query is None:
            return None
        return ReplayActionSpec(
            user_id,
            ActionType.SEARCH_USER,
            {"query": query}
        )

    if action_name == "trend":
        return ReplayActionSpec(user_id, ActionType.TREND, {})

    if action_name == "do_nothing":
        return ReplayActionSpec(user_id, ActionType.DO_NOTHING, {})

    logger.warning("[RESUME] Unsupported replay action: %s", action_name)
    return None


def load_platform_resume_plan(
    platform: str,
    simulation_dir: str,
    total_rounds: int,
    log_info=logger.info
) -> PlatformResumePlan:
    """从平台日志和旧数据库构建恢复计划。"""
    log_path = os.path.join(simulation_dir, platform, "actions.jsonl")
    db_path = os.path.join(simulation_dir, f"{platform}_simulation.db")

    artifacts_found, completed_rounds = _collect_completed_round_entries(
        log_path, total_rounds
    )
    artifacts_found = artifacts_found or (
        os.path.exists(db_path) and os.path.getsize(db_path) > 0
    )
    plan = PlatformResumePlan(platform=platform, artifacts_found=artifacts_found)

    if not completed_rounds:
        if artifacts_found:
            raise RuntimeError(f"{platform} 检测到历史痕迹，但没有有效的 round_end 检查点")
        return plan
    if not os.path.exists(db_path):
        raise RuntimeError(f"{platform} 存在历史日志，但数据库不存在，无法恢复")

    replayable_actions_count = sum(len(actions) for _, actions in completed_rounds)

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT rowid, user_id, action, info
            FROM trace
            WHERE action NOT IN (?, ?, ?)
            ORDER BY rowid ASC
            """,
            ("sign_up", "refresh", "interview"),
        )
        trace_rows = cursor.fetchall()

        row_index = 0
        for round_num, action_entries in completed_rounds:
            expected_count = len(action_entries)
            available_count = max(0, min(expected_count, len(trace_rows) - row_index))
            round_trace_rows = trace_rows[row_index:row_index + available_count]
            round_spec = ReplayRoundSpec(round_num=round_num)
            for index, action_entry in enumerate(action_entries):
                replay_action = _build_replay_action_from_log_entry(action_entry)
                if replay_action is None and index < len(round_trace_rows):
                    _, user_id, action_name, info_json = round_trace_rows[index]
                    replay_action = _build_replay_action_spec(cursor, user_id, action_name, info_json)
                if replay_action is None:
                    raise RuntimeError(
                        f"{platform} 无法恢复动作: round={round_num}, action={action_entry.get('action_type')}, agent_id={action_entry.get('agent_id')}"
                    )
                round_spec.actions.append(replay_action)

            plan.rounds.append(round_spec)
            row_index += available_count

        plan.has_checkpoint = True
        plan.last_completed_round = plan.rounds[-1].round_num
        plan.total_actions = replayable_actions_count
        log_info(
            f"[{platform.capitalize()}] 检测到恢复检查点: "
            f"last_round={plan.last_completed_round}, actions={plan.total_actions}"
        )
        return plan
    finally:
        conn.close()


async def replay_platform_state(
    env,
    resume_plan: PlatformResumePlan,
    platform_label: str,
    log_info=logger.info
) -> int:
    """
    顺序重放已完成轮次，重建平台 DB 和 Agent memory。

    注意：这里故意不用 env.step()，而是按 trace 原始顺序逐条执行，
    以保持 post/comment/follow 的自增 ID 与原运行一致。
    """
    if not resume_plan.has_checkpoint:
        return 0

    log_info(
        f"[{platform_label}] 开始重放 {len(resume_plan.rounds)} 个完整轮次 "
        f"({resume_plan.total_actions} 条动作)"
    )

    replayed_actions = 0

    for round_spec in resume_plan.rounds:
        if round_spec.actions:
            await env.platform.update_rec_table()
            for action_spec in round_spec.actions:
                agent = env.agent_graph.get_agent(action_spec.agent_id)
                await agent.perform_action_by_data(
                    action_spec.action_type,
                    **action_spec.action_args
                )
                replayed_actions += 1

            if env.platform_type == oasis.DefaultPlatformType.TWITTER:
                env.platform.sandbox_clock.time_step += 1

    log_info(
        f"[{platform_label}] 历史状态重放完成: "
        f"last_round={resume_plan.last_completed_round}, actions={replayed_actions}"
    )
    return replayed_actions


def get_last_trace_rowid(db_path: str) -> int:
    """获取 trace 表当前最大 rowid。"""
    if not os.path.exists(db_path):
        return 0

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(MAX(rowid), 0) FROM trace")
        row = cursor.fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


class PlatformSimulation:
    """平台模拟结果容器"""
    def __init__(self):
        self.env = None
        self.agent_graph = None
        self.total_actions = 0
        self.resume_plan = None


async def run_twitter_simulation(
    config: Dict[str, Any], 
    simulation_dir: str,
    action_logger: Optional[PlatformActionLogger] = None,
    main_logger: Optional[SimulationLogManager] = None,
    max_rounds: Optional[int] = None,
    resume: bool = False
) -> PlatformSimulation:
    """运行Twitter模拟
    
    Args:
        config: 模拟配置
        simulation_dir: 模拟目录
        action_logger: 动作日志记录器
        main_logger: 主日志管理器
        max_rounds: 最大模拟轮数（可选，用于截断过长的模拟）
        resume: 是否从最后一个有效检查点恢复
        
    Returns:
        PlatformSimulation: 包含env和agent_graph的结果对象
    """
    result = PlatformSimulation()
    
    def log_info(msg):
        if main_logger:
            main_logger.info(f"[Twitter] {msg}")
        logger.info(f"[Twitter] {msg}")
    
    log_info("初始化...")

    time_config = config.get("time_config", {})
    minutes_per_round = time_config.get("minutes_per_round", 30)
    total_rounds = _get_effective_total_rounds(config, max_rounds)
    resume_plan = None
    if resume:
        resume_plan = load_platform_resume_plan(
            "twitter",
            simulation_dir,
            total_rounds,
            log_info=log_info
        )
        result.resume_plan = resume_plan
    
    # Twitter 使用通用 LLM 配置
    model = create_model(config, use_boost=False)
    
    # OASIS Twitter使用CSV格式
    profile_path = os.path.join(simulation_dir, "twitter_profiles.csv")
    if not os.path.exists(profile_path):
        log_info(f"错误: Profile文件不存在: {profile_path}")
        return result
    
    result.agent_graph = await generate_twitter_agent_graph(
        profile_path=profile_path,
        model=model,
        available_actions=TWITTER_ACTIONS,
    )
    
    # 从配置文件获取 Agent 真实名称映射（使用 entity_name 而非默认的 Agent_X）
    agent_names = get_agent_names_from_config(config)
    # 如果配置中没有某个 agent，则使用 OASIS 的默认名称
    for agent_id, agent in result.agent_graph.get_agents():
        if agent_id not in agent_names:
            agent_names[agent_id] = getattr(agent, 'name', f'Agent_{agent_id}')
    
    db_path = os.path.join(simulation_dir, "twitter_simulation.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    
    result.env = oasis.make(
        agent_graph=result.agent_graph,
        platform=oasis.DefaultPlatformType.TWITTER,
        database_path=db_path,
        semaphore=30,  # 限制最大并发 LLM 请求数，防止 API 过载
    )
    
    await result.env.reset()
    log_info("环境已启动")

    total_actions = 0
    last_rowid = 0  # 跟踪数据库中最后处理的行号（使用 rowid 避免 created_at 格式差异）
    start_round_num = 0

    if resume_plan and resume_plan.has_checkpoint:
        total_actions = await replay_platform_state(
            result.env,
            resume_plan,
            "Twitter",
            log_info=log_info
        )
        last_rowid = get_last_trace_rowid(db_path)
        start_round_num = resume_plan.last_completed_round
        log_info(
            f"恢复完成，将从第 {start_round_num + 1} 轮继续 "
            f"(已恢复 {total_actions} 条动作)"
        )
    else:
        if action_logger:
            action_logger.log_simulation_start(config)

        # 执行初始事件
        event_config = config.get("event_config", {})
        initial_posts = event_config.get("initial_posts", [])

        # 记录 round 0 开始（初始事件阶段）
        if action_logger:
            action_logger.log_round_start(0, 0)  # round 0, simulated_hour 0

        initial_action_count = 0
        if initial_posts:
            initial_actions = {}
            for post in initial_posts:
                agent_id = post.get("poster_agent_id", 0)
                content = post.get("content", "")
                try:
                    agent = result.env.agent_graph.get_agent(agent_id)
                    initial_actions[agent] = ManualAction(
                        action_type=ActionType.CREATE_POST,
                        action_args={"content": content}
                    )

                    if action_logger:
                        action_logger.log_action(
                            round_num=0,
                            agent_id=agent_id,
                            agent_name=agent_names.get(agent_id, f"Agent_{agent_id}"),
                            action_type="CREATE_POST",
                            action_args={"content": content}
                        )
                        total_actions += 1
                        initial_action_count += 1
                except Exception as e:
                    logger.warning(f"创建Twitter初始帖子失败 (agent_id={agent_id}): {e}")

            if initial_actions:
                await env_step_with_timeout(
                    result.env, initial_actions,
                    step_description=f"Twitter initial posts ({len(initial_actions)} posts)"
                )
                log_info(f"已发布 {len(initial_actions)} 条初始帖子")

        # 记录 round 0 结束
        if action_logger:
            action_logger.log_round_end(0, initial_action_count)

        if max_rounds is not None and max_rounds > 0:
            config_total_rounds = _get_effective_total_rounds(config, None)
            if total_rounds < config_total_rounds:
                log_info(f"轮数已截断: {config_total_rounds} -> {total_rounds} (max_rounds={max_rounds})")
    
    start_time = datetime.now()

    if start_round_num >= total_rounds:
        result.total_actions = total_actions
        log_info(f"恢复检查点已达到目标轮次 ({start_round_num}/{total_rounds})，无需继续模拟")
        return result
    
    for round_num in range(start_round_num, total_rounds):
        # 检查是否收到退出信号
        if _shutdown_event and _shutdown_event.is_set():
            if main_logger:
                main_logger.info(f"收到退出信号，在第 {round_num + 1} 轮停止模拟")
            break
        
        simulated_minutes = round_num * minutes_per_round
        simulated_hour = (simulated_minutes // 60) % 24
        simulated_day = simulated_minutes // (60 * 24) + 1
        
        active_agents = get_active_agents_for_round(
            result.env, config, simulated_hour, round_num
        )
        
        # 无论是否有活跃agent，都记录round开始
        if action_logger:
            action_logger.log_round_start(round_num + 1, simulated_hour)
        
        if not active_agents:
            # 没有活跃agent时也记录round结束（actions_count=0）
            if action_logger:
                action_logger.log_round_end(round_num + 1, 0)
            continue
        
        actions = {agent: LLMAction() for _, agent in active_agents}
        try:
            await env_step_with_timeout(
                result.env, actions,
                step_description=f"Twitter round {round_num + 1} ({len(active_agents)} agents)"
            )
        except (TimeoutError, Exception) as e:
            log_info(f"Round {round_num + 1} 失败，跳过此轮: {e}")
            if action_logger:
                action_logger.log_round_end(round_num + 1, 0)
            continue

        # 从数据库获取实际执行的动作并记录
        actual_actions, last_rowid = fetch_new_actions_from_db(
            db_path, last_rowid, agent_names
        )

        round_action_count = 0
        for action_data in actual_actions:
            if action_logger:
                action_logger.log_action(
                    round_num=round_num + 1,
                    agent_id=action_data['agent_id'],
                    agent_name=action_data['agent_name'],
                    action_type=action_data['action_type'],
                    action_args=action_data['action_args']
                )
                total_actions += 1
                round_action_count += 1

        if action_logger:
            action_logger.log_round_end(round_num + 1, round_action_count)

        if (round_num + 1) % 20 == 0:
            progress = (round_num + 1) / total_rounds * 100
            log_info(f"Day {simulated_day}, {simulated_hour:02d}:00 - Round {round_num + 1}/{total_rounds} ({progress:.1f}%)")

    # 注意：不关闭环境，保留给Interview使用

    if action_logger:
        action_logger.log_simulation_end(total_rounds, total_actions)

    result.total_actions = total_actions
    elapsed = (datetime.now() - start_time).total_seconds()
    log_info(f"模拟循环完成! 耗时: {elapsed:.1f}秒, 总动作: {total_actions}")

    return result


async def run_reddit_simulation(
    config: Dict[str, Any], 
    simulation_dir: str,
    action_logger: Optional[PlatformActionLogger] = None,
    main_logger: Optional[SimulationLogManager] = None,
    max_rounds: Optional[int] = None,
    resume: bool = False
) -> PlatformSimulation:
    """运行Reddit模拟
    
    Args:
        config: 模拟配置
        simulation_dir: 模拟目录
        action_logger: 动作日志记录器
        main_logger: 主日志管理器
        max_rounds: 最大模拟轮数（可选，用于截断过长的模拟）
        resume: 是否从最后一个有效检查点恢复
        
    Returns:
        PlatformSimulation: 包含env和agent_graph的结果对象
    """
    result = PlatformSimulation()
    
    def log_info(msg):
        if main_logger:
            main_logger.info(f"[Reddit] {msg}")
        logger.info(f"[Reddit] {msg}")
    
    log_info("初始化...")

    time_config = config.get("time_config", {})
    minutes_per_round = time_config.get("minutes_per_round", 30)
    total_rounds = _get_effective_total_rounds(config, max_rounds)
    resume_plan = None
    if resume:
        resume_plan = load_platform_resume_plan(
            "reddit",
            simulation_dir,
            total_rounds,
            log_info=log_info
        )
        result.resume_plan = resume_plan
    
    # Reddit 使用加速 LLM 配置（如果有的话，否则回退到通用配置）
    model = create_model(config, use_boost=True)
    
    profile_path = os.path.join(simulation_dir, "reddit_profiles.json")
    if not os.path.exists(profile_path):
        log_info(f"错误: Profile文件不存在: {profile_path}")
        return result
    
    result.agent_graph = await generate_reddit_agent_graph(
        profile_path=profile_path,
        model=model,
        available_actions=REDDIT_ACTIONS,
    )
    
    # 从配置文件获取 Agent 真实名称映射（使用 entity_name 而非默认的 Agent_X）
    agent_names = get_agent_names_from_config(config)
    # 如果配置中没有某个 agent，则使用 OASIS 的默认名称
    for agent_id, agent in result.agent_graph.get_agents():
        if agent_id not in agent_names:
            agent_names[agent_id] = getattr(agent, 'name', f'Agent_{agent_id}')
    
    db_path = os.path.join(simulation_dir, "reddit_simulation.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    
    result.env = oasis.make(
        agent_graph=result.agent_graph,
        platform=oasis.DefaultPlatformType.REDDIT,
        database_path=db_path,
        semaphore=30,  # 限制最大并发 LLM 请求数，防止 API 过载
    )
    
    await result.env.reset()
    log_info("环境已启动")

    total_actions = 0
    last_rowid = 0  # 跟踪数据库中最后处理的行号（使用 rowid 避免 created_at 格式差异）
    start_round_num = 0

    if resume_plan and resume_plan.has_checkpoint:
        total_actions = await replay_platform_state(
            result.env,
            resume_plan,
            "Reddit",
            log_info=log_info
        )
        last_rowid = get_last_trace_rowid(db_path)
        start_round_num = resume_plan.last_completed_round
        log_info(
            f"恢复完成，将从第 {start_round_num + 1} 轮继续 "
            f"(已恢复 {total_actions} 条动作)"
        )
    else:
        if action_logger:
            action_logger.log_simulation_start(config)

        # 执行初始事件
        event_config = config.get("event_config", {})
        initial_posts = event_config.get("initial_posts", [])

        # 记录 round 0 开始（初始事件阶段）
        if action_logger:
            action_logger.log_round_start(0, 0)  # round 0, simulated_hour 0

        initial_action_count = 0
        if initial_posts:
            initial_actions = {}
            for post in initial_posts:
                agent_id = post.get("poster_agent_id", 0)
                content = post.get("content", "")
                try:
                    agent = result.env.agent_graph.get_agent(agent_id)
                    initial_actions[agent] = ManualAction(
                        action_type=ActionType.CREATE_POST,
                        action_args={"content": content}
                    )

                    if action_logger:
                        action_logger.log_action(
                            round_num=0,
                            agent_id=agent_id,
                            agent_name=agent_names.get(agent_id, f"Agent_{agent_id}"),
                            action_type="CREATE_POST",
                            action_args={"content": content}
                        )
                        total_actions += 1
                        initial_action_count += 1
                except Exception as e:
                    logger.warning(f"创建Reddit初始帖子失败 (agent_id={agent_id}): {e}")

            if initial_actions:
                await env_step_with_timeout(
                    result.env, initial_actions,
                    step_description=f"Reddit initial posts ({len(initial_actions)} posts)"
                )
                log_info(f"已发布 {len(initial_actions)} 条初始帖子")

        # 记录 round 0 结束
        if action_logger:
            action_logger.log_round_end(0, initial_action_count)

        if max_rounds is not None and max_rounds > 0:
            config_total_rounds = _get_effective_total_rounds(config, None)
            if total_rounds < config_total_rounds:
                log_info(f"轮数已截断: {config_total_rounds} -> {total_rounds} (max_rounds={max_rounds})")
    
    start_time = datetime.now()

    if start_round_num >= total_rounds:
        result.total_actions = total_actions
        log_info(f"恢复检查点已达到目标轮次 ({start_round_num}/{total_rounds})，无需继续模拟")
        return result
    
    for round_num in range(start_round_num, total_rounds):
        # 检查是否收到退出信号
        if _shutdown_event and _shutdown_event.is_set():
            if main_logger:
                main_logger.info(f"收到退出信号，在第 {round_num + 1} 轮停止模拟")
            break
        
        simulated_minutes = round_num * minutes_per_round
        simulated_hour = (simulated_minutes // 60) % 24
        simulated_day = simulated_minutes // (60 * 24) + 1
        
        active_agents = get_active_agents_for_round(
            result.env, config, simulated_hour, round_num
        )
        
        # 无论是否有活跃agent，都记录round开始
        if action_logger:
            action_logger.log_round_start(round_num + 1, simulated_hour)
        
        if not active_agents:
            # 没有活跃agent时也记录round结束（actions_count=0）
            if action_logger:
                action_logger.log_round_end(round_num + 1, 0)
            continue
        
        actions = {agent: LLMAction() for _, agent in active_agents}
        try:
            await env_step_with_timeout(
                result.env, actions,
                step_description=f"Reddit round {round_num + 1} ({len(active_agents)} agents)"
            )
        except (TimeoutError, Exception) as e:
            log_info(f"Round {round_num + 1} 失败，跳过此轮: {e}")
            if action_logger:
                action_logger.log_round_end(round_num + 1, 0)
            continue

        # 从数据库获取实际执行的动作并记录
        actual_actions, last_rowid = fetch_new_actions_from_db(
            db_path, last_rowid, agent_names
        )

        round_action_count = 0
        for action_data in actual_actions:
            if action_logger:
                action_logger.log_action(
                    round_num=round_num + 1,
                    agent_id=action_data['agent_id'],
                    agent_name=action_data['agent_name'],
                    action_type=action_data['action_type'],
                    action_args=action_data['action_args']
                )
                total_actions += 1
                round_action_count += 1

        if action_logger:
            action_logger.log_round_end(round_num + 1, round_action_count)

        if (round_num + 1) % 20 == 0:
            progress = (round_num + 1) / total_rounds * 100
            log_info(f"Day {simulated_day}, {simulated_hour:02d}:00 - Round {round_num + 1}/{total_rounds} ({progress:.1f}%)")

    # 注意：不关闭环境，保留给Interview使用

    if action_logger:
        action_logger.log_simulation_end(total_rounds, total_actions)

    result.total_actions = total_actions
    elapsed = (datetime.now() - start_time).total_seconds()
    log_info(f"模拟循环完成! 耗时: {elapsed:.1f}秒, 总动作: {total_actions}")

    return result


async def main():
    parser = argparse.ArgumentParser(description='OASIS双平台并行模拟')
    parser.add_argument(
        '--config', 
        type=str, 
        required=True,
        help='配置文件路径 (simulation_config.json)'
    )
    parser.add_argument(
        '--twitter-only',
        action='store_true',
        help='只运行Twitter模拟'
    )
    parser.add_argument(
        '--reddit-only',
        action='store_true',
        help='只运行Reddit模拟'
    )
    parser.add_argument(
        '--max-rounds',
        type=int,
        default=None,
        help='最大模拟轮数（可选，用于截断过长的模拟）'
    )
    parser.add_argument(
        '--resume',
        action='store_true',
        default=False,
        help='从最后一个有效 round_end 恢复模拟'
    )
    parser.add_argument(
        '--no-wait',
        action='store_true',
        default=False,
        help='模拟完成后立即关闭环境，不进入等待命令模式'
    )
    
    args = parser.parse_args()
    
    # 在 main 函数开始时创建 shutdown 事件，确保整个程序都能响应退出信号
    global _shutdown_event
    _shutdown_event = asyncio.Event()
    
    if not os.path.exists(args.config):
        logger.error(f"错误: 配置文件不存在: {args.config}")
        sys.exit(1)
    
    config = load_config(args.config)
    simulation_dir = os.path.dirname(args.config) or "."
    wait_for_commands = not args.no_wait
    
    # 初始化日志配置（禁用 OASIS 日志，清理旧文件）
    init_logging_for_simulation(simulation_dir, clear_old_logs=not args.resume)
    
    # 创建日志管理器
    log_manager = SimulationLogManager(simulation_dir, append=args.resume)
    twitter_logger = log_manager.get_twitter_logger()
    reddit_logger = log_manager.get_reddit_logger()
    
    log_manager.info("=" * 60)
    log_manager.info("OASIS 双平台并行模拟")
    log_manager.info(f"配置文件: {args.config}")
    log_manager.info(f"模拟ID: {config.get('simulation_id', 'unknown')}")
    log_manager.info(f"等待命令模式: {'启用' if wait_for_commands else '禁用'}")
    log_manager.info(f"恢复模式: {'启用' if args.resume else '禁用'}")
    log_manager.info("=" * 60)
    
    time_config = config.get("time_config", {})
    total_hours = time_config.get('total_simulation_hours', 72)
    minutes_per_round = time_config.get('minutes_per_round', 30)
    config_total_rounds = (total_hours * 60) // minutes_per_round
    
    log_manager.info(f"模拟参数:")
    log_manager.info(f"  - 总模拟时长: {total_hours}小时")
    log_manager.info(f"  - 每轮时间: {minutes_per_round}分钟")
    log_manager.info(f"  - 配置总轮数: {config_total_rounds}")
    if args.max_rounds:
        log_manager.info(f"  - 最大轮数限制: {args.max_rounds}")
        if args.max_rounds < config_total_rounds:
            log_manager.info(f"  - 实际执行轮数: {args.max_rounds} (已截断)")
    log_manager.info(f"  - Agent数量: {len(config.get('agent_configs', []))}")
    
    log_manager.info("日志结构:")
    log_manager.info(f"  - 主日志: simulation.log")
    log_manager.info(f"  - Twitter动作: twitter/actions.jsonl")
    log_manager.info(f"  - Reddit动作: reddit/actions.jsonl")
    log_manager.info("=" * 60)

    if args.resume:
        effective_total_rounds = _get_effective_total_rounds(config, args.max_rounds)
        _sanitize_resume_action_logs(
            simulation_dir,
            effective_total_rounds,
            log_info=log_manager.info
        )
    
    start_time = datetime.now()
    
    # 存储两个平台的模拟结果
    twitter_result: Optional[PlatformSimulation] = None
    reddit_result: Optional[PlatformSimulation] = None
    
    if args.twitter_only:
        twitter_result = await run_twitter_simulation(
            config, simulation_dir, twitter_logger, log_manager, args.max_rounds, args.resume
        )
    elif args.reddit_only:
        reddit_result = await run_reddit_simulation(
            config, simulation_dir, reddit_logger, log_manager, args.max_rounds, args.resume
        )
    else:
        # 并行运行（每个平台使用独立的日志记录器）
        results = await asyncio.gather(
            run_twitter_simulation(config, simulation_dir, twitter_logger, log_manager, args.max_rounds, args.resume),
            run_reddit_simulation(config, simulation_dir, reddit_logger, log_manager, args.max_rounds, args.resume),
        )
        twitter_result, reddit_result = results
    
    total_elapsed = (datetime.now() - start_time).total_seconds()
    log_manager.info("=" * 60)
    log_manager.info(f"模拟循环完成! 总耗时: {total_elapsed:.1f}秒")
    
    # 是否进入等待命令模式
    if wait_for_commands:
        log_manager.info("")
        log_manager.info("=" * 60)
        log_manager.info("进入等待命令模式 - 环境保持运行")
        log_manager.info("支持的命令: interview, batch_interview, close_env")
        log_manager.info("=" * 60)
        
        # 创建IPC处理器
        ipc_handler = ParallelIPCHandler(
            simulation_dir=simulation_dir,
            twitter_env=twitter_result.env if twitter_result else None,
            twitter_agent_graph=twitter_result.agent_graph if twitter_result else None,
            reddit_env=reddit_result.env if reddit_result else None,
            reddit_agent_graph=reddit_result.agent_graph if reddit_result else None
        )
        ipc_handler.update_status("alive")
        
        # 等待命令循环（使用全局 _shutdown_event）
        try:
            while not _shutdown_event.is_set():
                should_continue = await ipc_handler.process_commands()
                if not should_continue:
                    break
                # 使用 wait_for 替代 sleep，这样可以响应 shutdown_event
                try:
                    await asyncio.wait_for(_shutdown_event.wait(), timeout=0.5)
                    break  # 收到退出信号
                except asyncio.TimeoutError:
                    pass  # 超时继续循环
        except KeyboardInterrupt:
            logger.info("收到中断信号")
        except asyncio.CancelledError:
            logger.info("任务被取消")
        except Exception as e:
            logger.error(f"命令处理出错: {e}")
        
        log_manager.info("\n关闭环境...")
        ipc_handler.update_status("stopped")
    
    # 关闭环境
    if twitter_result and twitter_result.env:
        await twitter_result.env.close()
        log_manager.info("[Twitter] 环境已关闭")
    
    if reddit_result and reddit_result.env:
        await reddit_result.env.close()
        log_manager.info("[Reddit] 环境已关闭")
    
    log_manager.info("=" * 60)
    log_manager.info(f"全部完成!")
    log_manager.info(f"日志文件:")
    log_manager.info(f"  - {os.path.join(simulation_dir, 'simulation.log')}")
    log_manager.info(f"  - {os.path.join(simulation_dir, 'twitter', 'actions.jsonl')}")
    log_manager.info(f"  - {os.path.join(simulation_dir, 'reddit', 'actions.jsonl')}")
    log_manager.info("=" * 60)


def setup_signal_handlers(loop=None):
    """
    设置信号处理器，确保收到 SIGTERM/SIGINT 时能够正确退出
    
    持久化模拟场景：模拟完成后不退出，等待 interview 命令
    当收到终止信号时，需要：
    1. 通知 asyncio 循环退出等待
    2. 让程序有机会正常清理资源（关闭数据库、环境等）
    3. 然后才退出
    """
    def signal_handler(signum, frame):
        global _cleanup_done
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        logger.info(f"收到 {sig_name} 信号，正在退出...")

        if not _cleanup_done:
            _cleanup_done = True
            # 设置事件通知 asyncio 循环退出（让循环有机会清理资源）
            if _shutdown_event:
                _shutdown_event.set()

        # 不要直接 sys.exit()，让 asyncio 循环正常退出并清理资源
        # 如果是重复收到信号，才强制退出
        else:
            logger.warning("强制退出...")
            sys.exit(1)
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


if __name__ == "__main__":
    setup_signal_handlers()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序被中断")
    except SystemExit:
        pass
    finally:
        # 清理 multiprocessing 资源跟踪器（防止退出时的警告）
        try:
            from multiprocessing import resource_tracker
            resource_tracker._resource_tracker._stop()
        except Exception:
            pass
        logger.info("模拟进程已退出")
