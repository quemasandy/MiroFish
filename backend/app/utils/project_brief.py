"""
Project brief helpers.

Normalizes structured project context captured at intake and exposes
small helpers for prompts and locale-sensitive defaults.
"""

from __future__ import annotations

import json
from typing import Any, Dict


BRIEF_FIELDS = (
    "analysis_domain",
    "geography",
    "timezone",
    "prediction_horizon",
    "decision_focus",
)

ANALYSIS_DOMAIN_HINTS = {
    "public_opinion_election": (
        "这是一个选举/公共舆论预测项目。优先关注候选人、政党、媒体、"
        "社区组织、意见领袖、联盟、冲突、动员、叙事和选民分层。"
    ),
    "public_opinion": (
        "这是一个公共舆论项目。优先关注群体反应、媒体传播、关键议题、"
        "叙事演化和意见领袖。"
    ),
    "reputation_crisis": (
        "这是一个声誉/危机传播项目。优先关注品牌、机构、媒体、受影响群体、"
        "情绪升级和修复路径。"
    ),
    "policy_response": (
        "这是一个政策反应项目。优先关注政策受众、政府机构、利益相关方、"
        "舆论反弹与支持基础。"
    ),
}

COUNTRY_KEYWORDS = {
    "ecuador": "厄瓜多尔",
    "quito": "厄瓜多尔",
    "guayaquil": "厄瓜多尔",
    "china": "中国",
    "beijing": "中国",
    "shanghai": "中国",
    "usa": "美国",
    "united states": "美国",
    "new york": "美国",
    "mexico": "墨西哥",
    "colombia": "哥伦比亚",
    "peru": "秘鲁",
    "chile": "智利",
    "argentina": "阿根廷",
    "brazil": "巴西",
    "spain": "西班牙",
}


def normalize_project_brief(raw: Any) -> Dict[str, str]:
    """Normalize a project brief from JSON/string/dict input."""
    if raw is None:
        return {}

    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}

    if not isinstance(raw, dict):
        return {}

    brief: Dict[str, str] = {}
    for field in BRIEF_FIELDS:
        value = raw.get(field)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            brief[field] = normalized

    return brief


def build_project_brief_context(brief: Dict[str, str]) -> str:
    """Render structured brief context for prompts."""
    if not brief:
        return ""

    lines = ["## 项目结构化简报"]
    labels = {
        "analysis_domain": "分析域",
        "geography": "地理范围",
        "timezone": "时区",
        "prediction_horizon": "预测时间范围",
        "decision_focus": "决策目标",
    }
    for field in BRIEF_FIELDS:
        value = brief.get(field)
        if value:
            lines.append(f"- {labels[field]}: {value}")

    domain = brief.get("analysis_domain")
    if domain and domain in ANALYSIS_DOMAIN_HINTS:
        lines.append(f"- 领域提示: {ANALYSIS_DOMAIN_HINTS[domain]}")

    return "\n".join(lines)


def infer_country_label(brief: Dict[str, str]) -> str:
    """Infer a Chinese country label from geography/timezone hints."""
    haystack = " ".join([
        brief.get("geography", ""),
        brief.get("timezone", ""),
    ]).lower()

    for keyword, country_label in COUNTRY_KEYWORDS.items():
        if keyword in haystack:
            return country_label

    return "本地"


def infer_activity_locale_hint(brief: Dict[str, str]) -> str:
    """Return a human-readable locale hint for time/activity prompts."""
    geography = brief.get("geography")
    timezone = brief.get("timezone")

    if geography and timezone:
        return f"{geography}（时区 {timezone}）"
    if geography:
        return geography
    if timezone:
        return f"时区 {timezone}"
    return "项目本地语境"
