"""
模拟配置智能生成器
使用LLM根据模拟需求、文档内容、图谱信息自动生成细致的模拟参数
实现全程自动化，无需人工设置参数

采用分步生成策略，避免一次性生成过长内容导致失败：
1. 生成时间配置
2. 生成事件配置
3. 分批生成Agent配置
4. 生成平台配置
"""

import json
import math
import time
import zlib
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field, asdict
from datetime import datetime

from ..config import Config
from ..llm.client import create_completion, is_response_format_unsupported
from ..llm.profiles import LLMProfile, LLMSettings, load_settings
from ..llm.router import assign_agent_profiles, resolve_profile
from ..utils.logger import get_logger
from ..utils.locale import get_language_instruction, get_locale, t
from ..utils.openai_chat_compat import extract_chat_completion_text
from .persona_archetypes import archetype_activity_overrides, assign_archetypes, get_archetype
from .zep_entity_reader import EntityNode

logger = get_logger('mirofish.simulation_config')

LLM_STAGE = "config"

_DEFAULT_ACTIVITY_MULTIPLIERS = {
    "dead": 0.05,      # 深夜几乎无人
    "morning": 0.4,    # 早间逐渐活跃
    "work": 0.7,       # 工作时段中等
    "peak": 1.5,       # 晚间高峰
    "night": 0.5,      # 深夜下降
}

# 地区作息配置（按 locale 选择；场景另有说明时由 LLM 调整）
# 小时为本地时间 0-23；区间均为左闭右开，例如 range(21, 24) = 21:00-24:00
REGION_PROFILES: Dict[str, Dict[str, Any]] = {
    # 原 CHINA_TIMEZONE_CONFIG，数值保持不变
    "zh-CN": {
        "timezone": "Asia/Shanghai",
        "utc_offset": "+08:00",
        "dead_hours": [0, 1, 2, 3, 4, 5],
        "morning_hours": [6, 7, 8],
        "work_hours": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
        "peak_hours": [19, 20, 21, 22],
        "night_hours": [23],
        "routine_windows": {},
        "activity_multipliers": dict(_DEFAULT_ACTIVITY_MULTIPLIERS),
        "rule_active_hours": {
            "institution": list(range(9, 18)),
            "media": list(range(7, 24)),
            "expert": list(range(8, 22)),
            "student": [8, 9, 10, 11, 12, 13, 18, 19, 20, 21, 22, 23],
            "alumni": [12, 13, 19, 20, 21, 22, 23],
            "person": [9, 10, 11, 12, 13, 18, 19, 20, 21, 22, 23],
        },
    },
    # 韩国：出勤 07-09，午餐 12-13，下班 18-20，高峰 21-24，深夜 02-06
    "ko-KR": {
        "timezone": "Asia/Seoul",
        "utc_offset": "+09:00",
        "dead_hours": list(range(2, 6)),
        "morning_hours": [6, 7, 8],
        "work_hours": list(range(9, 18)),
        "peak_hours": list(range(21, 24)),
        "night_hours": [0, 1],
        "routine_windows": {
            "commute": list(range(7, 9)),
            "lunch": list(range(12, 13)),
            "after_work": list(range(18, 20)),
        },
        "activity_multipliers": dict(_DEFAULT_ACTIVITY_MULTIPLIERS),
        "rule_active_hours": {
            "institution": list(range(9, 18)),
            "media": list(range(7, 24)),
            "expert": list(range(8, 23)),
            "student": [12, 13, 18, 19, 20, 21, 22, 23, 0, 1],
            "alumni": [7, 8, 12, 18, 19, 21, 22, 23],
            "person": [7, 8, 12, 18, 19, 20, 21, 22, 23],
        },
    },
    # 通用英语地区作息（不绑定具体时区）
    "en-US": {
        "timezone": "local",
        "utc_offset": None,
        "dead_hours": [1, 2, 3, 4, 5],
        "morning_hours": [6, 7, 8],
        "work_hours": list(range(9, 17)),
        "peak_hours": [18, 19, 20, 21],
        "night_hours": [22, 23, 0],
        "routine_windows": {},
        "activity_multipliers": dict(_DEFAULT_ACTIVITY_MULTIPLIERS),
        "rule_active_hours": {
            "institution": list(range(9, 17)),
            "media": list(range(6, 24)),
            "expert": list(range(8, 21)),
            "student": [11, 12, 13, 17, 18, 19, 20, 21, 22, 23],
            "alumni": [7, 8, 12, 17, 18, 19, 20, 21],
            "person": [7, 8, 12, 17, 18, 19, 20, 21, 22],
        },
    },
}

_LOCALE_REGIONS = {"ko": "ko-KR", "zh": "zh-CN", "en": "en-US"}
DEFAULT_REGION = "en-US"


def select_region(locale: Optional[str] = None) -> str:
    """Pick a region profile id for a UI/LLM locale (current thread/request locale by default)."""
    locale = (locale or get_locale() or "").lower()
    return _LOCALE_REGIONS.get(locale.split("-", 1)[0], DEFAULT_REGION)


def _stable_seed(value: str) -> int:
    return zlib.crc32(value.encode("utf-8"))


