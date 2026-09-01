"""extractor 单元测试：parse_token_id / save_original_params / inject_params /
extract_chat / extract_completions / strip_chat / strip_completions（spec §2.2 §2.3）。"""
from __future__ import annotations

import json

from _helpers import (
    build_chat_response,
    build_completions_response,
    chat_top_entry,
)
from anomaly_middleware.extractor import (
    OriginalParams,
    extract_chat_response,
    extract_chat_text_tokenids,
    extract_completions_response,
    extract_completions_text_tokenids,
    inject_params,
    parse_token_id,
    save_original_params,
    strip_chat_response,
    strip_completions_response,
)

NI = "你"  # E4 BD A0 = [228,189,160]
HAO = "好"  # E5 A5 BD = [229,165,189]


# --------------------------- parse_token_id --------------------------- #
def test_parse_token_id_formats():
    assert parse_token_id("token_id:1234") == 1234
    assert parse_token_id("1234") == 1234
    assert parse_token_id(1234) == 1234
    assert parse_token_id(" token_id:5678 ") == 5678
    assert parse_token_id("token_id:abc") == -1
    assert parse_token_id("xyz") == -1
    assert parse_token_id("") == -1
    assert parse_token_id(None) == -1
    assert parse_token_id(True) == -1  # bool 不当 int


# --------------------------- save_original_params --------------------------- #
def test_save_original_params_chat():
    body = {
        "model": "glm-4-7",
        "logprobs": True,
        "top_logprobs": 5,
        "return_tokens_as_token_ids": True,
        "n": 4,
        "stream": True,
    }
    orig = save_original_params(body, True)
    assert orig.is_chat is True
    assert orig.logprobs is True
    assert orig.top_logprobs == 5
    assert orig.return_tokens_as_token_ids is True
    assert orig.n == 4
    assert orig.stream is True


def test_save_original_params_completions_defaults():
    body = {"model": "glm-4-7", "logprobs": 3}
    orig = save_original_params(body, False)
    assert orig.is_chat is False
    assert orig.logprobs == 3
    assert orig.top_logprobs is None
    assert orig.return_tokens_as_token_ids is False
    assert orig.n == 1
    assert orig.stream is False


def test_save_original_params_no_logprobs():
    orig = save_original_params({"model": "m"}, True)
    assert orig.logprobs is None
    assert orig.top_logprobs is None
    assert orig.return_tokens_as_token_ids is False


# --------------------------- inject_params --------------------------- #
def test_inject_chat_no_logprobs():
    body = {"model": "m", "messages": []}
    out = inject_params(body, True, 20)
    parsed = json.loads(out)
    assert parsed["logprobs"] is True
    assert parsed["top_logprobs"] == 20
    assert parsed["return_tokens_as_token_ids"] is True
    # Content-Length 由 middleware patch；此处仅校验 body 长度可序列化
    assert len(out) == len(out)


def test_inject_chat_max_client_vs_n():
    # 客户端 top_logprobs=5, N=20 → 注入 20
    out = inject_params({"top_logprobs": 5}, True, 20)
    assert json.loads(out)["top_logprobs"] == 20
    # 客户端 top_logprobs=10, N=5 → 注入 10
    out = inject_params({"top_logprobs": 10}, True, 5)
    assert json.loads(out)["top_logprobs"] == 10


def test_inject_completions_max():
    # 客户端 logprobs=5, N=20 → 20
    out = inject_params({"logprobs": 5}, False, 20)
    p = json.loads(out)
    assert p["logprobs"] == 20
    assert p["return_tokens_as_token_ids"] is True
    # 客户端 logprobs=10, N=5 → 10
    out = inject_params({"logprobs": 10}, False, 5)
    assert json.loads(out)["logprobs"] == 10
    # 未带 → N
    out = inject_params({}, False, 7)
    assert json.loads(out)["logprobs"] == 7


# --------------------------- extract --------------------------- #
def test_extract_chat_truncates_to_n():
    e = chat_top_entry(100, NI, -0.1, n_top=20)
    data = build_chat_response("glm-4-7", [e])
    res = extract_chat_response(data, n_detect=4)
    assert len(res) == 1
    logprobs, token_ids = res[0]  # 2D: (num_tokens, n_detect)
    assert token_ids[:, 0].tolist() == [10000]  # top-1 候选 id（logprob 最高）
    assert logprobs.shape == (1, 4)  # 1 token, 截断到 N=4
    # 按 logprob 降序，第一项最大
    assert logprobs[0].tolist() == sorted(logprobs[0].tolist(), reverse=True)


