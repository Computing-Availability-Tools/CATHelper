#!/usr/bin/env python3
# -------------------------------------------------------------------------
#  This file is part of the MindStudio project.
# Copyright (c) 2025 Huawei Technologies Co.,Ltd.
#
# MindStudio is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the
# Mulan PSL v2. You may obtain a copy of Mulan PSL v2 at:
#
#          http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY
# KIND, EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# NON-INFRINGEMENT, MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# -------------------------------------------------------------------------
"""独立可执行脚本：离线生成词表类别映射 + token 文本表（design_pd_proxy.md §5.7.1）。

本脚本不依赖 anomaly_middleware 包（分类逻辑与包内 token_categorizer.py 一致），
可单独拷贝到有模型/Tokenizer 的服务器上执行（仅需 `pip install transformers`，
无需 GPU/NPU，不访问网络）：

    python gen_token_category.py --model-path /models/Qwen3-0.6B \
        [--model-name Qwen3-0.6B] [--output-dir /tmp/anomaly]

产物（同名成对，拷回 proxy 所在机器后按 --anomaly-token2category 指定具体文件）：
- <output-dir>/token2category/<model_name>_<vocab_size>.json
    {str(token_id): category} 全词表类别映射（检测器 tk2cat）
- <output-dir>/token_text/<model_name>_<vocab_size>.json
    {str(token_id): surface_text} 全词表 decode([id]) 文本表（proxy strip 还原）

文件名仅为人工识别用途，运行时不解析文件名语义；检测器 vocab_size 由
映射内容推断（max(键) + 1）。
"""
from __future__ import annotations

import argparse
import json
import os
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from transformers import AutoTokenizer

def parse_args():
    parser = argparse.ArgumentParser(
        prog="gen_token_category",
        description="离线生成词表类别映射 + token 文本表（供 PD 分离 proxy 精度异常检测使用）",
    )
    parser.add_argument(
        "--model-path",
        required=True,
        help="模型/Tokenizer 本地目录路径（离线加载，不访问网络）",
    )
    parser.add_argument(
        "--model-name",
        default=None,
        help="产物文件名标识（默认取 --model-path 的目录名；仅用于人工识别）",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="产物根目录（默认当前工作目录）",
    )
    parser.add_argument(
        "--no-trust-remote-code",
        action="store_true",
        help="禁用 trust_remote_code（默认启用，与推理侧加载行为一致）",
    )
    return parser.parse_args()


# --------------------------------------------------------------------------- #
# token 分类（与 anomaly_middleware/token_categorizer.py 保持逐行一致，
# 保证预生成类别与运行期检测算法的类别体系完全相同）
# --------------------------------------------------------------------------- #
@dataclass
class TokenInfo:
    token_id: int
    category: str


SCRIPT_LABELS = {
    "cjk": "chinese_cjk",
    "hiragana": "japanese_hiragana",
    "katakana": "japanese_katakana",
    "hangul": "korean_hangul",
    "thai": "thai",
    "greek": "greek",
    "variation_selector": "variation_selector",
    "latin": "english_latin",
    "latin_space": "english_latin_space",
    "digit": "numbers",
    "emoji": "emoji",
    "whitespace": "whitespace",
    "punct": "punctuation",
    "symbol": "symbol",
    "control": "control",
    "arabic": "arabic",
    "cyrillic": "cyrillic",
    "devanagari": "devanagari",
    "math_letter": "mathematics",
    "modifier_letter": "mathematics",
    "fraction": "mathematics",
}

PUNCT_CHARS = set("'`\".;:!?-–—()[]{}<>/\\@#*$%&+|~^=_")
WHITESPACE_CHARS = set(" \t\n\r\f\v▁ĠĊ█")


@lru_cache(maxsize=4096)
def _classify_char(ch):
    if ch in WHITESPACE_CHARS:
        return "whitespace"
    codepoint = ord(ch)
    if 0x1F300 <= codepoint <= 0x1FAFF:
        return "emoji"
    if ch.isdigit():
        return "digit"
    name = unicodedata.name(ch, "")
    if not name:
        category = unicodedata.category(ch)
        if category.startswith("C"):
            return "control"
        if category.startswith("P"):
            return "punct"
        if category.startswith("S"):
            return "symbol"
        return "other"
    name_upper = name.upper()
    if "PLANCK CONSTANT" in name_upper or "MATHEMATICAL" in name_upper or "DOUBLE-STRUCK CAPITAL" in name_upper:
        return "math_letter"
    if "MODIFIER LETTER" in name_upper:
        return "modifier_letter"
    if "SPACE" in name_upper or unicodedata.category(ch) in {"Zs", "Zl", "Zp"}:
        return "whitespace"
    if "CJK UNIFIED IDEOGRAPH" in name_upper or "CJK COMPATIBILITY" in name_upper:
        return "cjk"
    if "HIRAGANA" in name_upper:
        return "hiragana"
    if "HANGUL" in name_upper:
        return "hangul"
    if "THAI" in name_upper:
        return "thai"
    if "ARABIC" in name_upper:
        return "arabic"
    if "CYRILLIC" in name_upper:
        return "cyrillic"
    if "DEVANAGARI" in name_upper:
        return "devanagari"
    if "LATIN" in name_upper:
        return "latin"
    if "VARIATION SELECTOR" in name_upper:
        return "variation_selector"
    category = unicodedata.category(ch)
    if category.startswith("P"):
        return "punct"
    if category.startswith("S"):
        return "symbol"
    if category.startswith("C"):
        return "control"
    return "other"