def normalize_persona_mix(persona_mix: Optional[Dict[str, Any]], simulation_id: str) -> Dict[str, Any]:
    """Return {"preset", "seed"}; missing preset means "off", missing seed derives from the simulation id."""
    persona_mix = persona_mix or {}
    preset = persona_mix.get("preset") or "off"
    seed = persona_mix.get("seed")
    if seed is None:
        seed = _stable_seed(simulation_id)
    if not isinstance(preset, str):
        raise ValueError(f"persona_mix.preset must be a string, got {preset!r}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"persona_mix.seed must be an integer, got {seed!r}")
    return {"preset": preset, "seed": seed}


@dataclass
class AgentActivityConfig:
    """单个Agent的活动配置"""
    agent_id: int
    entity_uuid: str
    entity_name: str
    entity_type: str
    
    # 活跃度配置 (0.0-1.0)
    activity_level: float = 0.5  # 整体活跃度
    
    # 发言频率（每小时预期发言次数）
    posts_per_hour: float = 1.0
    comments_per_hour: float = 2.0
    
    # 活跃时间段（24小时制，0-23）
    active_hours: List[int] = field(default_factory=lambda: list(range(8, 23)))
    
    # 响应速度（对热点事件的反应延迟，单位：模拟分钟）
    response_delay_min: int = 5
    response_delay_max: int = 60
    
    # 情感倾向 (-1.0到1.0，负面到正面)
    sentiment_bias: float = 0.0
    
    # 立场（对特定话题的态度）
    stance: str = "neutral"  # supportive, opposing, neutral, observer
    
    # 影响力权重（决定其发言被其他Agent看到的概率）
    influence_weight: float = 1.0

    # 该Agent使用的LLM profile id（llm.router.assign_agent_profiles）
    llm_profile: Optional[str] = None

    # 人设原型 id（persona_archetypes），未启用时为 None
    archetype: Optional[str] = None


@dataclass  
class TimeSimulationConfig:
    """时间模拟配置（默认值来自 zh-CN 地区作息，生成时按 REGION_PROFILES 覆盖）"""
    # 模拟总时长（模拟小时数）
    total_simulation_hours: int = 72  # 默认模拟72小时（3天）
    
    # 每轮代表的时间（模拟分钟）- 默认60分钟（1小时），加快时间流速
    minutes_per_round: int = 60
    
    # 每小时激活的Agent数量范围
    agents_per_hour_min: int = 5
    agents_per_hour_max: int = 20
    
    # 高峰时段（默认晚间19-22点）
    peak_hours: List[int] = field(default_factory=lambda: [19, 20, 21, 22])
    peak_activity_multiplier: float = 1.5
    
    # 低谷时段（凌晨0-5点，几乎无人活动）
    off_peak_hours: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5])
    off_peak_activity_multiplier: float = 0.05  # 凌晨活跃度极低
    
    # 早间时段
    morning_hours: List[int] = field(default_factory=lambda: [6, 7, 8])
    morning_activity_multiplier: float = 0.4
    
    # 工作时段
    work_hours: List[int] = field(default_factory=lambda: [9, 10, 11, 12, 13, 14, 15, 16, 17, 18])
    work_activity_multiplier: float = 0.7

    # 地区作息来源
    region: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    # 额外命名时段（如 commute / lunch / after_work）
    routine_windows: Dict[str, List[int]] = field(default_factory=dict)


@dataclass
class EventConfig:
    """事件配置"""
    # 初始事件（模拟开始时的触发事件）
    initial_posts: List[Dict[str, Any]] = field(default_factory=list)
    
    # 定时事件（在特定时间触发的事件）
    scheduled_events: List[Dict[str, Any]] = field(default_factory=list)
    
    # 热点话题关键词
    hot_topics: List[str] = field(default_factory=list)
    
    # 舆论引导方向
    narrative_direction: str = ""


@dataclass
class PlatformConfig:
    """平台特定配置"""
    platform: str  # twitter or reddit
    
    # 推荐算法权重
    recency_weight: float = 0.4  # 时间新鲜度
    popularity_weight: float = 0.3  # 热度
    relevance_weight: float = 0.3  # 相关性
    
    # 病毒传播阈值（达到多少互动后触发扩散）
    viral_threshold: int = 10
    
    # 回声室效应强度（相似观点聚集程度）
    echo_chamber_strength: float = 0.5