def test_extract_completions_truncates_to_n():
    data = build_completions_response("glm-4-7", [100, 200], [-0.1, -0.2], n_top=20)
    res = extract_completions_response(data, n_detect=4)
    assert len(res) == 1
    logprobs, token_ids = res[0]  # 2D: (num_tokens, n_detect)
    assert token_ids[:, 0].tolist() == [10000, 10000]  # top-1 候选 id（每位置最高）
    assert logprobs.shape == (2, 4)  # 2 token, 每位置截断到 N=4


# --------------------------- strip chat --------------------------- #
def test_strip_chat_no_logprobs_becomes_null():
    e = chat_top_entry(100, NI, -0.1, n_top=20)
    data = build_chat_response("glm-4-7", [e])
    orig = OriginalParams(True, None, None, False, 1, False)  # 未请求 logprobs
    strip_chat_response(data, orig)
    assert data["choices"][0]["logprobs"] is None
    assert "token_id:" not in json.dumps(data, ensure_ascii=False)


def test_strip_chat_truncate_decode_text():
    e = chat_top_entry(100, NI, -0.1, n_top=20)
    data = build_chat_response("glm-4-7", [e])
    orig = OriginalParams(True, True, 3, False, 1, False)  # logprobs=true,top_logprobs=3
    strip_chat_response(data, orig)
    entry = data["choices"][0]["logprobs"]["content"][0]
    assert entry["token"] == NI  # 从 bytes 解码为文本，非 token_id:
    assert len(entry["top_logprobs"]) == 3
    for tp in entry["top_logprobs"]:
        assert tp["token"] == NI
    assert "token_id:" not in json.dumps(data, ensure_ascii=False)


def test_strip_chat_keep_token_ids_when_requested():
    e = chat_top_entry(100, NI, -0.1, n_top=20)
    data = build_chat_response("glm-4-7", [e])
    orig = OriginalParams(True, True, 3, True, 1, False)  # return_tokens_as_token_ids=True
    strip_chat_response(data, orig)
    entry = data["choices"][0]["logprobs"]["content"][0]
    assert entry["token"] == "token_id:100"  # 原样保留
    assert len(entry["top_logprobs"]) == 3
    assert entry["top_logprobs"][0]["token"].startswith("token_id:")


def test_strip_chat_detect_truncate_vs_client_truncate():
    # 客户端 logprobs=10, N=4：注入 10，每 token 10 项；检测截前 4，返回客户端 10
    e = chat_top_entry(100, NI, -0.1, n_top=10)
    data = build_chat_response("glm-4-7", [e])
    # 检测抽取截断到 N=4
    res = extract_chat_response(data, n_detect=4)
    assert len(res[0][0][0]) == 4
    # 客户端恢复截断到 10
    orig = OriginalParams(True, True, 10, False, 1, False)
    strip_chat_response(data, orig)
    entry = data["choices"][0]["logprobs"]["content"][0]
    assert len(entry["top_logprobs"]) == 10


def test_strip_chat_no_bytes_no_resolver_fallback_to_token_id():
    # 触发条件命中（chat + logprobs=True + top_logprobs=3 + rtati=False + resolver=None）
    # 无 bytes → 三层兜底落末层 token_id:NNN（§4.7 降级例外）
    e = chat_top_entry(100, NI, -0.1, n_top=5)
    e["bytes"] = None  # 主 token 无 bytes
    for tp in e["top_logprobs"]:
        tp["bytes"] = None  # top_logprobs 无 bytes
    data = build_chat_response("glm-4-7", [e])
    orig = OriginalParams(True, True, 3, False, 1, False)
    strip_chat_response(data, orig)  # resolver 默认 None
    entry = data["choices"][0]["logprobs"]["content"][0]
    assert entry["token"] == "token_id:100"  # 三层兜底落 token_id
    for tp in entry["top_logprobs"]:
        assert tp["token"].startswith("token_id:")  # top_logprobs 同样回退
    assert "token_id:" in json.dumps(data, ensure_ascii=False)


def test_strip_chat_n_choices_loop():
    e = chat_top_entry(100, NI, -0.1, n_top=5)
    data = build_chat_response("glm-4-7", [e], n=4)
    orig = OriginalParams(True, None, None, False, 4, False)
    strip_chat_response(data, orig)
    for choice in data["choices"]:
        assert choice["logprobs"] is None
    assert "token_id:" not in json.dumps(data, ensure_ascii=False)


