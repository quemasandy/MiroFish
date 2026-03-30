"""
LLM routing utilities.

Resolves which model configuration should be used for each pipeline stage.
"""

from dataclasses import dataclass
from typing import Optional, Set

from ..config import Config


ROUTING_PROFILE_CONSERVATIVE = "conservative"
ROUTING_PROFILE_BALANCED = "balanced"
ROUTING_PROFILE_MAX_QUALITY = "max_quality"

VALID_ROUTING_PROFILES = {
    ROUTING_PROFILE_CONSERVATIVE,
    ROUTING_PROFILE_BALANCED,
    ROUTING_PROFILE_MAX_QUALITY,
}

STAGE_ONTOLOGY = "ontology"
STAGE_GRAPH_EXTRACTION = "graph_extraction"
STAGE_PROFILE_GENERATION = "profile_generation"
STAGE_SIM_CONFIG_TIME_EVENT = "sim_config_time_event"
STAGE_SIM_CONFIG_AGENT_BATCH = "sim_config_agent_batch"
STAGE_SIMULATION_RUNTIME = "simulation_runtime"
STAGE_AGENT_INTERVIEW_RUNTIME = "agent_interview_runtime"
STAGE_REPORT_OUTLINE = "report_outline"
STAGE_REPORT_SECTION = "report_section"
STAGE_REPORT_TOOL_LLM = "report_tool_llm"
STAGE_REPORT_CHAT = "report_chat"

PREMIUM_STAGES_BY_PROFILE = {
    ROUTING_PROFILE_CONSERVATIVE: {
        STAGE_REPORT_OUTLINE,
        STAGE_REPORT_SECTION,
        STAGE_REPORT_TOOL_LLM,
        STAGE_REPORT_CHAT,
    },
    ROUTING_PROFILE_BALANCED: {
        STAGE_SIM_CONFIG_TIME_EVENT,
        STAGE_REPORT_OUTLINE,
        STAGE_REPORT_SECTION,
        STAGE_REPORT_TOOL_LLM,
        STAGE_REPORT_CHAT,
    },
    ROUTING_PROFILE_MAX_QUALITY: {
        STAGE_ONTOLOGY,
        STAGE_GRAPH_EXTRACTION,
        STAGE_PROFILE_GENERATION,
        STAGE_SIM_CONFIG_TIME_EVENT,
        STAGE_SIM_CONFIG_AGENT_BATCH,
        STAGE_REPORT_OUTLINE,
        STAGE_REPORT_SECTION,
        STAGE_REPORT_TOOL_LLM,
        STAGE_REPORT_CHAT,
    },
}


@dataclass(frozen=True)
class ResolvedLLMConfig:
    """Resolved LLM configuration for one pipeline stage."""

    stage: str
    profile: str
    route: str
    api_key: str
    base_url: str
    model_name: str
    premium_available: bool


def normalize_routing_profile(profile: Optional[str]) -> str:
    """Normalize an env-provided routing profile."""
    normalized = (profile or ROUTING_PROFILE_BALANCED).strip().lower()
    if normalized not in VALID_ROUTING_PROFILES:
        return ROUTING_PROFILE_BALANCED
    return normalized


def get_premium_stages(profile: Optional[str] = None) -> Set[str]:
    """Return the premium stage set for the selected profile."""
    normalized = normalize_routing_profile(profile or Config.LLM_ROUTING_PROFILE)
    return PREMIUM_STAGES_BY_PROFILE[normalized]


def resolve_llm_config(
    stage: str,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model_name: Optional[str] = None,
) -> ResolvedLLMConfig:
    """
    Resolve LLM credentials and model for a given pipeline stage.

    Explicit overrides bypass routing and preserve previous behavior.
    """
    if api_key is not None or base_url is not None or model_name is not None:
        return ResolvedLLMConfig(
            stage=stage,
            profile=normalize_routing_profile(Config.LLM_ROUTING_PROFILE),
            route="explicit",
            api_key=api_key or Config.LLM_API_KEY or "",
            base_url=base_url or Config.LLM_BASE_URL,
            model_name=model_name or Config.LLM_MODEL_NAME,
            premium_available=bool(Config.LLM_PREMIUM_MODEL_NAME and Config.LLM_PREMIUM_API_KEY),
        )

    profile = normalize_routing_profile(Config.LLM_ROUTING_PROFILE)
    premium_requested = stage in PREMIUM_STAGES_BY_PROFILE[profile]

    premium_api_key = Config.LLM_PREMIUM_API_KEY or Config.LLM_API_KEY or ""
    premium_base_url = Config.LLM_PREMIUM_BASE_URL or Config.LLM_BASE_URL
    premium_model_name = Config.LLM_PREMIUM_MODEL_NAME or ""
    premium_available = bool(premium_api_key and premium_model_name)

    if premium_requested and premium_available:
        return ResolvedLLMConfig(
            stage=stage,
            profile=profile,
            route="premium",
            api_key=premium_api_key,
            base_url=premium_base_url,
            model_name=premium_model_name,
            premium_available=True,
        )

    return ResolvedLLMConfig(
        stage=stage,
        profile=profile,
        route="base-fallback" if premium_requested else "base",
        api_key=Config.LLM_API_KEY or "",
        base_url=Config.LLM_BASE_URL,
        model_name=Config.LLM_MODEL_NAME,
        premium_available=premium_available,
    )