@dataclass
class SimulationParameters:
    """完整的模拟参数配置"""
    # 基础信息
    simulation_id: str
    project_id: str
    graph_id: str
    simulation_requirement: str
    
    # 时间配置
    time_config: TimeSimulationConfig = field(default_factory=TimeSimulationConfig)
    
    # Agent配置列表
    agent_configs: List[AgentActivityConfig] = field(default_factory=list)
    
    # 事件配置
    event_config: EventConfig = field(default_factory=EventConfig)
    
    # 平台配置
    twitter_config: Optional[PlatformConfig] = None
    reddit_config: Optional[PlatformConfig] = None
    
    # LLM配置
    llm_model: str = ""
    llm_base_url: str = ""
    # {profile_id: {"label", "model", "count"}}
    llm_models: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # 人设原型混合 {"preset", "seed"}
    persona_mix: Optional[Dict[str, Any]] = None
    
    # 生成元数据
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    generation_reasoning: str = ""  # LLM的推理说明
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        time_dict = asdict(self.time_config)
        return {
            "simulation_id": self.simulation_id,
            "project_id": self.project_id,
            "graph_id": self.graph_id,
            "simulation_requirement": self.simulation_requirement,
            "time_config": time_dict,
            "agent_configs": [asdict(a) for a in self.agent_configs],
            "event_config": asdict(self.event_config),
            "twitter_config": asdict(self.twitter_config) if self.twitter_config else None,
            "reddit_config": asdict(self.reddit_config) if self.reddit_config else None,
            "llm_model": self.llm_model,
            "llm_base_url": self.llm_base_url,
            "llm_models": self.llm_models,
            "persona_mix": self.persona_mix,
            "generated_at": self.generated_at,
            "generation_reasoning": self.generation_reasoning,
        }
    
    def to_json(self, indent: int = 2) -> str:
        """转换为JSON字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class SimulationConfigGenerator:
    """
    模拟配置智能生成器
    
    使用LLM分析模拟需求、文档内容、图谱实体信息，
    自动生成最佳的模拟参数配置
    
    采用分步生成策略：
    1. 生成时间配置和事件配置（轻量级）
    2. 分批生成Agent配置（每批10-20个）
    3. 生成平台配置
    """
    
    # 上下文最大字符数
    MAX_CONTEXT_LENGTH = 50000
    # 每批生成的Agent数量
    AGENTS_PER_BATCH = 15
    
    # 各步骤的上下文截断长度（字符数）
    TIME_CONFIG_CONTEXT_LENGTH = 10000   # 时间配置
    EVENT_CONFIG_CONTEXT_LENGTH = 8000   # 事件配置
    ENTITY_SUMMARY_LENGTH = 300          # 实体摘要
    AGENT_SUMMARY_LENGTH = 300           # Agent配置中的实体摘要
    ENTITIES_PER_TYPE_DISPLAY = 20       # 每类实体显示数量

    # 各步骤输出 token 上限
    TIME_CONFIG_MAX_TOKENS = 1024
    EVENT_CONFIG_MAX_TOKENS = 3072
    AGENT_BATCH_MAX_TOKENS = 4096
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None
    ):
        if api_key or base_url or model_name:
            # 显式参数：临时 openai_compatible profile（与 LLMClient 相同语义）
            self.profile = LLMProfile(
                id="custom",
                kind="openai_compatible",
                label="Custom",
                model=model_name or Config.LLM_MODEL_NAME,
                base_url=base_url or Config.LLM_BASE_URL,
                api_key=api_key or Config.LLM_API_KEY,
            )
        else:
            self.profile = resolve_profile(LLM_STAGE)

        if self.profile.kind == "openai_compatible" and not self.profile.api_key:
            raise ValueError(t("errors.llmApiKeyMissing"))

        self.api_key = self.profile.api_key
        self.base_url = self.profile.base_url
        self.model_name = self.profile.model
        self.region = select_region()
        self.region_profile = REGION_PROFILES[self.region]
    
    def generate_config(
        self,
        simulation_id: str,
        project_id: str,
        graph_id: str,
        simulation_requirement: str,
        document_text: str,
        entities: List[EntityNode],
        enable_twitter: bool = True,
        enable_reddit: bool = True,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        persona_mix: Optional[Dict[str, Any]] = None,
    ) -> SimulationParameters:
        """
        智能生成完整的模拟配置（分步生成）
        
        Args:
            simulation_id: 模拟ID
            project_id: 项目ID
            graph_id: 图谱ID
            simulation_requirement: 模拟需求描述
            document_text: 原始文档内容
            entities: 过滤后的实体列表
            enable_twitter: 是否启用Twitter
            enable_reddit: 是否启用Reddit
            progress_callback: 进度回调函数(current_step, total_steps, message)
            persona_mix: 人设原型混合 {"preset", "seed"}；None 表示不启用原型
            
        Returns:
            SimulationParameters: 完整的模拟参数
        """
        logger.info(f"开始智能生成模拟配置: simulation_id={simulation_id}, 实体数={len(entities)}")

        # 地区作息按当前 locale 选择（后台线程需已 set_locale）
        self.region = select_region()
        self.region_profile = REGION_PROFILES[self.region]

        # 人设原型分配（先于 LLM 调用，无效 preset 立即失败）
        persona_mix = normalize_persona_mix(persona_mix, simulation_id)
        agent_ids = list(range(len(entities)))
        archetype_by_agent = assign_archetypes(agent_ids, persona_mix["preset"], persona_mix["seed"])
        
        # 计算总步骤数
        num_batches = math.ceil(len(entities) / self.AGENTS_PER_BATCH)
        total_steps = 3 + num_batches  # 时间配置 + 事件配置 + N批Agent + 平台配置
        current_step = 0
        
        def report_progress(step: int, message: str):
            nonlocal current_step
            current_step = step
            if progress_callback:
                progress_callback(step, total_steps, message)
            logger.info(f"[{step}/{total_steps}] {message}")
        
        # 1. 构建基础上下文信息
        context = self._build_context(
            simulation_requirement=simulation_requirement,
            document_text=document_text,
            entities=entities
        )
        
        reasoning_parts = []
        
        # ========== 步骤1: 生成时间配置 ==========
        report_progress(1, t('progress.generatingTimeConfig'))
        num_entities = len(entities)
        time_config_result = self._generate_time_config(context, num_entities)
        time_config = self._parse_time_config(time_config_result, num_entities)
        reasoning_parts.append(f"{t('progress.timeConfigLabel')}: {time_config_result.get('reasoning', t('common.success'))}")
        
        # ========== 步骤2: 生成事件配置 ==========
        report_progress(2, t('progress.generatingEventConfig'))
        event_config_result = self._generate_event_config(context, simulation_requirement, entities)
        event_config = self._parse_event_config(event_config_result)
        reasoning_parts.append(f"{t('progress.eventConfigLabel')}: {event_config_result.get('reasoning', t('common.success'))}")
        
        # ========== 步骤3-N: 分批生成Agent配置 ==========
        all_agent_configs = []
        for batch_idx in range(num_batches):
            start_idx = batch_idx * self.AGENTS_PER_BATCH
            end_idx = min(start_idx + self.AGENTS_PER_BATCH, len(entities))
            batch_entities = entities[start_idx:end_idx]
            
            report_progress(
                3 + batch_idx,
                t('progress.generatingAgentConfig', start=start_idx + 1, end=end_idx, total=len(entities))
            )
            
            batch_configs = self._generate_agent_configs_batch(
                context=context,
                entities=batch_entities,
                start_idx=start_idx,
                simulation_requirement=simulation_requirement,
                archetype_by_agent=archetype_by_agent,
            )
            all_agent_configs.extend(batch_configs)

        # 原型行为参数覆盖 + 每个Agent的LLM profile
        self._apply_archetypes(all_agent_configs, archetype_by_agent)
        llm_models, agent_profile = self._assign_llm_profiles(all_agent_configs, simulation_id)
        
        reasoning_parts.append(t('progress.agentConfigResult', count=len(all_agent_configs)))
        
        # ========== 为初始帖子分配发布者 Agent ==========
        logger.info("为初始帖子分配合适的发布者 Agent...")
        event_config = self._assign_initial_post_agents(event_config, all_agent_configs)
        assigned_count = len([p for p in event_config.initial_posts if p.get("poster_agent_id") is not None])
        reasoning_parts.append(t('progress.postAssignResult', count=assigned_count))
        
        # ========== 最后一步: 生成平台配置 ==========
        report_progress(total_steps, t('progress.generatingPlatformConfig'))
        twitter_config = None
        reddit_config = None
        
        if enable_twitter:
            twitter_config = PlatformConfig(
                platform="twitter",
                recency_weight=0.4,
                popularity_weight=0.3,
                relevance_weight=0.3,
                viral_threshold=10,
                echo_chamber_strength=0.5
            )
        
        if enable_reddit:
            reddit_config = PlatformConfig(
                platform="reddit",
                recency_weight=0.3,
                popularity_weight=0.4,
                relevance_weight=0.3,
                viral_threshold=15,
                echo_chamber_strength=0.6
            )
        
        # 构建最终参数
        params = SimulationParameters(
            simulation_id=simulation_id,
            project_id=project_id,
            graph_id=graph_id,
            simulation_requirement=simulation_requirement,
            time_config=time_config,
            agent_configs=all_agent_configs,
            event_config=event_config,
            twitter_config=twitter_config,
            reddit_config=reddit_config,
            llm_model=agent_profile.model,
            llm_base_url=agent_profile.base_url or "",
            llm_models=llm_models,
            persona_mix=persona_mix,
            generation_reasoning=" | ".join(reasoning_parts)
        )
        
        logger.info(f"模拟配置生成完成: {len(params.agent_configs)} 个Agent配置")
        
        return params

    def _apply_archetypes(
        self,
        agent_configs: List[AgentActivityConfig],
        archetype_by_agent: Dict[int, Optional[str]],
    ) -> None:
        """Override persona-driven behavior (activity, hours, delay, sentiment) from the assigned archetype."""
        for cfg in agent_configs:
            archetype_id = archetype_by_agent.get(cfg.agent_id)
            cfg.archetype = archetype_id
            if not archetype_id:
                continue
            overrides = archetype_activity_overrides(archetype_id)
            cfg.activity_level = overrides["activity_level"]
            cfg.active_hours = list(overrides["active_hours"])
            delay_min, delay_max = overrides["response_delay_minutes"]
            cfg.response_delay_min = delay_min
            cfg.response_delay_max = delay_max
            cfg.sentiment_bias = overrides["sentiment_bias"]

    def _assign_llm_profiles(
        self,
        agent_configs: List[AgentActivityConfig],
        simulation_id: str,
    ) -> "tuple[Dict[str, Dict[str, Any]], LLMProfile]":
        """Assign each agent a profile from the agent pool (seeded by simulation id) and summarize the mix."""
        settings: LLMSettings = load_settings()
        agent_ids = [cfg.agent_id for cfg in agent_configs]
        if settings.agent_pool:
            mapping = assign_agent_profiles(agent_ids, seed=simulation_id, settings=settings)
            profiles = {pid: settings.profile(pid) for pid in set(mapping.values())}
            route_profile = resolve_profile("simulation_agent", settings)
        else:
            # 仅显式参数构造（无环境 profile）：所有Agent使用该 profile
            mapping = {agent_id: self.profile.id for agent_id in agent_ids}
            profiles = {self.profile.id: self.profile}
            route_profile = self.profile

        llm_models: Dict[str, Dict[str, Any]] = {}
        for cfg in agent_configs:
            pid = mapping[cfg.agent_id]
            cfg.llm_profile = pid
            entry = llm_models.setdefault(
                pid, {"label": profiles[pid].label, "model": profiles[pid].model, "count": 0}
            )
            entry["count"] += 1
        return llm_models, route_profile
    
    def _build_context(
        self,
        simulation_requirement: str,
        document_text: str,
        entities: List[EntityNode]
    ) -> str:
        """构建LLM上下文，截断到最大长度"""
        
        # 实体摘要
        entity_summary = self._summarize_entities(entities)
        
        # 构建上下文
        context_parts = [
            f"## Simulation requirement\n{simulation_requirement}",
            f"\n## Entities ({len(entities)})\n{entity_summary}",
        ]
        
        current_length = sum(len(p) for p in context_parts)
        remaining_length = self.MAX_CONTEXT_LENGTH - current_length - 500  # 留500字符余量
        
        if remaining_length > 0 and document_text:
            doc_text = document_text[:remaining_length]
            if len(document_text) > remaining_length:
                doc_text += "\n...(document truncated)"
            context_parts.append(f"\n## Source document\n{doc_text}")
        
        return "\n".join(context_parts)
    
    def _summarize_entities(self, entities: List[EntityNode]) -> str:
        """生成实体摘要"""
        lines = []
        
        # 按类型分组
        by_type: Dict[str, List[EntityNode]] = {}
        for e in entities:
            t = e.get_entity_type() or "Unknown"
            if t not in by_type:
                by_type[t] = []
            by_type[t].append(e)
        
        for entity_type, type_entities in by_type.items():
            lines.append(f"\n### {entity_type} ({len(type_entities)})")
            # 使用配置的显示数量和摘要长度
            display_count = self.ENTITIES_PER_TYPE_DISPLAY
            summary_len = self.ENTITY_SUMMARY_LENGTH
            for e in type_entities[:display_count]:
                summary_preview = (e.summary[:summary_len] + "...") if len(e.summary) > summary_len else e.summary
                lines.append(f"- {e.name}: {summary_preview}")
            if len(type_entities) > display_count:
                lines.append(f"  ... and {len(type_entities) - display_count} more")
        
        return "\n".join(lines)
    
    def _call_llm_with_retry(self, prompt: str, system_prompt: str, max_tokens: int) -> Dict[str, Any]:
        """带重试的LLM调用（stage=config），包含JSON修复逻辑"""
        max_attempts = 3
        last_error = None
        response_format: Optional[Dict[str, str]] = {"type": "json_object"}
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        attempt = 0
        while attempt < max_attempts:
            try:
                response = create_completion(
                    self.profile,
                    stage=LLM_STAGE,
                    messages=messages,
                    response_format=response_format,
                    temperature=0.7 - (attempt * 0.1),  # 每次重试降低温度
                    max_tokens=max_tokens,
                )
            except Exception as e:
                if response_format is not None and is_response_format_unsupported(e):
                    # provider 明确不支持 JSON 模式：降级一次，不计入尝试次数
                    logger.warning("LLM provider rejected response_format; retrying with prompt-only JSON")
                    response_format = None
                    continue
                logger.warning(f"LLM调用失败 (attempt {attempt+1}): {str(e)[:80]}")
                last_error = e
                attempt += 1
                if attempt < max_attempts:
                    time.sleep(2 * attempt)
                continue

            content = extract_chat_completion_text(response)
            finish_reason = response.choices[0].finish_reason

            # 检查是否被截断
            if finish_reason == 'length':
                logger.warning(f"LLM输出被截断 (attempt {attempt+1})")
                content = self._fix_truncated_json(content)

            # 尝试解析JSON
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                logger.warning(f"JSON解析失败 (attempt {attempt+1}): {str(e)[:80]}")

                # 尝试修复JSON
                fixed = self._try_fix_config_json(content)
                if fixed:
                    return fixed

                last_error = e
                attempt += 1

        raise last_error or Exception("LLM调用失败")
    
    def _fix_truncated_json(self, content: str) -> str:
        """修复被截断的JSON"""
        content = content.strip()
        
        # 计算未闭合的括号
        open_braces = content.count('{') - content.count('}')
        open_brackets = content.count('[') - content.count(']')
        
        # 检查是否有未闭合的字符串
        if content and content[-1] not in '",}]':
            content += '"'
        
        # 闭合括号
        content += ']' * open_brackets
        content += '}' * open_braces
        
        return content
    
    def _try_fix_config_json(self, content: str) -> Optional[Dict[str, Any]]:
        """尝试修复配置JSON"""
        import re
        
        # 修复被截断的情况
        content = self._fix_truncated_json(content)
        
        # 提取JSON部分
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            json_str = json_match.group()
            
            # 移除字符串中的换行符
            def fix_string(match):
                s = match.group(0)
                s = s.replace('\n', ' ').replace('\r', ' ')
                s = re.sub(r'\s+', ' ', s)
                return s
            
            json_str = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', fix_string, json_str)
            
            try:
                return json.loads(json_str)
            except:
                # 尝试移除所有控制字符
                json_str = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', json_str)
                json_str = re.sub(r'\s+', ' ', json_str)
                try:
                    return json.loads(json_str)
                except:
                    pass
        
        return None
    
    TIME_CONFIG_SYSTEM_PROMPT = """You are a social media simulation expert. Design the time configuration for a multi-agent social media simulation.