# --------------------------- strip completions --------------------------- #
def test_strip_completions_no_logprobs_null():
    data = build_completions_response("glm-4-7", [100], [-0.1], n_top=20)
    orig = OriginalParams(False, None, None, False, 1, False)
    strip_completions_response(data, orig)
    assert data["choices"][0]["logprobs"] is None
    assert "token_id:" not in json.dumps(data, ensure_ascii=False)


def test_strip_completions_token_ids_kept_truncated():
    data = build_completions_response("glm-4-7", [100, 200], [-0.1, -0.2], n_top=20)
    orig = OriginalParams(False, 3, None, True, 1, False)  # logprobs=3, token_ids=True
    strip_completions_response(data, orig)
    lp = data["choices"][0]["logprobs"]
    assert lp["tokens"] == ["token_id:100", "token_id:200"]  # 原样保留
    assert len(lp["top_logprobs"]) == 2
    for pos in lp["top_logprobs"]:
        assert len(pos) == 3  # 截断到 3
        assert all(k.startswith("token_id:") for k in pos.keys())


def test_strip_completions_no_resolver_fallback_to_token_id():
    # 触发条件命中（completions + logprobs=3 + rtati=False + resolver=None）
    # completions 无 bytes → tokens/top_logprobs 回退 token_id:NNN，保证 topk logprob 数据不丢失
    data = build_completions_response("glm-4-7", [100, 200], [-0.1, -0.2], n_top=20)
    orig = OriginalParams(False, 3, None, False, 1, False)
    strip_completions_response(data, orig)  # resolver 默认 None
    lp = data["choices"][0]["logprobs"]
    assert lp["tokens"] == ["token_id:100", "token_id:200"]
    assert len(lp["top_logprobs"]) == 2
    for pos in lp["top_logprobs"]:
        assert len(pos) == 3  # 截断到 3
        assert all(k.startswith("token_id:") for k in pos.keys())
    assert "token_id:" in json.dumps(data, ensure_ascii=False)


# --------------------------- strip with resolver --------------------------- #
class _FakeTok:
    def __init__(self, m):
        self._m = m

    def decode(self, ids, **kw):
        return "".join(self._m.get(i, "") for i in ids)


def _resolver(mapping):
    from anomaly_middleware.token_resolver import TokenTextResolver

    return TokenTextResolver(_FakeTok(mapping))


def test_strip_chat_top_logprobs_resolver_first_with_broken_bytes():
    # 真实 vLLM 形态：top bytes 破损（解码为 token_id: 字符串）
    e = chat_top_entry(100, NI, -0.1, n_top=5, vllm_broken_top_bytes=True)
    data = build_chat_response("glm-4-7", [e])
    orig = OriginalParams(True, True, 3, False, 1, False)
    r = _resolver({100: NI, 10000: "甲", 10001: "乙", 10002: "丙"})
    strip_chat_response(data, orig, r)
    entry = data["choices"][0]["logprobs"]["content"][0]
    assert entry["token"] == NI  # 主 token resolver 优先
    assert len(entry["top_logprobs"]) == 3
    for tp in entry["top_logprobs"]:
        assert tp["token"] in ("甲", "乙", "丙")  # resolver 文本，非 token_id:
    assert "token_id:" not in json.dumps(data, ensure_ascii=False)


def test_strip_chat_top_logprobs_no_resolver_fallback_to_token_id():
    # 触发条件命中 + resolver=None + 破损 bytes
    # 主 token: bytes 真实文本 → 三层第二层（NI）
    # top_logprobs: bytes 破损（解码出 token_id: 前缀，守卫拒绝）→ 三层第三层（token_id:NNN）
    e = chat_top_entry(100, NI, -0.1, n_top=5, vllm_broken_top_bytes=True)
    data = build_chat_response("glm-4-7", [e])
    orig = OriginalParams(True, True, 3, False, 1, False)
    strip_chat_response(data, orig)  # resolver 默认 None
    entry = data["choices"][0]["logprobs"]["content"][0]
    assert entry["token"] == NI  # 主 token 三层第二层（bytes 真实文本）
    assert len(entry["top_logprobs"]) == 3
    for tp in entry["top_logprobs"]:
        assert tp["token"].startswith("token_id:")  # 三层第三层（token_id 回退）
    assert "token_id:" in json.dumps(data, ensure_ascii=False)


def test_strip_completions_resolver_text():
    data = build_completions_response("glm-4-7", [100, 200], [-0.1, -0.2], n_top=5)
    orig = OriginalParams(False, 3, None, False, 1, False)
    r = _resolver({100: "你", 200: "好", 10000: "甲", 10001: "乙", 10002: "丙"})
    strip_completions_response(data, orig, r)
    lp = data["choices"][0]["logprobs"]
    assert lp["tokens"] == ["你", "好"]
    assert len(lp["top_logprobs"]) == 2
    for pos in lp["top_logprobs"]:
        assert len(pos) == 3  # 截断到 3
        assert all(isinstance(k, str) and not k.startswith("token_id:") for k in pos)
    assert "token_id:" not in json.dumps(data, ensure_ascii=False)


