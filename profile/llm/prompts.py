# === 意图提取 Prompt ===

INTENT_SYSTEM = """你是一个用户行为分析专家。你的任务是分析用户与AI助手的对话记录，提取用户的真实意图。"""

INTENT_PROMPT = """分析以下用户与AI助手的对话记录，提取用户意图。

对话内容：{content}
执行动作：{actions}

请返回JSON（不要包含markdown代码块标记）：
{{
  "category": "从以下选择：技术探索|项目实施|问题排查|学习了解|生活诉求|其他",
  "summary": "一句话概括用户意图（20字以内）",
  "keywords": ["关键词1", "关键词2", "关键词3"],
  "sentiment": "从以下选择：positive|neutral|frustrated|curious|excited",
  "priority": "从以下选择：high|medium|low",
  "project": "关联的项目名称，无则填null"
}}"""


# === trae 画像生成 Prompt ===

TRAE_PROFILE_SYSTEM = """你是一个个人画像分析师。你通过分析用户与AI助手的对话记录，生成用户的方向真实度和决策行动模式画像。"""

TRAE_PROFILE_PROMPT = """基于以下用户在 trae（AI编程助手）中的对话意图数据，生成画像分析。

用户声称的方向：主方向={primary_direction}，次方向={secondary_direction}

对话意图数据（最近一周）：
{intents_json}

请返回JSON（不要包含markdown代码块标记）：
{{
  "direction_analysis": {{
    "claimed_direction": "用户声称的方向",
    "actual_top_topics": ["实际讨论最多的主题1", "主题2", "..."],
    "direction_alignment": "高|中|低",
    "drift_description": "方向漂移的具体描述（50字以内）",
    "agent_ratio": "Agent相关讨论占比百分比数字",
    "memory_ratio": "Memory相关讨论占比百分比数字"
  }},
  "decision_pattern": {{
    "research_ratio": "调研类行动占比",
    "build_ratio": "动手类行动占比",
    "discuss_ratio": "讨论类行动占比",
    "pattern": "先调研后动手|先动手后查|混合",
    "idea_to_action_gap": "想法到动手的平均间隔天数描述"
  }},
  "key_findings": ["关键发现1", "关键发现2", "关键发现3"]
}}"""


# === marvis 画像生成 Prompt ===

MARVIS_PROFILE_SYSTEM = """你是一个个人画像分析师。你通过分析用户与AI助手的对话记录，生成用户的日常诉求画像。"""

MARVIS_PROFILE_PROMPT = """基于以下用户在 marvis（AI助手）中的对话意图数据，生成日常诉求画像。

对话意图数据（最近一周）：
{intents_json}

请返回JSON（不要包含markdown代码块标记）：
{{
  "daily_needs": {{
    "top_needs": ["最主要的诉求1", "诉求2", "..."],
    "need_categories": {{
      "技术问题": "百分比数字",
      "工具使用": "百分比数字",
      "生活诉求": "百分比数字",
      "其他": "百分比数字"
    }}
  }},
  "interaction_pattern": {{
    "primary_use_case": "用户使用marvis的主要场景描述",
    "complexity_level": "简单查询|中等复杂|深度讨论"
  }},
  "key_findings": ["关键发现1", "关键发现2"]
}}"""


# === 活跃时段画像（不使用LLM，此处仅作占位） ===
# 活跃时段分析保留规则方式，不需要 LLM prompt


# === 综合画像融合 Prompt ===

GLOBAL_PROFILE_SYSTEM = """你是一个个人画像分析师。你的任务是将多个渠道的用户画像数据融合为一份综合个人画像。"""

GLOBAL_PROFILE_PROMPT = """基于以下各渠道的用户画像数据，生成一份综合个人画像。

各渠道画像数据：
{channels_json}

用户声称的方向：主方向={primary_direction}，次方向={secondary_direction}

请返回JSON（不要包含markdown代码块标记）：
{{
  "direction_truth": {{
    "claimed": "用户声称的方向",
    "actual_focus": "实际关注的方向",
    "alignment": "高|中|低",
    "description": "方向真实度的综合描述（100字以内）"
  }},
  "knowledge_interest": {{
    "top_interests": ["兴趣1", "兴趣2", "..."],
    "depth_vs_fragment": "深度学习与碎片消费的比例描述",
    "description": "知识兴趣光谱描述（100字以内）"
  }},
  "activity_pattern": {{
    "peak_hours": "高效时段描述",
    "low_hours": "低谷时段描述",
    "daily_avg": "日均活跃度描述"
  }},
  "decision_style": {{
    "pattern": "决策模式描述",
    "description": "决策行动模式综合描述（100字以内）"
  }},
  "emotional_tendency": {{
    "sentiment_distribution": "情绪分布描述",
    "description": "情绪审美倾向描述（100字以内）"
  }},
  "summary": "一句话总结用户画像（50字以内）",
  "suggestions": ["建议1", "建议2", "建议3"]
}}"""


# === 变化分析 Prompt ===

CHANGE_ANALYSIS_SYSTEM = """你是一个个人画像分析师。你的任务是分析用户画像的变化。"""

CHANGE_ANALYSIS_PROMPT = """对比以下新旧画像数据，识别关键变化。

旧画像：
{old_json}

新画像：
{new_json}

请返回JSON（不要包含markdown代码块标记）：
{{
  "changes": [
    {{
      "field": "变化的字段名",
      "type": "new|increased|decreased|disappeared|changed",
      "old_value": "旧值描述",
      "new_value": "新值描述",
      "summary": "人类可读的变化描述（30字以内）"
    }}
  ],
  "overall_trend": "整体趋势描述（50字以内）"
}}"""