Rules:
- Infer the time zone and daily routine of the target population from the scenario. If the scenario does not name a region, follow the reference daily routine provided in the request.
- General pattern: very low activity late at night, rising in the morning, moderate during working hours, highest in the evening peak.
- Adapt the windows to the event and the participants: students may peak late in the evening, media are active most of the day, official institutions mostly during working hours, and breaking news can keep people talking late at night (shorten off_peak_hours then).
- Hours are local integers 0-23.

Return only a JSON object (no markdown) with exactly these fields:
- total_simulation_hours (int): 24-168; short for breaking events, longer for ongoing topics
- minutes_per_round (int): 30-120, 60 recommended
- agents_per_hour_min (int): minimum agents activated per hour, within the allowed range given in the request
- agents_per_hour_max (int): maximum agents activated per hour, within the allowed range given in the request
- peak_hours (int array): peak activity hours
- off_peak_hours (int array): lowest activity hours, usually late night
- morning_hours (int array): morning hours
- work_hours (int array): working hours
- reasoning (string): a short explanation of the choices

Example:
{"total_simulation_hours": 72, "minutes_per_round": 60, "agents_per_hour_min": 5, "agents_per_hour_max": 50, "peak_hours": [19, 20, 21, 22], "off_peak_hours": [0, 1, 2, 3, 4, 5], "morning_hours": [6, 7, 8], "work_hours": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18], "reasoning": "..."}"""

    def _region_routine_text(self) -> str:
        """Reference daily routine of the selected region (dynamic, goes in the user message)."""
        rp = self.region_profile
        m = rp["activity_multipliers"]
        tz = rp["timezone"] if rp["utc_offset"] is None else f"{rp['timezone']}, UTC{rp['utc_offset']}"
        lines = [
            f"## Reference daily routine (region {self.region}, time zone {tz})",
            f"- off-peak / almost no activity (multiplier {m['dead']}): {rp['dead_hours']}",
            f"- morning (multiplier {m['morning']}): {rp['morning_hours']}",
            f"- working hours (multiplier {m['work']}): {rp['work_hours']}",
            f"- evening peak (multiplier {m['peak']}): {rp['peak_hours']}",
            f"- late night (multiplier {m['night']}): {rp['night_hours']}",
        ]
        for name, hours in rp["routine_windows"].items():
            lines.append(f"- {name.replace('_', ' ')} window (activity bump): {hours}")
        return "\n".join(lines)

    def _generate_time_config(self, context: str, num_entities: int) -> Dict[str, Any]:
        """生成时间配置"""
        # 使用配置的上下文截断长度
        context_truncated = context[:self.TIME_CONFIG_CONTEXT_LENGTH]
        
        # 计算最大允许值（90%的agent数）
        max_agents_allowed = max(1, int(num_entities * 0.9))
        
        prompt = f"""{context_truncated}

