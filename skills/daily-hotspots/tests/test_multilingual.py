"""R1 headroom — multilingual / CJK entity-normalization robustness (self-evolve 0->1).

The acceptance-gate suite was ASCII-only. `extract_entities` / `simhash` use a token regex
that matches `[a-z0-9]...` exclusively, so any CJK (Chinese / Japanese / Korean) text is
silently dropped. That is a real correctness hazard for a radar whose own architecture
targets 出海/跨境 and Chinese-language sources:

  * a CJK-only title yields entities == []  -> canonical_key collapses to "::<track>"
  * two *different* CJK opportunities in the same track then share one canonical_key
    -> exact-key dedup merges them = silent data loss / false SUPPRESS
  * simhash(CJK) == 0 -> the soft near-dup layer is also blind to CJK

These cases are marked xfail on the current implementation (they expose the gap without
breaking the green baseline) and become permanent regression guards once a CJK-aware
tokenizer lands. Each case asserts a *capability* (CJK survives, keys stay distinct), not
one specific segmentation strategy, so any reasonable fix satisfies them.
"""
import pytest

from lib import extract_entities, canonical_key, simhash

xfail_cjk = pytest.mark.xfail(
    reason="R1 headroom: CJK/multilingual normalization not yet supported", strict=False
)


def _has_cjk_token(tokens):
    return any(any("一" <= ch <= "鿿" or "぀" <= ch <= "ヿ"
                   or "가" <= ch <= "힯" for ch in t) for t in tokens)


# --- A. CJK-only titles must yield non-empty, genuinely-CJK entity sets ---------
CJK_TITLES = [
    "开源大模型推理引擎与智能体框架",
    "区块链去中心化交易所协议",
    "完全不同的中文医疗健康话题",
    "国产数据库向量检索新方案",
    "跨境电商海外仓自动化系统",
]


@xfail_cjk
@pytest.mark.parametrize("title", CJK_TITLES)
def test_cjk_entities_not_dropped(title):
    ents = extract_entities(title)
    assert ents, f"CJK title produced no entities: {title!r}"
    assert _has_cjk_token(ents), f"no CJK token captured from {title!r}: {ents}"


# --- B. distinct CJK opportunities in the SAME track keep DISTINCT keys ----------
CJK_DISTINCT_PAIRS = [
    ("开源大模型推理引擎与智能体框架", "完全不同的中文医疗健康话题"),
    ("国产数据库向量检索新方案", "跨境电商海外仓自动化系统"),
    ("中文语音合成实时克隆服务", "工业物联网边缘计算网关"),
]


@xfail_cjk
@pytest.mark.parametrize("a,b", CJK_DISTINCT_PAIRS)
def test_cjk_no_canonical_collapse(a, b):
    ka = canonical_key(extract_entities(a), "ai-agents")
    kb = canonical_key(extract_entities(b), "ai-agents")
    assert ka != kb, f"distinct CJK titles collapsed to same key {ka!r}"


# --- C. simhash must see CJK content (non-zero + distinct) -----------------------
@xfail_cjk
@pytest.mark.parametrize("title", ["开源大模型推理引擎与智能体框架", "区块链去中心化交易所协议"])
def test_cjk_simhash_nonzero(title):
    assert simhash(title) != 0, f"simhash(CJK) is 0 for {title!r}"


@xfail_cjk
def test_cjk_simhash_distinct():
    assert simhash("开源大模型推理引擎与智能体框架") != simhash("区块链去中心化交易所协议")


# --- D. mixed CN/EN keeps BOTH the ASCII entity and a CJK token ------------------
@xfail_cjk
@pytest.mark.parametrize("title", [
    "MinerU 开源 PDF 文档解析工具",
    "GPT-4 国产平替推理框架发布",
])
def test_mixed_lang_keeps_ascii_and_cjk(title):
    ents = extract_entities(title)
    assert any(t.isascii() for t in ents), f"lost ASCII entity in {title!r}: {ents}"
    assert _has_cjk_token(ents), f"lost CJK entity in {title!r}: {ents}"
