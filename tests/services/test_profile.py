"""M4 服务层：profile 画像快照单测（画像 = schema 槽位投影 + 近期动态）。"""

from datetime import UTC, datetime

import pytest

from mex.domain.memory import Confidence, Memory
from mex.domain.schema import FieldSpec, Schema, SubTopicSpec, TopicSpec
from mex.services.profile import estimate_tokens, generate_profile
from mex.store import memories
from mex.store.connection import connect, init_db, transaction


@pytest.fixture
def conn(tmp_path):
    """临时库连接。"""
    init_db(tmp_path / "test.db")
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def build_schema() -> Schema:
    """与默认 schema 结构相近的测试 schema（topic 顺序即输出顺序）。"""
    return Schema(
        topics={
            "basic_info": TopicSpec(
                name="basic_info",
                description="基础信息",
                sub_topics={
                    "name": SubTopicSpec(name="name", description="姓名"),
                    "location": SubTopicSpec(name="location", description="现居地"),
                    "cultural_background": SubTopicSpec(name="cultural_background", description="文化背景"),
                    "language_habit": SubTopicSpec(name="language_habit", description="日常语言使用习惯"),
                    "life_stage": SubTopicSpec(name="life_stage", description="人生阶段"),
                },
            ),
            "work": TopicSpec(
                name="work",
                description="工作与职业",
                sub_topics={
                    "company": SubTopicSpec(name="company", description="公司"),
                    "position": SubTopicSpec(name="position", description="职位"),
                    "tech_stack": SubTopicSpec(name="tech_stack", description="技术栈", unique=False),
                    "current_project": SubTopicSpec(name="current_project", description="当前项目"),
                    "soft_skill": SubTopicSpec(name="soft_skill", description="软技能", unique=False),
                    "job_search": SubTopicSpec(
                        name="job_search",
                        description="求职状态与偏好",
                        fields={
                            "status": FieldSpec(description="求职状态"),
                            "expected_position": FieldSpec(description="期望岗位"),
                        },
                    ),
                },
            ),
            "career": TopicSpec(
                name="career",
                description="职业发展",
                sub_topics={"current_goal": SubTopicSpec(name="current_goal", description="本月目标")},
            ),
            "finance": TopicSpec(
                name="finance",
                description="财务",
                sub_topics={"risk_appetite": SubTopicSpec(name="risk_appetite", description="风险偏好")},
            ),
            "health": TopicSpec(
                name="health",
                description="健康",
                sub_topics={"habit": SubTopicSpec(name="habit", description="生活习惯", unique=False)},
            ),
            "values": TopicSpec(
                name="values",
                description="三观与原则",
                sub_topics={
                    "personality": SubTopicSpec(name="personality", description="性格倾向"),
                    "risk_preference": SubTopicSpec(name="risk_preference", description="风险偏好"),
                    "decision_style": SubTopicSpec(name="decision_style", description="认知与决策风格"),
                    "emotion_triggers": SubTopicSpec(
                        name="emotion_triggers",
                        description="情绪触发点",
                        unique=False,
                        fields={
                            "trigger": FieldSpec(description="触发话题或情境", required=True),
                            "reaction": FieldSpec(description="情绪反应", required=True),
                        },
                    ),
                    "bottom_line": SubTopicSpec(name="bottom_line", description="底线", unique=False),
                },
            ),
            "interaction": TopicSpec(
                name="interaction",
                description="交互偏好",
                sub_topics={
                    "communication_style": SubTopicSpec(name="communication_style", description="沟通风格偏好"),
                    "info_density": SubTopicSpec(name="info_density", description="信息密度偏好"),
                    "feedback_style": SubTopicSpec(
                        name="feedback_style", description="对 AI 犯错的反馈偏好与肯定表达方式",
                    ),
                    "behavior_habits": SubTopicSpec(
                        name="behavior_habits", description="行为习惯与痛点", unique=False,
                    ),
                },
            ),
            "goals": TopicSpec(
                name="goals",
                description="目标与计划",
                sub_topics={
                    "short_term_goal": SubTopicSpec(
                        name="short_term_goal",
                        description="短期目标",
                        unique=False,
                        fields={
                            "goal": FieldSpec(description="目标内容", required=True),
                            "deadline": FieldSpec(description="截止时间", format="time", required=True),
                            "domain": FieldSpec(description="所属领域"),
                        },
                    ),
                },
            ),
            "family": TopicSpec(
                name="family",
                description="家庭",
                sub_topics={
                    "relationships": SubTopicSpec(
                        name="relationships",
                        description="核心人际关系及质量",
                        unique=False,
                        fields={
                            "relation": FieldSpec(description="关系", required=True),
                            "quality": FieldSpec(description="关系质量或状态", required=True),
                        },
                    ),
                },
            ),
            "edu": TopicSpec(
                name="edu",
                description="教育背景",
                sub_topics={"focus": SubTopicSpec(name="focus", description="教育聚焦方向")},
            ),
        },
    )


