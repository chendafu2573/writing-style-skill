#!/usr/bin/env python3
"""Run deterministic checks for a generated Chinese social draft."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ASSISTANT_PHRASES = (
    "值得注意的是", "综上所述", "现在回答一个简单的问题",
    "我先说", "接下来我们看看", "这才是大家真正",
    "我只站自己的钱", "真正的问题不是",
)


def check(text: str) -> dict:
    findings = []
    for phrase in ASSISTANT_PHRASES:
        if phrase in text:
            findings.append({"type": "assistant_phrase", "text": phrase})
    if re.search(r"玩家普遍|社区真正的情绪|大家都认为", text):
        findings.append({"type": "unsupported_group_claim", "text": "群体概括需有来源"})
    if text.count("不是") >= 3:
        findings.append({"type": "contrast_density", "text": "否定式转折密度偏高"})
    sentences = [s for s in re.split(r"[。！？!?；;]+|\n+", text) if s.strip()]
    return {
        "assistant_phrase_count": sum(1 for item in findings if item["type"] == "assistant_phrase"),
        "sentence_count": len(sentences),
        "findings": findings,
        "status": "WARN" if findings else "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("text", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.text.read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
