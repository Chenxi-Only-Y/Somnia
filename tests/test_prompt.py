"""Prompt 端到端回归：固定"最终送进模型的到底是什么"。

改 prompt 或记忆层时，这组测试会告诉你有没有悄悄丢层。
跑法：python tests/test_prompt.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import store, memory, composer, scheduler
from somnia.config import Config

CFG = Config()


class Mock:
    """假模型：记录 prompt，返回固定文本。"""

    def __init__(self, reply="嗯"):
        self.reply = reply
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.reply

    @property
    def last(self):
        return self.prompts[-1]


def _db():
    d = tempfile.mkdtemp()
    return store.connect(str(Path(d) / "t.db"))


def test_all_memory_layers_reach_prompt():
    m = Mock()
    composer.generate(
        CFG, [("user", "在吗"), ("assistant", "嗯")], ["他喜欢辣"],
        "今天好累", 2,
        summaries=[("2026-10-18", "他说周五面试")],
        pending=scheduler.pending_block(["想起他说的那家店"]),
        llm=m,
    )
    p = m.last
    assert "他喜欢辣" in p                    # L3
    assert "他说周五面试" in p                # L2
    assert "在吗" in p                        # L1
    assert "想起他说的那家店" in p            # Outbox
    assert "今天好累" in p                    # 当前消息


def test_pending_block_allows_mild_complaint_in_prompt():
    m = Mock()
    composer.generate(CFG, [], [], "在吗", 1,
                      pending=scheduler.pending_block(["刚看到只猫"]), llm=m)
    assert "一天最多一次" in m.last


def test_burst_rendered_as_list_not_items():
    m = Mock()
    composer.generate(CFG, [], [], "在吗\n今天好累\n算了", 2, llm=m)
    p = m.last
    assert "连发了几条" in p
    assert "- 在吗" in p and "- 算了" in p
    assert "不要逐条回应" in p


def test_single_message_not_rendered_as_burst():
    m = Mock()
    composer.generate(CFG, [], [], "在吗", 1, llm=m)
    assert "连发了几条" not in m.last


def test_segment_count_in_instruction():
    m = Mock()
    composer.generate(CFG, [], [], "在吗", 3, llm=m)
    assert "最多 3 条" in m.last


def test_max_len_comes_from_config():
    """prompt 里的字数上限必须和 Lint 的截断长度一致。"""
    cfg = Config(max_len=12)
    m = Mock()
    composer.generate(cfg, [], [], "在吗", 2, llm=m)
    assert "每条不超过 12 字" in m.last


def test_lint_applied_to_model_output():
    m = Mock("听起来你很难过。| 抱抱你。| 那就早点睡")
    out = composer.generate(CFG, [], [], "累", 3, llm=m)
    joined = "".join(out)
    assert "听起来" not in joined
    assert "抱抱你" not in joined
    assert "。" not in joined
    assert out[0] == "你很难过"


def test_empty_model_output_gives_empty_list():
    m = Mock("")
    assert composer.generate(CFG, [], [], "在吗", 1, llm=m) == []


def test_segment_cap_enforced():
    m = Mock("一|二|三|四|五")
    assert len(composer.generate(CFG, [], [], "在吗", 2, llm=m)) == 2


def test_blank_segments_dropped():
    m = Mock(" | 好 | ")
    assert composer.generate(CFG, [], [], "在吗", 3, llm=m) == ["好"]


def test_end_to_end_from_database():
    c = _db()
    store.add_message(c, "user", "我明天要面试")
    store.add_fact(c, "他喜欢辣", importance=0.9)
    store.add_summary(c, "2026-10-18", "他说过怕黑")

    history, facts, summaries, recall = memory.context(store, c, CFG)
    m = Mock()
    composer.generate(CFG, history, facts, "在吗", 1, summaries=summaries, llm=m)

    p = m.last
    assert "他喜欢辣" in p
    assert "他说过怕黑" in p
    assert "我明天要面试" in p


def test_persona_body_off_by_default():
    """默认不把人设正文发进 prompt；开了开关才发。"""
    import dataclasses, tempfile
    from pathlib import Path
    from somnia import composer, persona as P
    from somnia.config import Config

    d = Path(tempfile.mkdtemp())
    (d / "persona.md").write_text("## 身份\n\n我是测试人设，怕麻烦\n", encoding="utf-8")
    (d / "SOUL.md").write_text("## 硬规则\n\n不许索取\n", encoding="utf-8")
    per = P.load(str(d))

    base = Config.from_env()
    off = composer.build_prompt(dataclasses.replace(base, prompt_persona=False),
                                [], [], "在吗", 1, persona=per)
    on = composer.build_prompt(dataclasses.replace(base, prompt_persona=True),
                               [], [], "在吗", 1, persona=per)
    assert "怕麻烦" not in off and "不许索取" in off
    assert "怕麻烦" in on and "不许索取" in on


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