{self._region_routine_text()}

## Constraints
- Total agents: {num_entities}
- agents_per_hour_min and agents_per_hour_max must be between 1 and {max_agents_allowed}

Generate the time configuration JSON."""

        system_prompt = f"{self.TIME_CONFIG_SYSTEM_PROMPT}\n\n{get_language_instruction()}"

        try:
            return self._call_llm_with_retry(prompt, system_prompt, self.TIME_CONFIG_MAX_TOKENS)
        except Exception as e:
            logger.warning(f"时间配置LLM生成失败: {e}, 使用默认配置")
            return self._get_default_time_config(num_entities)
    
    def _get_default_time_config(self, num_entities: int) -> Dict[str, Any]:
        """获取默认时间配置（当前地区作息）"""
        rp = self.region_profile
        return {
            "total_simulation_hours": 72,
            "minutes_per_round": 60,  # 每轮1小时，加快时间流速
            "agents_per_hour_min": max(1, num_entities // 15),
            "agents_per_hour_max": max(5, num_entities // 5),
            "peak_hours": list(rp["peak_hours"]),
            "off_peak_hours": list(rp["dead_hours"]),
            "morning_hours": list(rp["morning_hours"]),
            "work_hours": list(rp["work_hours"]),
            "reasoning": t('progress.defaultTimeConfigReasoning', region=self.region)
        }
    
    def _parse_time_config(self, result: Dict[str, Any], num_entities: int) -> TimeSimulationConfig:
        """解析时间配置结果，并验证agents_per_hour值不超过总agent数"""
        rp = self.region_profile
        multipliers = rp["activity_multipliers"]
        # 获取原始值
        agents_per_hour_min = result.get("agents_per_hour_min", max(1, num_entities // 15))
        agents_per_hour_max = result.get("agents_per_hour_max", max(5, num_entities // 5))
        
        # 验证并修正：确保不超过总agent数
        if agents_per_hour_min > num_entities:
            logger.warning(f"agents_per_hour_min ({agents_per_hour_min}) 超过总Agent数 ({num_entities})，已修正")
            agents_per_hour_min = max(1, num_entities // 10)
        
        if agents_per_hour_max > num_entities:
            logger.warning(f"agents_per_hour_max ({agents_per_hour_max}) 超过总Agent数 ({num_entities})，已修正")
            agents_per_hour_max = max(agents_per_hour_min + 1, num_entities // 2)
        
        # 确保 min < max
        if agents_per_hour_min >= agents_per_hour_max:
            agents_per_hour_min = max(1, agents_per_hour_max // 2)
            logger.warning(f"agents_per_hour_min >= max，已修正为 {agents_per_hour_min}")
        
        return TimeSimulationConfig(
            total_simulation_hours=result.get("total_simulation_hours", 72),
            minutes_per_round=result.get("minutes_per_round", 60),  # 默认每轮1小时
            agents_per_hour_min=agents_per_hour_min,
            agents_per_hour_max=agents_per_hour_max,
            peak_hours=result.get("peak_hours", list(rp["peak_hours"])),
            off_peak_hours=result.get("off_peak_hours", list(rp["dead_hours"])),
            off_peak_activity_multiplier=multipliers["dead"],
            morning_hours=result.get("morning_hours", list(rp["morning_hours"])),
            morning_activity_multiplier=multipliers["morning"],
            work_hours=result.get("work_hours", list(rp["work_hours"])),
            work_activity_multiplier=multipliers["work"],
            peak_activity_multiplier=multipliers["peak"],
            region=self.region,
            timezone=rp["timezone"],
            routine_windows={k: list(v) for k, v in rp["routine_windows"].items()},
        )
    
    EVENT_CONFIG_SYSTEM_PROMPT = """You are a public opinion analyst. Design the event configuration for a social media simulation.