def test_strip_completions_no_resolver_still_null():
    # 客户端未请求 logprobs（completions 无 topk）→ 中间件置 choice.logprobs=null，
    # 不触发降级回退，全文无 token_id:
    data = build_completions_response("glm-4-7", [100, 200], [-0.1, -0.2], n_top=20)
    orig = OriginalParams(False, None, None, False, 1, False)  # 未请求 logprobs
    strip_completions_response(data, orig)
    assert data["choices"][0]["logprobs"] is None
    assert "token_id:" not in json.dumps(data, ensure_ascii=False)


def test_strip_chat_main_token_resolver_first_then_bytes():
    # resolver 有该 id → 用 resolver 文本（即使 bytes 也在）
    e = chat_top_entry(100, NI, -0.1, n_top=3)
    data = build_chat_response("glm-4-7", [e])
    orig = OriginalParams(True, True, 2, False, 1, False)
    r = _resolver({100: "X"})
    strip_chat_response(data, orig, r)
    entry = data["choices"][0]["logprobs"]["content"][0]
    assert entry["token"] == "X"  # resolver 优先，覆盖 bytes 的 NI


# ------------------- 降级回退 token_id:NNN（§4.7 例外）------------------- #
def test_strip_chat_no_topk_no_resolver_still_null():
    # chat + logprobs=True + top_logprobs=None → 不触发降级（未请求 topk）
    # 主 token: bytes 兜底（NI）；top_logprobs 空 list（m=None → []）；全文无 token_id:
    e = chat_top_entry(100, NI, -0.1, n_top=5, vllm_broken_top_bytes=True)
    data = build_chat_response("glm-4-7", [e])
    orig = OriginalParams(True, True, None, False, 1, False)  # top_logprobs=None
    strip_chat_response(data, orig)  # resolver 默认 None
    entry = data["choices"][0]["logprobs"]["content"][0]
    assert entry["token"] == NI  # 主 token bytes 真实文本（三层第二层，未触发降级）
    assert entry["top_logprobs"] == []  # m=None → 空 list
    assert "token_id:" not in json.dumps(data, ensure_ascii=False)


def test_strip_chat_main_token_bytes_broken_falls_to_id():
    # 触发条件命中 + 主 token bytes 破碎（解码出 token_id: 前缀，守卫拒绝）
    # 三层兜底：resolver(None) → bytes(破碎,守卫拒绝) → token_id:NNN
    e = chat_top_entry(100, NI, -0.1, n_top=5, vllm_broken_top_bytes=True)
    e["bytes"] = list(f"token_id:100".encode("utf-8"))  # 主 token bytes 也破碎
    data = build_chat_response("glm-4-7", [e])
    orig = OriginalParams(True, True, 3, False, 1, False)
    strip_chat_response(data, orig)  # resolver 默认 None
    entry = data["choices"][0]["logprobs"]["content"][0]
    assert entry["token"] == "token_id:100"  # 三层第三层（token_id 回退）
    for tp in entry["top_logprobs"]:
        assert tp["token"].startswith("token_id:")
    assert "token_id:" in json.dumps(data, ensure_ascii=False)


def test_strip_chat_resolver_available_no_fallback_to_id():
    # 触发条件本应命中（topk + rtati=False + resolver 本应为 None）但 resolver 可用
    # → resolver 文本优先，不进入降级；全文无 token_id:
    e = chat_top_entry(100, NI, -0.1, n_top=5, vllm_broken_top_bytes=True)
    data = build_chat_response("glm-4-7", [e])
    orig = OriginalParams(True, True, 3, False, 1, False)
    r = _resolver({100: "X", 10000: "甲", 10001: "乙", 10002: "丙", 10003: "丁", 10004: "戊"})
    strip_chat_response(data, orig, r)
    entry = data["choices"][0]["logprobs"]["content"][0]
    assert entry["token"] == "X"  # resolver 文本，非 token_id:
    for tp in entry["top_logprobs"]:
        assert tp["token"] in ("甲", "乙", "丙")  # resolver 文本（截断到 3）
    assert "token_id:" not in json.dumps(data, ensure_ascii=False)


