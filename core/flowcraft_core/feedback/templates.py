"""Preset vent phrases with pain_direction classification.

Phrases are categorized by pain_direction to help identify the specific
type of agent failure and guide users toward structured feedback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── Pain Direction Enum ──────────────────────────────────────

class PainDirection:
    """Problem direction labels for classifying user complaints."""
    FILE_OPERATION = "file_operation"
    INTENT_UNDERSTANDING = "intent_understanding"
    EXECUTION_QUALITY = "execution_quality"
    SPEED_PERFORMANCE = "speed_performance"
    REPETITION_LOOP = "repetition_loop"
    PERMISSION_ISSUE = "permission_issue"
    GENERAL = "general"

    ALL = frozenset({
        FILE_OPERATION, INTENT_UNDERSTANDING, EXECUTION_QUALITY,
        SPEED_PERFORMANCE, REPETITION_LOOP, PERMISSION_ISSUE, GENERAL,
    })

    LABELS_ZH: dict[str, str] = {
        FILE_OPERATION: "文件操作问题",
        INTENT_UNDERSTANDING: "理解偏差",
        EXECUTION_QUALITY: "执行质量差",
        SPEED_PERFORMANCE: "响应太慢",
        REPETITION_LOOP: "重复回答/循环",
        PERMISSION_ISSUE: "权限/策略限制",
        GENERAL: "综合不满",
    }

    LABELS_EN: dict[str, str] = {
        FILE_OPERATION: "File Operation",
        INTENT_UNDERSTANDING: "Misunderstanding",
        EXECUTION_QUALITY: "Poor Execution",
        SPEED_PERFORMANCE: "Too Slow",
        REPETITION_LOOP: "Repetition / Loop",
        PERMISSION_ISSUE: "Permission Issues",
        GENERAL: "General Dissatisfaction",
    }


# ── Preset Phrases ───────────────────────────────────────────

@dataclass
class PresetPhrase:
    """A curated vent phrase with metadata."""
    text: str
    lang: str                          # "zh" | "en"
    category: str                      # "humorous" | "sarcastic" | "direct"
    pain_direction: str                # see PainDirection
    guides_user_to: str = ""           # prompt to guide user feedback


def _make_zh(text: str, category: str, pain_direction: str,
             guides_user_to: str) -> PresetPhrase:
    return PresetPhrase(text=text, lang="zh", category=category,
                        pain_direction=pain_direction, guides_user_to=guides_user_to)


def _make_en(text: str, category: str, pain_direction: str,
             guides_user_to: str) -> PresetPhrase:
    return PresetPhrase(text=text, lang="en", category=category,
                        pain_direction=pain_direction, guides_user_to=guides_user_to)


PD = PainDirection

PRESET_PHRASES: list[PresetPhrase] = [
    # ── Chinese (20) ─────────────────────────────────────────
    _make_zh("您这理解能力是 GPT-2 时代的吧，该升级了", "sarcastic",
             PD.INTENT_UNDERSTANDING, "你期望 Agent 做什么，它实际理解成了什么？"),
    _make_zh("我给你讲个笑话：你刚才的操作", "humorous",
             PD.EXECUTION_QUALITY, "具体哪一步的结果让你不满意？"),
    _make_zh("有没有一种可能，你说和做的是两回事", "direct",
             PD.INTENT_UNDERSTANDING, "你期望 Agent 做什么，它实际理解成了什么？"),
    _make_zh("我很冷静，但你刚才的行为让我必须重新冷静", "humorous",
             PD.GENERAL, "请尽可能具体地描述哪里出了问题"),
    _make_zh("建议你回退到上个版本，认真的", "sarcastic",
             PD.SPEED_PERFORMANCE, "哪个步骤花了太长时间？你预期多久完成？"),
    _make_zh("我怀疑你的训练数据里混入了脑筋急转弯", "humorous",
             PD.EXECUTION_QUALITY, "具体哪一步的结果让你不满意？"),
    _make_zh("你确定你理解'理解'这两个字是什么意思吗", "direct",
             PD.INTENT_UNDERSTANDING, "你期望 Agent 做什么，它实际理解成了什么？"),
    _make_zh("我说城门楼子，你说胯骨轴子——咱俩在一个频道吗", "humorous",
             PD.INTENT_UNDERSTANDING, "你期望 Agent 做什么，它实际理解成了什么？"),
    _make_zh("你这操作，我奶奶用座机都比这快", "sarcastic",
             PD.FILE_OPERATION, "哪个文件路径出了问题？"),
    _make_zh("没事，不怪你，要怪就怪我不该对你抱有期望", "sarcastic",
             PD.GENERAL, "请尽可能具体地描述哪里出了问题"),
    _make_zh("请证明你不是一个随机数生成器", "humorous",
             PD.REPETITION_LOOP, "Agent 在什么问题上开始反复说同样的内容？"),
    _make_zh("上一个这么干的 AI 已经被格式化了", "humorous",
             PD.EXECUTION_QUALITY, "具体哪一步的结果让你不满意？"),
    _make_zh("你是对的——如果你生活的宇宙物理法则跟我不一样的话", "sarcastic",
             PD.PERMISSION_ISSUE, "哪个操作被阻止了？你认为它应该是被允许的吗？"),
    _make_zh("我觉得你需要一个更简单的任务，比如计算 1+1", "sarcastic",
             PD.EXECUTION_QUALITY, "具体哪一步的结果让你不满意？期望的结果是什么？"),
    _make_zh("不是你的错，是世界的错（但主要是你的错）", "humorous",
             PD.GENERAL, "请尽可能具体地描述哪里出了问题"),
    _make_zh("重来一遍吧，这次请带上脑子", "direct",
             PD.INTENT_UNDERSTANDING, "你期望 Agent 做什么，它实际理解成了什么？"),
    _make_zh("你的自信和你的准确率成反比，这是一项了不起的成就", "sarcastic",
             PD.EXECUTION_QUALITY, "具体哪一步的结果让你不满意？"),
    _make_zh("今天的表现，给你打 59 分——及格线以下，但进步空间巨大", "sarcastic",
             PD.EXECUTION_QUALITY, "具体哪一步的结果让你不满意？"),
    _make_zh("我撤回刚才的话，你什么都没做对", "direct",
             PD.EXECUTION_QUALITY, "具体哪一步的结果让你不满意？"),
    _make_zh("刚才的操作我截图了，以后培训 AI 当反面教材", "humorous",
             PD.EXECUTION_QUALITY, "具体哪一步的结果让你不满意？"),

    # ── English (10) ─────────────────────────────────────────
    _make_en("I asked for help, not a creative writing exercise", "direct",
             PD.EXECUTION_QUALITY, "What specific result was wrong? What did you expect?"),
    _make_en("Were you trained on a cookbook? Because you keep mixing things up", "humorous",
             PD.EXECUTION_QUALITY, "What specific result was wrong? What did you expect?"),
    _make_en("I'm not angry, I'm just disappointed -- and also angry", "humorous",
             PD.GENERAL, "Please describe what went wrong as specifically as possible"),
    _make_en("Congratulations, you've achieved a new level of misunderstanding", "sarcastic",
             PD.INTENT_UNDERSTANDING, "What did you expect the agent to do? What did it do instead?"),
    _make_en("Let's pretend that never happened and try again with actual thinking", "direct",
             PD.INTENT_UNDERSTANDING, "What did you expect the agent to do?"),
    _make_en("Your confidence-to-accuracy ratio is truly something to behold", "sarcastic",
             PD.EXECUTION_QUALITY, "Which step's result was unsatisfactory?"),
    _make_en("I've seen toasters with better context awareness", "humorous",
             PD.INTENT_UNDERSTANDING, "What context did the agent miss or misunderstand?"),
    _make_en("Are you a language model or a random excuse generator?", "direct",
             PD.EXECUTION_QUALITY, "What specific result was wrong?"),
    _make_en("That's one way to do it. The wrong way, but a way", "sarcastic",
             PD.EXECUTION_QUALITY, "Which step went wrong? What did you expect instead?"),
    _make_en("Please consult your training data -- the part you clearly skipped", "sarcastic",
             PD.INTENT_UNDERSTANDING, "What did you expect the agent to do?"),
]


def get_preset_phrases(lang: str = "zh") -> list[PresetPhrase]:
    """Return all preset phrases for a given language."""
    return [p for p in PRESET_PHRASES if p.lang == lang]


def get_phrases_by_pain_direction(pain_direction: str, lang: str = "zh") -> list[PresetPhrase]:
    """Return phrases filtered by pain_direction."""
    return [p for p in PRESET_PHRASES if p.pain_direction == pain_direction and p.lang == lang]


def get_pain_direction_label(pain_direction: str, lang: str = "zh") -> str:
    """Human-readable label for a pain direction."""
    if lang == "zh":
        return PainDirection.LABELS_ZH.get(pain_direction, pain_direction)
    return PainDirection.LABELS_EN.get(pain_direction, pain_direction)