Tasks:
- Extract hot topic keywords.
- Describe how public opinion is likely to develop.
- Write the initial posts that start the simulation. Every post must name its poster_type.

Rules:
- poster_type MUST exactly match one of the available entity types listed in the request, written in English PascalCase as given, so the post can be assigned to a fitting agent. For example, official statements come from Official/University types, news from MediaOutlet, student opinions from Student.
- Only content, narrative_direction, hot_topics and reasoning are natural-language text; JSON field names stay unchanged.

Return only a JSON object (no markdown):
{"hot_topics": ["keyword1", "keyword2"], "narrative_direction": "<how opinion develops>", "initial_posts": [{"content": "<post text>", "poster_type": "<one of the available entity types>"}], "reasoning": "<short explanation>"}"""

    def _generate_event_config(
        self, 
        context: str, 
        simulation_requirement: str,
        entities: List[EntityNode]
    ) -> Dict[str, Any]:
        """生成事件配置"""
        
        # 获取可用的实体类型列表，供 LLM 参考
        entity_types_available = list(set(
            e.get_entity_type() or "Unknown" for e in entities
        ))
        
        # 为每种类型列出代表性实体名称
        type_examples = {}
        for e in entities:
            etype = e.get_entity_type() or "Unknown"
            if etype not in type_examples:
                type_examples[etype] = []
            if len(type_examples[etype]) < 3:
                type_examples[etype].append(e.name)
        
        type_info = "\n".join([
            f"- {t}: {', '.join(examples)}" 
            for t, examples in type_examples.items()
        ])
        
        # 使用配置的上下文截断长度
        context_truncated = context[:self.EVENT_CONFIG_CONTEXT_LENGTH]
        
        prompt = f"""{context_truncated}