def make_memory(**kwargs):
    """构造测试记忆。"""
    defaults = {
        "id": "m-1",
        "topic": "work",
        "sub_topic": "company",
        "content": "华为",
        "is_ai_inferred": False,
        "confidence": Confidence.CONFIRMED,
        "evidence": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(kwargs)
    return Memory(**defaults)


def seed(conn, *ms):
    """批量插入测试数据。"""
    with transaction(conn):
        for m in ms:
            memories.insert(conn, m)


class TestEstimateTokens:
    def test_pure_chinese(self):
        assert estimate_tokens("你好") == 3

    def test_pure_english(self):
        assert estimate_tokens("hello world") == 3

    def test_mixed(self):
        assert estimate_tokens("你好 hello") == 5

    def test_empty(self):
        assert estimate_tokens("") == 0


class TestFormat:
    def test_outside_records_not_in_profile(self, conn):
        """画像外记录（sub_topic 为空）不进画像主体；近期窗口外的也不进近期动态。

        窗口边界用固定 now 锚定：记录创建于 2026-01-01，now 取 2026-01-15
        （7 天窗口起点 01-08），故不进入近期动态小节。
        """
        schema = build_schema()
        seed(
            conn,
            make_memory(id="a", topic="finance", sub_topic=None, content="计划配置百万医疗险+重疾险"),
            make_memory(id="b", topic="finance", sub_topic="risk_appetite", content="稳健"),
        )
        text = generate_profile(conn, schema, max_tokens=500, now=datetime(2026, 1, 15, tzinfo=UTC))
        assert "计划配置百万医疗险" not in text
        assert "- 风险偏好：稳健" in text

    def test_profile_snapshot(self, conn):
        """画像 = schema 槽位投影：字段槽位全部输出，未分类不出现。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(id="s1", topic="basic_info", sub_topic="name", content="王某"),
            make_memory(id="s2", topic="basic_info", sub_topic="location", content="北京"),
            make_memory(id="s3", topic="work", sub_topic="company", content="华为"),
            make_memory(id="s4", topic="work", sub_topic="position", content="无线部门工程师"),
            make_memory(id="s5", topic="work", sub_topic="tech_stack", content="Python"),
            make_memory(id="s5b", topic="work", sub_topic="tech_stack", content="Go"),
            make_memory(
                id="s6",
                topic="finance",
                sub_topic="risk_appetite",
                content="稳健偏保守",
                confidence=Confidence.SPECULATED,
            ),
            make_memory(
                id="t1",
                topic="work",
                sub_topic="current_project",
                content="正在从零开发个人记忆系统 meX",
                updated_at="2026-08-05T16:00:00Z",
            ),
        )
        expected = "\n".join(
            [
                "# 用户画像",
                "## 基础信息",
                "- 姓名：王某",
                "- 现居地：北京",
                "## 工作与职业",
                "- 公司：华为",
                "- 职位：无线部门工程师",
                "- 技术栈：Python",
                "- 技术栈：Go",
                "- 当前项目：正在从零开发个人记忆系统 meX",
                "## 财务",
                "- 风险偏好：稳健偏保守（待确认）",
            ],
        )
        assert generate_profile(conn, schema) == expected

    def test_confidence_marks(self, conn):
        """五种置信度各自的标注规则。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(id="c1", topic="basic_info", sub_topic="name", content="甲", confidence=Confidence.CONFIRMED),
            make_memory(
                id="c2",
                topic="basic_info",
                sub_topic="location",
                content="乙",
                confidence=Confidence.EXPLICIT,
            ),
            make_memory(
                id="c3",
                topic="work",
                sub_topic="company",
                content="丙",
                confidence=Confidence.INFERRED,
            ),
            make_memory(
                id="c4",
                topic="work",
                sub_topic="position",
                content="丁",
                confidence=Confidence.SPECULATED,
            ),
            make_memory(
                id="c5",
                topic="finance",
                sub_topic="risk_appetite",
                content="戊",
                confidence=Confidence.UNCERTAIN,
            ),
        )
        text = generate_profile(conn, schema)
        lines = text.splitlines()
        assert "- 姓名：甲" in lines
        assert "- 现居地：乙" in lines
        assert "- 公司：丙（推断）" in lines
        assert "- 职位：丁（待确认）" in lines
        assert "- 风险偏好：戊（待确认）" in lines

    def test_many_slot_renders_each_row(self, conn):
        """可多条槽位：每条记录独立成行（不再合并成数组）。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(id="v1", topic="work", sub_topic="tech_stack", content="Python"),
            make_memory(id="v2", topic="work", sub_topic="tech_stack", content="Go"),
            make_memory(id="v3", topic="work", sub_topic="tech_stack", content="Rust"),
            make_memory(id="v4", topic="health", sub_topic="habit", content="早睡"),
            make_memory(id="v5", topic="health", sub_topic="habit", content="跑步"),
        )
        text = generate_profile(conn, schema)
        assert "- 技术栈：Python" in text
        assert "- 技术栈：Go" in text
        assert "- 技术栈：Rust" in text
        assert "- 生活习惯：早睡" in text
        assert "- 生活习惯：跑步" in text

    def test_many_slot_collapses_over_three(self, conn):
        """可多条槽位超过 3 条时折叠为前 3 条 + 共 X 条提示。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(id="e1", topic="work", sub_topic="tech_stack", content="Python"),
            make_memory(id="e2", topic="work", sub_topic="tech_stack", content="Go"),
            make_memory(id="e3", topic="work", sub_topic="tech_stack", content="Rust"),
            make_memory(id="e4", topic="work", sub_topic="tech_stack", content="C++"),
            make_memory(id="e5", topic="work", sub_topic="tech_stack", content="Java"),
        )
        text = generate_profile(conn, schema, max_tokens=2000)
        assert "- 技术栈：Python" in text
        assert "共 5 条" in text
        assert "mex search --topic work" in text
        assert "- 技术栈：C++" not in text
        assert "- 技术栈：Java" not in text

    def test_plain_string_content_shown_as_is(self, conn):
        """content 永远单值字符串，原样展示（不再尝试解析 JSON 数组）。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(id="d1", topic="work", sub_topic="tech_stack", content="不是JSON"),
            make_memory(id="d2", topic="work", sub_topic="company", content='["a"]'),
        )
        text = generate_profile(conn, schema)
        assert "- 技术栈：不是JSON" in text
        assert '- 公司：["a"]' in text

    def test_empty_db_empty_snapshot(self, conn):
        text = generate_profile(conn, build_schema())
        assert text == "# 用户画像"

    def test_topic_outside_schema_skipped(self, conn):
        schema = build_schema()
        seed(
            conn,
            make_memory(id="o1", topic="basic_info", sub_topic="name", content="甲"),
            make_memory(id="o2", topic="removed_topic", sub_topic="old_field", content="悬空"),
        )
        text = generate_profile(conn, schema)
        assert "甲" in text
        assert "removed_topic" not in text


class TestTopicsFilter:
    def test_filter_single_topic(self, conn):
        schema = build_schema()
        seed(
            conn,
            make_memory(id="f1", topic="basic_info", sub_topic="name", content="甲"),
            make_memory(id="f2", topic="work", sub_topic="company", content="乙"),
            make_memory(id="f3", topic="work", sub_topic="current_project", content="丙"),
        )
        text = generate_profile(conn, schema, topics=["work"])
        assert "## 工作与职业" in text
        assert "乙" in text
        assert "丙" in text
        assert "基础信息" not in text
        assert "# 用户画像" in text


class TestTruncation:
    def _long_text(self, n: int) -> str:
        return "这是一个用来撑大token预算的中文测试句子。" * n

    def test_high_confidence_kept_low_dropped(self, conn):
        schema = build_schema()
        seed(
            conn,
            make_memory(id="t1", topic="work", sub_topic="company", content="华为", confidence=Confidence.CONFIRMED),
            make_memory(
                id="t2",
                topic="work",
                sub_topic="current_project",
                content=self._long_text(30),
                confidence=Confidence.UNCERTAIN,
            ),
        )
        text = generate_profile(conn, schema, max_tokens=200)
        assert "华为" in text
        assert "这是一个用来撑大" not in text

    def test_low_confidence_dropped_first(self, conn):
        schema = build_schema()
        seed(
            conn,
            make_memory(id="t1", topic="work", sub_topic="company", content="高置信", confidence=Confidence.CONFIRMED),
            make_memory(
                id="t2",
                topic="work",
                sub_topic="position",
                content=self._long_text(20),
                confidence=Confidence.UNCERTAIN,
            ),
        )
        text = generate_profile(conn, schema, max_tokens=150)
        assert "高置信" in text
        assert "这是一个用来撑大" not in text

    def test_high_confidence_kept_when_budget_allows_all(self, conn):
        schema = build_schema()
        seed(
            conn,
            make_memory(id="k1", topic="work", sub_topic="company", content="华为"),
            make_memory(
                id="k2",
                topic="work",
                sub_topic="current_project",
                content=self._long_text(10),
            ),
        )
        text = generate_profile(conn, schema, max_tokens=2000)
        assert "华为" in text
        assert "这是一个用来撑大" in text


class TestStructuredContent:
    """结构化 JSON 字段的画像渲染。"""

    @pytest.fixture
    def structured_schema(self):
        return Schema(
            topics={
                "work": TopicSpec(
                    name="work",
                    description="工作与职业",
                    sub_topics={
                        "company": SubTopicSpec(name="company", description="公司"),
                        "experience": SubTopicSpec(
                            name="experience",
                            description="工作经历",
                            unique=False,
                            fields={
                                "start": FieldSpec(description="开始年份"),
                                "end": FieldSpec(description="结束年份"),
                                "company": FieldSpec(description="公司名"),
                                "position": FieldSpec(description="职位"),
                                "summary": FieldSpec(description="工作内容概要"),
                            },
                        ),
                    },
                ),
            },
        )

    def test_structured_json_rendered_readable(self, conn, structured_schema):
        """结构化字段的 JSON content 渲染为可读文本，而非原始 JSON。"""
        seed(
            conn,
            make_memory(
                id="e1",
                topic="work",
                sub_topic="experience",
                content=(
                    '{"start": "2019", "end": "2023", "company": "华为", '
                    '"position": "后端工程师", "summary": "订单系统重构"}'
                ),
            ),
        )
        text = generate_profile(conn, structured_schema, max_tokens=500)
        assert '"start"' not in text  # 不展示原始 JSON 键
        assert "开始年份：2019" in text
        assert "公司名：华为" in text
        assert "职位：后端工程师" in text
        assert '"summary"' not in text  # JSON 键名不被当作文本展示
        assert "工作内容概要：订单系统重构" in text

    def test_structured_broken_json_falls_back_to_raw(self, conn, structured_schema):
        """JSON 解析失败时回退展示原文（容错，不崩溃）。"""
        seed(
            conn,
            make_memory(id="e2", topic="work", sub_topic="experience", content="不是 JSON 的文本"),
        )
        text = generate_profile(conn, structured_schema, max_tokens=500)
        assert "不是 JSON 的文本" in text


class TestValuesFields:
    """values 领域新字段进画像快照、结构化字段渲染为可读文本。"""

    def test_new_plain_fields_appear_in_profile(self, conn):
        """新字段写入后 profile 输出对应行（描述为简短展示名）。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(id="n1", topic="values", sub_topic="personality", content="INTJ"),
            make_memory(id="n2", topic="values", sub_topic="risk_preference", content="稳健偏保守"),
            make_memory(id="n3", topic="values", sub_topic="decision_style", content="数据驱动、深思熟虑"),
        )
        text = generate_profile(conn, schema)
        assert "## 三观与原则" in text
        assert "- 性格倾向：INTJ" in text
        assert "- 风险偏好：稳健偏保守" in text
        assert "- 认知与决策风格：数据驱动、深思熟虑" in text

    def test_emotion_triggers_rendered_readable(self, conn):
        """结构化 content 渲染为可读文本（按键序），不暴露原始 JSON。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(
                id="e1",
                topic="values",
                sub_topic="emotion_triggers",
                content='{"trigger": "前领导", "reaction": "暴躁"}',
            ),
        )
        text = generate_profile(conn, schema)
        assert '"trigger"' not in text
        assert '"reaction"' not in text
        assert "- 情绪触发点：触发话题或情境：前领导；情绪反应：暴躁" in text

    def test_bottom_line_many_slot_renders_each_row(self, conn):
        """bottom_line 可多条：每条独立成行。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(id="b1", topic="values", sub_topic="bottom_line", content="不接受长期 996"),
            make_memory(id="b2", topic="values", sub_topic="bottom_line", content="不接受欺骗朋友"),
        )
        text = generate_profile(conn, schema)
        assert "- 底线：不接受长期 996" in text
        assert "- 底线：不接受欺骗朋友" in text


