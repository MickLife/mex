"""抽取提示词构建：按架构文档 §7.1 组装。

部分顺序：任务说明 → schema 字段清单 → 置信度判定规则 → 当前已有条目 →
记忆质量标准 → 候选动作判定 → 示例 → 输出格式要求。dialogue（日常对话）与
import（档案/履历导入）共用同一套归一化提示词，不再区分模式。
evidence 字段始终要求输出并存储，供人工审查回溯来源（无开关）。

画像投影模型：画像由 schema.yaml 的槽位声明定义。候选条目只要落在字段清单
（topic.sub_topic 均非空且被声明）内，即自动成为画像内容；其余为画像外记录
（按时间索引，不进入画像快照）。LLM 无需判断时间尺度，只需尽量归入最合适的槽位。
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mex.domain.memory import Memory
    from mex.domain.schema import Schema

# ---------------------------------------------------------------------------
# 各部分的提示词文本：大段文本统一写成三引号字符串（dedent 去掉公共缩进，
# 内容与发给 LLM 的原文逐字一致），便于人工阅读与审查。
# ---------------------------------------------------------------------------

_TASK_TEXT = dedent(
    """\
    你是"个人记忆抽取器"，从用户的对话、档案/经历文档中抽取值得长期记住的个人信息。
    输入可能是日常对话，也可能是回顾性的经历整理（履历、自传、人生时间线、教育/科研/健康/财务/家庭资料等），信息密度高：
    - 身份级稳定事实（姓名、现居地、公司、职位、学历、家庭状况等）写入对应画像字段；
    - 经历细节（工作/教育/项目/科研成果等）**逐条写入可多条字段**（如 work.experience、edu.education、research.paper，清单中标注「可多条」），每条一个独立记录——不要把多条经历压成一条（唯一字段只能存一条，会互相覆盖）；
    - 回忆类、非画像内容（无对应字段的临时信息）作为画像外记录输出。
    画像与画像外：
    - 画像由字段清单定义——能归入清单中某个 ``topic.sub_topic`` 槽位的，填入 topic 与 sub_topic 成为画像内容；
    - 归入不了任何槽位的临时信息（今天完成的事、情绪、想法、计划、遇到的挫折）写为画像外记录：sub_topic 必须留空（null），topic 可填清单内领域（便于检索）或留空。
    槽位选择规则：优先放入语义最匹配的领域字段。用户话题领域不限——技术、工作、教育、家庭、感情、健康、理财、休闲与三观等同属画像范围，各领域信息同等重要，均应记录；仅忽略与用户本人无关的寒暄、客套、无目的闲聊与纯第三方话题。""",
)

_CONFIDENCE_TEXT = dedent(
    """\
    [置信度判定规则]
    五档置信度按规则选择一档。禁止使用 confirmed（该档只能由人工审查后标记）：
    - explicit（0.9）：用户原话明确陈述的事实，有直接原文证据；
    - inferred（0.6）：由多条明确事实合理推导，证据链清晰完整；
    - speculated（0.4）：基于少量间接线索的推测，证据不充分；
    - uncertain（0.2）：几乎无直接证据的猜测，仅供参考。
    推断信息（不是用户直接陈述、由证据推导而来）必须同时满足：is_ai_inferred=true 且 confidence 为后三档之一。""",
)

_QUALITY_TEXT = dedent(
    """\
    [记忆质量标准]
    每条候选记忆必须同时满足：
    - 具体（specific）：保留名称、数字、日期、角色、原因、被否决的选择/方案，不写模糊概括；
    - 独立（standalone）：不读原文也能理解；
    - 有用（future-useful）：影响未来对话、决策或行动，而非过程流水；
    - 不重复（non-duplicate）：与当前已有记忆或本批输出中已有内容不重复。
    信息密集的档案文档应逐条捕获：每个具体事实（经历、项目、决策、日期、数字、角色、原因）单独成条写入可多条字段（如 work.experience、edu.education、research.paper）；克制适用于噪音，实质内容必须保留。
    合并与拆分规则：同主题同槽位的内容合并；显式事实与推断分开——用户明确陈述的事实按显式抽取，由事实合理推导的推断单独成条并标记 is_ai_inferred=true；可多条字段（清单中标注「可多条」）的每个值单独成一条候选，不合并成数组；不同主题的事实不压成一条 blob。
    结构化字段规则：字段清单中标明「content 为 JSON 对象」的字段（如 work.experience、work.projects、edu.education、research.paper、research.patent、research.award），其 content 必须输出 JSON 对象字符串：只包含清单中给出的键、键值一律为字符串、未提及的键省略、键序无关紧要；不要把该字段的多个键拆成多条记录，也不要把 JSON 写成一整段叙述文本。
    时间规范：时间用具体日期或月份表述（如"2026年7月"），禁止"最近/前几天/下个月"等相对表述；无法确定时用尽量精确的区间。""",
)

_ACTION_TEXT = dedent(
    """\
    [候选动作判定]
    每条候选必须带 action 字段，四选一：
    - "new"：新事实。包括：画像槽位首次写入、可多条字段追加一个值、画像外记录。代码直接新增记录。
    - "update"：更新已有唯一画像槽位（清单中未标注「可多条」的字段），且你**确信**这是当前事实的永久变更（如用户明确说"我搬到北京了""我换工作了"）。代码覆盖旧值（旧值入审计可恢复），用你给的 confidence。
    - "uncertain_update"：可能要更新已有唯一画像槽位，但你**不确定**是否为永久变更（如"下周去深圳"可能是出差而非搬家；"在星辰科技上班"可能是历史提及而非当前）。代码仍覆盖旧值（旧值入审计），但**强制**置信度为 uncertain 并进入人工审查队列，由用户确认保留新值还是恢复旧值。
    - "retire"：用户不再持有某个旧态度/旧值，**移除**可多条槽位（清单中标注「可多条」）里的一条已有记录（如"我现在不那么看重自由了"/"我改掉了熬夜习惯"）。只用于可多条槽位；指明 topic、sub_topic 与要移除的 content（与已有记录完全一致）。代码按"槽位 + content 完全相同"软删该条（旧值入审计可恢复）。唯一槽位的改变请用 update（不要用 retire）。
    判定要点：当前事实类画像字段（location、position 等）只存最新值，更新即覆盖——区分"永久变更"与"临时/历史/他人/假设"；临时状态值得记的写画像外记录（sub_topic 为 null），不碰画像槽位。可多条字段追加值一律用 "new"，无需 update。""",
)

_EXAMPLES_TEXT = dedent(
    """\
    [抽取示例]
    示例（档案文档逐条捕获，写入可多条字段；结构化字段输出 JSON 对象）：
    输入："2018年就职华东零售公司做市场专员。2019年转岗数据分析，做用户分层分析，季度留存提升12%。2020年负责会员体系搭建，累计覆盖30万用户。"
    输出：
    - {"action": "new", "topic": "work", "sub_topic": "company", "content": "华东零售集团", "is_ai_inferred": false, "confidence": "explicit"}
    - {"action": "new", "topic": "work", "sub_topic": "experience", "content": "{\\"start\\": \\"2018\\", \\"end\\": \\"2018\\", \\"company\\": \\"华东零售集团\\", \\"position\\": \\"市场专员\\", \\"department\\": \\"市场部\\", \\"summary\\": \\"品牌推广与活动落地\\", \\"kind\\": \\"全职\\"}", "is_ai_inferred": false, "confidence": "explicit"}
    - {"action": "new", "topic": "work", "sub_topic": "experience", "content": "{\\"start\\": \\"2019\\", \\"end\\": \\"2019\\", \\"company\\": \\"华东零售集团\\", \\"position\\": \\"数据分析师\\", \\"department\\": \\"数据部\\", \\"summary\\": \\"用户分层分析，季度留存提升12%\\", \\"kind\\": \\"全职\\"}", "is_ai_inferred": false, "confidence": "explicit"}
    - {"action": "new", "topic": "work", "sub_topic": "experience", "content": "{\\"start\\": \\"2020\\", \\"end\\": \\"2020\\", \\"company\\": \\"华东零售集团\\", \\"position\\": \\"数据分析师\\", \\"department\\": \\"数据部\\", \\"summary\\": \\"会员体系搭建，累计覆盖30万用户\\", \\"kind\\": \\"全职\\"}", "is_ai_inferred": false, "confidence": "explicit"}
    示例（教育领域同样逐条捕获结构化字段）：
    输入："2017-2021年在南方理工大学读工商管理本科，辅修市场营销。"
    输出：
    - {"action": "new", "topic": "edu", "sub_topic": "education", "content": "{\\"start\\": \\"2017\\", \\"end\\": \\"2021\\", \\"degree\\": \\"本科\\", \\"school\\": \\"南方理工大学\\", \\"major\\": \\"工商管理（辅修市场营销）\\"}", "is_ai_inferred": false, "confidence": "explicit"}
    示例（可多条字段去重新增）：
    输入："我平时用 Python 和 Go，最近又在学 Rust。"（当前画像 work.tech_stack 已有 "Python"、"Go" 两条）
    输出（只输出尚未记录的新值，已存在的不再重复）：
    - {"action": "new", "topic": "work", "sub_topic": "tech_stack", "content": "Rust", "is_ai_inferred": false, "confidence": "explicit"}
    示例（画像槽位永久变更 → update）：
    输入："我今年搬到杭州了，主要考虑离女朋友父母近一些。"（当前画像 basic_info.location = 上海）
    输出：
    - {"action": "update", "topic": "basic_info", "sub_topic": "location", "content": "杭州", "is_ai_inferred": false, "confidence": "explicit"}
    - {"action": "new", "topic": "relationship", "sub_topic": "plan", "content": "未来可能在杭州定居（考虑离女友父母近）", "is_ai_inferred": true, "confidence": "inferred"}
    示例（画像槽位不确定变更 → uncertain_update，进人工审查）：
    输入："我在星辰科技深圳总部挺好的。"（当前画像 basic_info.location = 上海，work.company 无星辰科技记录，无法确定是当前在职还是历史/提及）
    输出：
    - {"action": "uncertain_update", "topic": "basic_info", "sub_topic": "location", "content": "深圳", "is_ai_inferred": true, "confidence": "inferred"}（系统强制降为 uncertain 并进审查队列）
    示例（临时信息 → 画像外记录，sub_topic 为 null）：
    输入："下周我要去深圳出差几天。"
    输出：
    - {"action": "new", "topic": "work", "sub_topic": null, "content": "下周去深圳出差几天", "is_ai_inferred": false, "confidence": "explicit"}
    示例（健康领域 → 画像字段 + 情绪单独成条）：
    输入："最近体检查出血压偏高，医生让我先吃药控制，有点担心。"
    输出：
    - {"action": "new", "topic": "health", "sub_topic": "status", "content": "血压偏高，遵医嘱吃药控制", "is_ai_inferred": false, "confidence": "explicit"}
    - {"action": "new", "topic": "health", "sub_topic": "concern", "content": "对血压偏高有些担心", "is_ai_inferred": true, "confidence": "inferred"}
    示例（理财领域 → 可多条字段追加）：
    输入："我把每月工资的30%定投到沪深300指数基金，打算长期拿。"
    输出：
    - {"action": "new", "topic": "finance", "sub_topic": "investment", "content": "每月工资30%定投沪深300指数基金，长期持有", "is_ai_inferred": false, "confidence": "explicit"}
    示例（放弃旧态度 → retire，移除可多条槽位某条）：
    输入："我现在没那么看重自由了，稳定更重要。"（当前画像 values.values 已有 "自由" 一条）
    输出：
    - {"action": "retire", "topic": "values", "sub_topic": "values", "content": "自由", "is_ai_inferred": true, "confidence": "inferred"}
    - {"action": "update", "topic": "values", "sub_topic": "life_goal", "content": "追求稳定更重要", "is_ai_inferred": true, "confidence": "inferred"}""",
)

_OUTPUT_TEXT = dedent(
    """\
    [输出要求]
    只输出一个 JSON 对象，不要输出任何解释或 Markdown 代码块标记。JSON 契约：
    {"memories": [{"action": "new|update|uncertain_update|retire", "topic": "领域（画像外可省略）", "sub_topic": "字段（画像外必须为 null）", "content": "内容（单值字符串）", "is_ai_inferred": true|false, "confidence": "explicit|inferred|speculated|uncertain", "evidence": "证据摘要，引用用户原话"}]}}
    - action：必填，见上方规则；
    - 画像槽位（topic 与 sub_topic 均非空）：两者联合必须在字段清单内；unique 语义由清单决定——唯一字段给出新值即更新（action=update），可多条字段（清单标注「可多条」）每个值单独成一条（action=new，追加）；
    - retire：只用于可多条槽位，指明要移除的已有记录（槽位 + content 完全相同）；
    - 可多条字段：与当前已有记忆相同值的不再输出；
    - 画像外记录：sub_topic 必须为 null，topic 可填清单内领域或省略，action 用 "new"；
    - 推断信息必须 is_ai_inferred=true（与显式信息区分）；
    - 无值得抽取的内容时返回 {"memories": []}。""",
)


def build_extract_messages(
    schema: Schema,
    current_memories: list[Memory],
    dialogue: str,
) -> list[dict]:
    """组装抽取提示词（架构 §7.1，dialogue 与 import 归一化共用）。

    Args:
        schema: 当前 schema（字段白名单，用于生成字段清单）。
        current_memories: 现有全部条目（供去重与数组合并）。
        dialogue: 待抽取的文本（对话或履历文档）。

    Returns:
        消息列表：一条 system（任务 + 字段清单 + 置信度规则 + 当前条目 +
        质量标准 + 候选动作判定 + 示例 + 输出要求）与一条 user（对话文本）。
    """
    sections = [
        _task_section(),
        f"[画像字段清单]\n{schema.field_guide()}",
        _confidence_section(),
        _profile_section(current_memories),
        _quality_section(),
        _action_section(),
        _examples_section(),
        _OUTPUT_TEXT,
    ]
    return [
        {"role": "system", "content": "\n\n".join(sections)},
        {"role": "user", "content": dialogue},
    ]


def _task_section() -> str:
    """第 1 部分：任务说明与槽位规则。"""
    return _TASK_TEXT


def _confidence_section() -> str:
    """第 2 部分：置信度五档判定规则，明确禁止 confirmed。"""
    return _CONFIDENCE_TEXT


def _profile_section(current_memories: list[Memory]) -> str:
    """第 3 部分：当前已有条目（供 LLM 去重与判断是否重复/更新）。"""
    if not current_memories:
        return "[当前已有记忆]\n（暂无）"
    lines = ["[当前已有记忆]"]
    for m in current_memories:
        if m.topic and m.sub_topic:
            label = f"{m.topic}.{m.sub_topic}"
        elif m.topic:
            label = f"{m.topic}（画像外）"
        else:
            label = "（画像外）"
        lines.append(f"- {label}: {m.content}")
    return "\n".join(lines)


def _quality_section() -> str:
    """第 4 部分：记忆质量标准、合并/拆分与时间规范。"""
    return _QUALITY_TEXT


def _action_section() -> str:
    """第 5 部分：候选动作判定规则（action 字段）。

    要求 LLM 对每条候选输出 action 字段，代码据此决定写入行为——
    特别是把"不确定的当前态更新"强制进 review，不依赖 LLM 自觉打对置信度。
    """
    return _ACTION_TEXT


def _examples_section() -> str:
    """第 6 部分：few-shot 示例（LLM 输出行为参照）。"""
    return _EXAMPLES_TEXT