## Available entity types (with examples)
{type_info}

Generate the event configuration JSON."""

        system_prompt = f"{self.EVENT_CONFIG_SYSTEM_PROMPT}\n\n{get_language_instruction()}"

        try:
            return self._call_llm_with_retry(prompt, system_prompt, self.EVENT_CONFIG_MAX_TOKENS)
        except Exception as e:
            logger.warning(f"事件配置LLM生成失败: {e}, 使用默认配置")
            return {
                "hot_topics": [],
                "narrative_direction": "",
                "initial_posts": [],
                "reasoning": t('progress.defaultEventConfigReasoning')
            }
    
    def _parse_event_config(self, result: Dict[str, Any]) -> EventConfig:
        """解析事件配置结果"""
        return EventConfig(
            initial_posts=result.get("initial_posts", []),
            scheduled_events=[],
            hot_topics=result.get("hot_topics", []),
            narrative_direction=result.get("narrative_direction", "")
        )
    
    def _assign_initial_post_agents(
        self,
        event_config: EventConfig,
        agent_configs: List[AgentActivityConfig]
    ) -> EventConfig:
        """
        为初始帖子分配合适的发布者 Agent
        
        根据每个帖子的 poster_type 匹配最合适的 agent_id
        """
        if not event_config.initial_posts:
            return event_config
        
        # 按实体类型建立 agent 索引
        agents_by_type: Dict[str, List[AgentActivityConfig]] = {}
        for agent in agent_configs:
            etype = agent.entity_type.lower()
            if etype not in agents_by_type:
                agents_by_type[etype] = []
            agents_by_type[etype].append(agent)
        
        # 类型映射表（处理 LLM 可能输出的不同格式）
        type_aliases = {
            "official": ["official", "university", "governmentagency", "government"],
            "university": ["university", "official"],
            "mediaoutlet": ["mediaoutlet", "media"],
            "student": ["student", "person"],
            "professor": ["professor", "expert", "teacher"],
            "alumni": ["alumni", "person"],
            "organization": ["organization", "ngo", "company", "group"],
            "person": ["person", "student", "alumni"],
        }
        
        # 记录每种类型已使用的 agent 索引，避免重复使用同一个 agent
        used_indices: Dict[str, int] = {}
        
        updated_posts = []
        for post in event_config.initial_posts:
            poster_type = post.get("poster_type", "").lower()
            content = post.get("content", "")
            
            # 尝试找到匹配的 agent
            matched_agent_id = None
            
            # 1. 直接匹配
            if poster_type in agents_by_type:
                agents = agents_by_type[poster_type]
                idx = used_indices.get(poster_type, 0) % len(agents)
                matched_agent_id = agents[idx].agent_id
                used_indices[poster_type] = idx + 1
            else:
                # 2. 使用别名匹配
                for alias_key, aliases in type_aliases.items():
                    if poster_type in aliases or alias_key == poster_type:
                        for alias in aliases:
                            if alias in agents_by_type:
                                agents = agents_by_type[alias]
                                idx = used_indices.get(alias, 0) % len(agents)
                                matched_agent_id = agents[idx].agent_id
                                used_indices[alias] = idx + 1
                                break
                    if matched_agent_id is not None:
                        break
            
            # 3. 如果仍未找到，使用影响力最高的 agent
            if matched_agent_id is None:
                logger.warning(f"未找到类型 '{poster_type}' 的匹配 Agent，使用影响力最高的 Agent")
                if agent_configs:
                    # 按影响力排序，选择影响力最高的
                    sorted_agents = sorted(agent_configs, key=lambda a: a.influence_weight, reverse=True)
                    matched_agent_id = sorted_agents[0].agent_id
                else:
                    matched_agent_id = 0
            
            updated_posts.append({
                "content": content,
                "poster_type": post.get("poster_type", "Unknown"),
                "poster_agent_id": matched_agent_id
            })
            
            logger.info(f"初始帖子分配: poster_type='{poster_type}' -> agent_id={matched_agent_id}")
        
        event_config.initial_posts = updated_posts
        return event_config
    
    AGENT_CONFIG_SYSTEM_PROMPT = """You are a social media behavior analyst. Generate a social media activity configuration for each entity in the request.

Guidelines (adapt to the scenario; active hours follow the reference daily routine of the target population given in the request):
- Official institutions (University/GovernmentAgency): low activity (0.1-0.3), active during local working hours, slow responses (60-240 minutes), high influence (2.5-3.0)
- Media (MediaOutlet): medium activity (0.4-0.6), active most of the waking day, fast responses (5-30 minutes), high influence (2.0-2.5)
- Individuals (Student/Person/Alumni): high activity (0.6-0.9), mainly in the evening peak and commute/lunch breaks, fast responses (1-15 minutes), low influence (0.8-1.2)
- Public figures/experts: medium activity (0.4-0.6), medium-high influence (1.5-2.0)
- When an entity has an "archetype", keep stance, posting and commenting frequency consistent with it.

Rules:
- agent_id must equal the input agent_id.
- stance MUST be one of the English strings: "supportive", "opposing", "neutral", "observer".
- Keep all JSON field names and numeric values as specified; hours are local integers 0-23.