def categorize_token(token_id, token_raw, decoded):
    char_counts = Counter()
    printable = 0
    for char in decoded:
        char_class = _classify_char(char)
        char_counts[char_class] += 1
        if char_class not in {"control"}:
            printable += 1

    total_chars = sum(char_counts.values()) or 1
    printable_fraction = printable / total_chars
    dominant, dom_count = char_counts.most_common(1)[0] if char_counts else ("other", 0)
    dominant_ratio = dom_count / total_chars

    label = SCRIPT_LABELS.get(dominant, "other")
    if dominant == "latin" and printable_fraction > 0.8:
        if "whitespace" in char_counts:
            label = "english_latin_space"
        else:
            label = "english_latin"
    elif dominant == "digit" and dominant_ratio > 0.6:
        label = "numbers"
    elif dominant == "punct" and dominant_ratio > 0.7:
        label = "punctuation"
    elif dominant == "symbol" and dominant_ratio > 0.6:
        label = "symbol_cluster"
    elif dominant == "control":
        label = "control_bytes"
    elif dominant in {
        "cjk",
        "hiragana",
        "katakana",
        "hangul",
        "thai",
        "greek",
        "variation_selector",
    }:
        label = SCRIPT_LABELS[dominant]
    elif dominant == "whitespace" and printable < 0.4:
        label = "whitespace"
    elif dominant_ratio < 0.5 and printable_fraction < 0.7:
        label = "mixed_noise"

    if label not in {"punctuation", "symbol_cluster", "numbers"} and dominant not in {
        "latin",
        "cjk",
        "hiragana",
        "katakana",
        "hangul",
        "thai",
    }:
        dense_symbol_ratio = (char_counts.get("symbol", 0) + char_counts.get("punct", 0)) / total_chars
        if dense_symbol_ratio > 0.6 and total_chars >= 3:
            label = "gibberish_symbols"
    return TokenInfo(token_id=token_id, category=label)


def invert_vocab(vocab):
    size = max(vocab.values()) + 1
    tokens = ["" for _ in range(size)]
    for token, idx in vocab.items():
        if idx < size:
            tokens[idx] = token
    return tokens


def _get_decode_fn(tokenizer):
    """返回 (token_str, idx) -> Optional[str] 的闭包，或 None。

    优先 backend_tokenizer.decoder.decode（最精确）；
    退到 tokenizer.decode([idx])（高层 API，覆盖慢速 tokenizer）；均无则 None。
    """
    backend = getattr(tokenizer, "backend_tokenizer", None)
    decoder = getattr(backend, "decoder", None) if backend is not None else None
    if decoder is not None and hasattr(decoder, "decode"):
        def _backend(token, idx):
            return decoder.decode([token])
        return _backend
    if hasattr(tokenizer, "decode"):
        def _highlevel(token, idx):
            return tokenizer.decode([idx])
        return _highlevel
    return None


def _safe_decode(decode_fn, token, idx):
    """逐 token decode + 异常吞掉；失败或无 decode_fn 返回 None（该 token 跳过）。"""
    if decode_fn is None:
        return None
    try:
        return decode_fn(token, idx)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 生成与落盘
# --------------------------------------------------------------------------- #
def build_mappings(tokenizer):
    """逐 token decode → 分类，一次遍历同时产出类别映射与文本表。

    个别 token decode 失败 → 跳过（不入两份映射），不影响其余 token。
    """
    vocab = tokenizer.get_vocab()
    tokens = invert_vocab(vocab)
    vocab_size = tokenizer.vocab_size

    decode_fn = _get_decode_fn(tokenizer)
    if decode_fn is None:
        raise RuntimeError(
            "tokenizer 无可用 decode 路径"
            "（backend_tokenizer.decoder.decode / tokenizer.decode 均缺失）"
        )

    tk2cat = {}
    token_text = {}
    for idx, token in enumerate(tokens):
        decoded = _safe_decode(decode_fn, token, idx)
        if decoded is None:
            continue
        info = categorize_token(idx, token, decoded)
        tk2cat[str(info.token_id)] = info.category
        token_text[str(info.token_id)] = decoded
    return tk2cat, token_text, vocab_size


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def main():
    args = parse_args()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    print(f"加载 tokenizer: {args.model_path}")
    tokenizer = AutoTokenizer.from_pretrained(
        args.model_path,
        trust_remote_code=not args.no_trust_remote_code,
        local_files_only=True,
    )

    model_name = args.model_name or os.path.basename(os.path.normpath(args.model_path))
    tk2cat, token_text, vocab_size = build_mappings(tokenizer)
    if not tk2cat:
        raise RuntimeError(
            "词表映射为空：tokenizer 无任何 token 可解码，请检查模型文件完整性"
        )

    name = f"{model_name}_{vocab_size}"
    tk2cat_dir = os.path.join(args.output_dir, "token2category")
    text_dir = os.path.join(args.output_dir, "token_text")
    os.makedirs(tk2cat_dir, exist_ok=True)
    os.makedirs(text_dir, exist_ok=True)

    tk2cat_path = os.path.join(tk2cat_dir, f"{name}.json")
    text_path = os.path.join(text_dir, f"{name}.json")
    _save_json(tk2cat_path, tk2cat)
    _save_json(text_path, token_text)

    print("生成完成（两文件同名成对，文本表供 proxy strip 还原使用）:")
    print(f"  token2category: {tk2cat_path} (entries={len(tk2cat)})")
    print(f"  token_text:     {text_path} (entries={len(token_text)})")
    print("请将两文件拷贝到 proxy 所在机器，启动参数示例:")
    print(f"  --anomaly-token2category <path>/{os.path.basename(tk2cat_path)}")


if __name__ == "__main__":
    main()