def test_strip_completions_rtati_true_no_fallback():
    # completions + rtati=True + resolver=None → passthrough，不触发降级
    # 原样保留 token_id:NNN（这正是客户端所要），与降级回退走不同代码路径
    data = build_completions_response("glm-4-7", [100, 200], [-0.1, -0.2], n_top=20)
    orig = OriginalParams(False, 3, None, True, 1, False)  # rtati=True
    strip_completions_response(data, orig)  # resolver 默认 None
    lp = data["choices"][0]["logprobs"]
    assert lp["tokens"] == ["token_id:100", "token_id:200"]  # passthrough 原样
    for pos in lp["top_logprobs"]:
        assert all(k.startswith("token_id:") for k in pos.keys())  # passthrough 原样


# --------------------------- 多候选 n>1（spec §2.3：循环处理每份候选） --------------------------- #
def test_strip_completions_n_choices_loop_with_resolver():
    data = build_completions_response("glm-4-7", [100, 200], [-0.1, -0.2],
                                      n_top=5, n=3)
    orig = OriginalParams(False, 3, None, False, 3, False)
    r = _resolver({100: "你", 200: "好", 10000: "甲", 10001: "乙", 10002: "丙"})
    strip_completions_response(data, orig, r)
    for choice in data["choices"]:
        lp = choice["logprobs"]
        assert lp["tokens"] == ["你", "好"]
        for pos in lp["top_logprobs"]:
            assert len(pos) == 3  # 每份候选都截断到 3
            assert all(not k.startswith("token_id:") for k in pos)
    assert "token_id:" not in json.dumps(data, ensure_ascii=False)


def test_extract_completions_n_choices_all_returned():
    data = build_completions_response("glm-4-7", [100, 200], [-0.1, -0.2],
                                      n_top=20, n=3)
    res = extract_completions_response(data, n_detect=4)
    assert len(res) == 3  # per choice
    for logprobs, token_ids in res:
        assert token_ids[:, 0].tolist() == [10000, 10000]  # top-1 候选 id
        assert logprobs.shape[0] == 2  # 2 token per choice


def test_extract_completions_none_position_handled():
    # top_logprobs 中某位置为 None -> 抽取为填充 (-100.0, 0)，不崩溃
    data = build_completions_response("glm-4-7", [100, 200], [-0.1, -0.2], n_top=20)
    data["choices"][0]["logprobs"]["top_logprobs"][1] = None
    res = extract_completions_response(data, n_detect=4)
    logprobs, token_ids = res[0]
    assert logprobs.shape == (2, 4)
    # pos 0 正常 top-1 候选 = 10000；pos 1 = None -> 填充 token_id=0
    assert token_ids[:, 0].tolist() == [10000, 0]
    assert token_ids[1].tolist() == [0, 0, 0, 0]
    assert logprobs[1].tolist() == [-100.0, -100.0, -100.0, -100.0]


# --------------------------- extract text tokenids --------------------------- #
def test_extract_chat_text_tokenids_basic():
    e1 = chat_top_entry(100, NI, -0.1, n_top=5)
    e2 = chat_top_entry(200, HAO, -0.2, n_top=5)
    data = build_chat_response("m", [e1, e2])
    result = extract_chat_text_tokenids(data)
    assert result == [[100, 200]]


def test_extract_chat_text_tokenids_multi_choice():
    e = chat_top_entry(100, NI, -0.1, n_top=5)
    data = build_chat_response("m", [e], n=3)
    result = extract_chat_text_tokenids(data)
    assert result == [[100], [100], [100]]


def test_extract_chat_text_tokenids_no_logprobs():
    data = {"choices": [{"message": {"content": "x"}}]}
    result = extract_chat_text_tokenids(data)
    assert result == [[]]


def test_extract_chat_text_tokenids_parse_failure():
    e = {"token": "bad", "logprob": -0.1, "bytes": None, "top_logprobs": []}
    data = build_chat_response("m", [e])
    result = extract_chat_text_tokenids(data)
    assert result == [[-1]]


def test_extract_completions_text_tokenids_basic():
    data = build_completions_response("m", [100, 200], [-0.1, -0.2], n_top=5)
    result = extract_completions_text_tokenids(data)
    assert result == [[100, 200]]


def test_extract_completions_text_tokenids_multi_choice():
    data = build_completions_response("m", [100], [-0.1], n_top=5, n=2)
    result = extract_completions_text_tokenids(data)
    assert result == [[100], [100]]


def test_extract_completions_text_tokenids_no_logprobs():
    data = {"choices": [{"text": "x"}]}
    result = extract_completions_text_tokenids(data)
    assert result == [[]]