class TestInteractionFields:
    """交互偏好领域的画像渲染（模块五）。"""

    def test_profile_renders_interaction_section(self, conn):
        """新领域渲染为"## 交互偏好"小节及各字段行。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(id="i1", topic="interaction", sub_topic="communication_style", content="结论先行，轻松幽默"),
            make_memory(id="i2", topic="interaction", sub_topic="info_density", content="要点式速读清单"),
            make_memory(
                id="i3",
                topic="interaction",
                sub_topic="feedback_style",
                content='犯错直接指出；肯定时说"懂了"',
            ),
        )
        text = generate_profile(conn, schema)
        assert "## 交互偏好" in text
        assert "- 沟通风格偏好：结论先行，轻松幽默" in text
        assert "- 信息密度偏好：要点式速读清单" in text
        assert '- 对 AI 犯错的反馈偏好与肯定表达方式：犯错直接指出；肯定时说"懂了"' in text

    def test_behavior_habits_many_collapses_over_three(self, conn):
        """行为习惯可多条，超过 3 条折叠为前 3 条 + 共 X 条提示。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(
                id="h1", topic="interaction", sub_topic="behavior_habits",
                content="严重拖延症，需截止日期压力才能行动",
            ),
            make_memory(id="h2", topic="interaction", sub_topic="behavior_habits", content="夜猫子，深夜效率最高"),
            make_memory(id="h3", topic="interaction", sub_topic="behavior_habits", content="健忘，重要事必须提前提醒"),
            make_memory(id="h4", topic="interaction", sub_topic="behavior_habits", content="讨厌被打断"),
        )
        text = generate_profile(conn, schema, max_tokens=2000)
        # 契约：4 条只展示 3 条 + 折叠提示（具体折叠哪条取决于排序，不作为断言目标）
        shown = [c for c in ("严重拖延症", "夜猫子", "健忘", "讨厌被打断") if c in text]
        assert len(shown) == 3
        assert "共 4 条" in text
        assert "mex search --topic interaction" in text