Return only a JSON object (no markdown):
{"agent_configs": [{"agent_id": 0, "activity_level": 0.5, "posts_per_hour": 0.5, "comments_per_hour": 1.0, "active_hours": [8, 12, 19, 20, 21], "response_delay_min": 5, "response_delay_max": 60, "sentiment_bias": 0.0, "stance": "neutral", "influence_weight": 1.0}]}
Field meanings: activity_level 0.0-1.0; posts_per_hour and comments_per_hour are expected counts per hour; response_delay_min/max in minutes; sentiment_bias -1.0 (negative) to 1.0 (positive)."""

    def _generate_agent_configs_batch(
        self,
        context: str,
        entities: List[EntityNode],
        start_idx: int,
        simulation_requirement: str,
        archetype_by_agent: Optional[Dict[int, Optional[str]]] = None,
    ) -> List[AgentActivityConfig]:
        """分批生成Agent配置"""
        
        # 构建实体信息（使用配置的摘要长度）
        entity_list = []
        summary_len = self.AGENT_SUMMARY_LENGTH
        archetype_by_agent = archetype_by_agent or {}
        for i, e in enumerate(entities):
            item = {
                "agent_id": start_idx + i,
                "entity_name": e.name,
                "entity_type": e.get_entity_type() or "Unknown",
                "summary": e.summary[:summary_len] if e.summary else ""
            }
            archetype_id = archetype_by_agent.get(start_idx + i)
            if archetype_id:
                item["archetype"] = get_archetype(archetype_id)["traits_en"]
            entity_list.append(item)
        
        prompt = f"""## Simulation requirement
{simulation_requirement}

{self._region_routine_text()}

## Entities
```json
{json.dumps(entity_list, ensure_ascii=False, indent=2)}
```

Generate the activity configuration JSON for every entity above."""

        system_prompt = f"{self.AGENT_CONFIG_SYSTEM_PROMPT}\n\n{get_language_instruction()}"

        try:
            result = self._call_llm_with_retry(prompt, system_prompt, self.AGENT_BATCH_MAX_TOKENS)
            llm_configs = {cfg["agent_id"]: cfg for cfg in result.get("agent_configs", [])}
        except Exception as e:
            logger.warning(f"Agent配置批次LLM生成失败: {e}, 使用规则生成")
            llm_configs = {}
        
        # 构建AgentActivityConfig对象
        configs = []
        for i, entity in enumerate(entities):
            agent_id = start_idx + i
            cfg = llm_configs.get(agent_id, {})
            
            # 如果LLM没有生成，使用规则生成
            if not cfg:
                cfg = self._generate_agent_config_by_rule(entity)
            
            config = AgentActivityConfig(
                agent_id=agent_id,
                entity_uuid=entity.uuid,
                entity_name=entity.name,
                entity_type=entity.get_entity_type() or "Unknown",
                activity_level=cfg.get("activity_level", 0.5),
                posts_per_hour=cfg.get("posts_per_hour", 0.5),
                comments_per_hour=cfg.get("comments_per_hour", 1.0),
                active_hours=cfg.get("active_hours", list(range(9, 23))),
                response_delay_min=cfg.get("response_delay_min", 5),
                response_delay_max=cfg.get("response_delay_max", 60),
                sentiment_bias=cfg.get("sentiment_bias", 0.0),
                stance=cfg.get("stance", "neutral"),
                influence_weight=cfg.get("influence_weight", 1.0)
            )
            configs.append(config)
        
        return configs
    
    def _rule_hours(self, kind: str) -> List[int]:
        return list(self.region_profile["rule_active_hours"][kind])

    def _generate_agent_config_by_rule(self, entity: EntityNode) -> Dict[str, Any]:
        """基于规则生成单个Agent配置（当前地区作息）"""
        entity_type = (entity.get_entity_type() or "Unknown").lower()
        
        if entity_type in ["university", "governmentagency", "ngo"]:
            # 官方机构：工作时间活动，低频率，高影响力
            return {
                "activity_level": 0.2,
                "posts_per_hour": 0.1,
                "comments_per_hour": 0.05,
                "active_hours": self._rule_hours("institution"),
                "response_delay_min": 60,
                "response_delay_max": 240,
                "sentiment_bias": 0.0,
                "stance": "neutral",
                "influence_weight": 3.0
            }
        elif entity_type in ["mediaoutlet"]:
            # 媒体：全天活动，中等频率，高影响力
            return {
                "activity_level": 0.5,
                "posts_per_hour": 0.8,
                "comments_per_hour": 0.3,
                "active_hours": self._rule_hours("media"),
                "response_delay_min": 5,
                "response_delay_max": 30,
                "sentiment_bias": 0.0,
                "stance": "observer",
                "influence_weight": 2.5
            }
        elif entity_type in ["professor", "expert", "official"]:
            # 专家/教授：工作+晚间活动，中等频率
            return {
                "activity_level": 0.4,
                "posts_per_hour": 0.3,
                "comments_per_hour": 0.5,
                "active_hours": self._rule_hours("expert"),
                "response_delay_min": 15,
                "response_delay_max": 90,
                "sentiment_bias": 0.0,
                "stance": "neutral",
                "influence_weight": 2.0
            }
        elif entity_type in ["student"]:
            # 学生：晚间为主，高频率
            return {
                "activity_level": 0.8,
                "posts_per_hour": 0.6,
                "comments_per_hour": 1.5,
                "active_hours": self._rule_hours("student"),
                "response_delay_min": 1,
                "response_delay_max": 15,
                "sentiment_bias": 0.0,
                "stance": "neutral",
                "influence_weight": 0.8
            }
        elif entity_type in ["alumni"]:
            # 校友：晚间为主
            return {
                "activity_level": 0.6,
                "posts_per_hour": 0.4,
                "comments_per_hour": 0.8,
                "active_hours": self._rule_hours("alumni"),
                "response_delay_min": 5,
                "response_delay_max": 30,
                "sentiment_bias": 0.0,
                "stance": "neutral",
                "influence_weight": 1.0
            }
        else:
            # 普通人：晚间高峰
            return {
                "activity_level": 0.7,
                "posts_per_hour": 0.5,
                "comments_per_hour": 1.2,
                "active_hours": self._rule_hours("person"),
                "response_delay_min": 2,
                "response_delay_max": 20,
                "sentiment_bias": 0.0,
                "stance": "neutral",
                "influence_weight": 1.0
            }
    