class TestGoalsFields:
    """goals 领域的渲染与折叠（短期目标：结构化 + 可多条）。"""

    def test_short_term_goal_rendered_readable(self, conn):
        """结构化 content 按键序渲染为可读文本，不暴露原始 JSON。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(
                id="g1", topic="goals", sub_topic="short_term_goal",
                content='{"goal": "通过 PMP 考试", "deadline": "2026-09", "domain": "职业"}',
            ),
        )
        text = generate_profile(conn, schema)
        assert "## 目标与计划" in text
        assert '"goal"' not in text
        assert '"deadline"' not in text
        assert "- 短期目标：目标内容：通过 PMP 考试；截止时间：2026-09；所属领域：职业" in text

    def test_short_term_goal_many_collapses_over_three(self, conn):
        """多条短期目标：超过 3 条折叠为前 3 条 + 共 X 条提示。"""
        schema = build_schema()
        for i, goal in enumerate(("通过 PMP 考试", "减重 10 斤", "读完 6 本书", "攒 5 万应急金"), start=1):
            seed(
                conn,
                make_memory(
                    id=f"g{i}", topic="goals", sub_topic="short_term_goal",
                    content=f'{{"goal": "{goal}", "deadline": "2026-0{i}", "domain": "测试"}}',
                ),
            )
        text = generate_profile(conn, schema, max_tokens=2000)
        shown = [g for g in ("PMP 考试", "减重 10 斤", "读完 6 本书", "攒 5 万应急金") if g in text]
        assert len(shown) == 3
        assert "共 4 条" in text
        assert "mex search --topic goals" in text


class TestBasicEduFields:
    """基础/教育/家庭/工作领域的补充字段渲染。"""

    def test_basic_info_and_edu_focus_rendered(self, conn):
        """基础信息新字段与教育聚焦方向进画像快照。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(id="b1", topic="basic_info", sub_topic="cultural_background", content="汉族，北方文化背景"),
            make_memory(id="b2", topic="basic_info", sub_topic="life_stage", content="大三学生"),
            make_memory(id="e1", topic="edu", sub_topic="focus", content="自然语言处理"),
        )
        text = generate_profile(conn, schema)
        assert "- 文化背景：汉族，北方文化背景" in text
        assert "- 人生阶段：大三学生" in text
        assert "## 教育背景" in text
        assert "- 教育聚焦方向：自然语言处理" in text

    def test_family_relationships_rendered_readable(self, conn):
        """family.relationships 结构化 content 渲染为可读文本。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(
                id="r1", topic="family", sub_topic="relationships",
                content='{"relation": "母亲", "quality": "关系紧张"}',
            ),
        )
        text = generate_profile(conn, schema)
        assert "## 家庭" in text
        assert '"relation"' not in text
        assert "- 核心人际关系及质量：关系：母亲；关系质量或状态：关系紧张" in text

    def test_job_search_and_soft_skill_rendered(self, conn):
        """work.job_search 结构化渲染 + soft_skill 可多条独立行。"""
        schema = build_schema()
        seed(
            conn,
            make_memory(
                id="j1", topic="work", sub_topic="job_search",
                content='{"status": "主动求职", "expected_position": "算法工程师"}',
            ),
            make_memory(id="s1", topic="work", sub_topic="soft_skill", content="跨部门沟通"),
            make_memory(id="s2", topic="work", sub_topic="soft_skill", content="公开演讲"),
        )
        text = generate_profile(conn, schema)
        assert '"status"' not in text
        assert "- 求职状态与偏好：求职状态：主动求职；期望岗位：算法工程师" in text
        assert "- 软技能：跨部门沟通" in text
        assert "- 软技能：公开演讲" in text


class TestRecentSection:
    """近期动态小节：最近画像外记录进 profile。"""

    NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

    def _seed_recent(self, conn):
        seed(
            conn,
            make_memory(
                id="n1", topic="finance", sub_topic=None, content="正在准备明天某大厂终面",
                created_at="2026-08-09T00:00:00Z",
            ),
            make_memory(
                id="n2", topic="finance", sub_topic=None, content="本周减重计划",
                created_at="2026-08-05T00:00:00Z",
            ),
            make_memory(
                id="n3", topic="finance", sub_topic=None, content="一个月前的旧笔记",
                created_at="2026-07-05T00:00:00Z",
            ),
        )

    def test_recent_section_window_order(self, conn):
        """近 7 天内画像外记录进"## 近期动态"，按 created_at 倒序，窗口外不纳入。"""
        self._seed_recent(conn)
        text = generate_profile(conn, build_schema(), now=self.NOW)
        assert "## 近期动态" in text
        assert "正在准备明天某大厂终面" in text
        assert "本周减重计划" in text
        assert "一个月前的旧笔记" not in text  # 窗口外（07-05 早于 08-03 边界）
        lines = text.splitlines()
        assert lines.index("- 正在准备明天某大厂终面") < lines.index("- 本周减重计划")

    def test_recent_days_and_limit_params(self, conn):
        """recent_days=3 只含近 3 天；recent_limit=1 只留最新一条。"""
        self._seed_recent(conn)
        text = generate_profile(conn, build_schema(), now=self.NOW, recent_days=3, recent_limit=10)
        assert "正在准备明天某大厂终面" in text  # 08-09 在近 3 天（边界 08-07）
        assert "本周减重计划" not in text  # 08-05 在 3 天窗外
        text = generate_profile(conn, build_schema(), now=self.NOW, recent_days=7, recent_limit=1)
        assert "正在准备明天某大厂终面" in text
        assert "本周减重计划" not in text

    def test_recent_limit_zero_disables(self, conn):
        """recent_limit=0 关闭近期动态小节。"""
        self._seed_recent(conn)
        text = generate_profile(conn, build_schema(), now=self.NOW, recent_limit=0)
        assert "## 近期动态" not in text
        assert "正在准备明天某大厂终面" not in text

    def test_topics_filter_applies_to_recent(self, conn):
        """topics 筛选同时作用于画像主体与近期动态。"""
        seed(
            conn,
            make_memory(
                id="f1", topic="finance", sub_topic=None, content="理财动态",
                created_at="2026-08-09T00:00:00Z",
            ),
            make_memory(
                id="w1", topic="work", sub_topic=None, content="工作动态",
                created_at="2026-08-09T00:00:00Z",
            ),
        )
        text = generate_profile(conn, build_schema(), topics=["finance"], now=self.NOW)
        assert "理财动态" in text
        assert "工作动态" not in text

    def test_recent_truncation_keeps_latest(self, conn):
        """近期动态超预算时按 created_at 倒序保留最新（删最旧）。"""
        long_content = "这是一条很长的近期动态内容，用来撑大 token 预算以触发截断。" * 20
        seed(
            conn,
            make_memory(
                id="r1", topic="finance", sub_topic=None, content="最新一条短内容",
                created_at="2026-08-09T00:00:00Z",
            ),
            make_memory(
                id="r2", topic="finance", sub_topic=None, content=long_content,
                created_at="2026-08-08T00:00:00Z",
            ),
            make_memory(
                id="r3", topic="finance", sub_topic=None, content="更早一条",
                created_at="2026-08-04T00:00:00Z",
            ),
        )
        text = generate_profile(conn, build_schema(), max_tokens=60, now=self.NOW)
        assert "最新一条短内容" in text
        assert "更早一条" not in text
        assert "很长" not in text  # 长内容行被删

    def test_forgotten_outside_excluded_from_recent(self, conn):
        """软删的画像外记录不进近期动态。"""
        seed(
            conn,
            make_memory(
                id="f1", topic="finance", sub_topic=None, content="已删除的动态",
                created_at="2026-08-09T00:00:00Z",
            ),
        )
        with transaction(conn):
            memories.forget(conn, "f1", "不再需要")
        text = generate_profile(conn, build_schema(), now=self.NOW)
        assert "## 近期动态" not in text

    def test_profile_slots_still_projected_with_recent(self, conn):
        """画像槽位与近期动态共存时，主体仍按 schema 投影（行为不变）。"""
        seed(
            conn,
            make_memory(id="p1", topic="basic_info", sub_topic="name", content="王某"),
            make_memory(
                id="n1", topic="finance", sub_topic=None, content="正在准备明天终面",
                created_at="2026-08-09T00:00:00Z",
            ),
        )
        text = generate_profile(conn, build_schema(), now=self.NOW)
        assert "## 基础信息" in text
        assert "- 姓名：王某" in text
        assert "## 近期动态" in text
        assert "正在准备明天终面" in text

    def test_expired_excluded_from_profile(self, conn):
        """TTL 惰性过滤：已过期的画像槽位与近期动态都不进画像快照。"""
        seed(
            conn,
            make_memory(
                id="p1", topic="basic_info", sub_topic="name", content="已过期",
                expires_at="2000-01-01T00:00:00Z",
            ),
            make_memory(
                id="p2", topic="work", sub_topic="company", content="未过期",
            ),
        )
        text = generate_profile(conn, build_schema(), now=self.NOW)
        assert "未过期" in text
        assert "已过期" not in text
