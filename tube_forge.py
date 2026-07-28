#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TubeForge — 选题 → AI 写稿 → 分场景 → 输出 ClipForge 兼容 project.yaml

用法:
  tube-forge run "你的选题" > projects/<slug>/project.yaml
  tube-forge run "你的选题" -o projects/<slug>/project.yaml

依赖:
  pip install requests pyyaml
  环境变量: DEEPSEEK_API_KEY (必填)

输出:
  project.yaml — 直接喂给 ClipForge: clipforge.py all projects/<slug>
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("缺少依赖: pip install pyyaml requests", file=sys.stderr)
    sys.exit(1)

import requests


# ---------------- 常量 ----------------

SYSTEM_PROMPT = """你是一个 YouTube 视频脚本写手。你的任务：
1. 根据用户给定的选题，写一段 2~8 分钟的短视频口播脚本
2. 将脚本按"一个场景 = 一个画面 = 一段口播"的原则分成 8~40 个场景
3. 每场景给出 text(口播文案)、keywords(3个英文搜索词)、asset_type(video/image/auto)

输出格式：纯 JSON 数组，不要 markdown 代码块，不要多余文字。
数组每个元素格式：
{
  "text": "口播文案，20~60字",
  "keywords": ["keyword1", "keyword2", "keyword3"],
  "asset_type": "video"
}

规则：
- 视频结构：Hook(前8秒吸引) → Setup(8-25秒铺垫) → Body(展开论述) → CTA(最后10秒呼吁)
- 开头 Hook 前5秒内必须有悬念/反常识内容
- 每句口播 20~60 字，约 8~15 秒
- 全片场景数按目标时长：2-3min=10-16场景，4-6min=18-28场景，7-10min=28-45场景
- 每场景必须给 **3 个英文 keywords**（第1个最精确，第2/3个为 fallback）
- keywords 必须全是：英文、全小写、名词性、有具体画面感
- ✅ 正确示例：["stock market chart", "bullish trend", "trading floor"]
- ❌ 禁止示例：["success", "growth", "finance concept"]
- 相邻场景的 keywords 必须差异大，避免搜到相同素材
- 抽象概念必须转成具体视觉画面（如"复利"→"coins stacking timelapse"）
- 动态/动作内容用 "video"，静态概念用 "image"，不确定用 "auto"
- 数字写为朗读形式（"99%"→"百分之九十九"），不用缩写
"""


# ---------------- API 调用 ----------------

def call_deepseek(prompt: str) -> str:
    """调用 DeepSeek API 生成脚本。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("[tubeforge] 错误: 需要设置 DEEPSEEK_API_KEY 环境变量", file=sys.stderr)
        print("[tubeforge] 设置方式: export DEEPSEEK_API_KEY=sk-xxx", file=sys.stderr)
        sys.exit(1)

    try:
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "temperature": 0.7,
                "max_tokens": 4096,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"选题: {prompt}"},
                ],
            },
            timeout=120,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return content
    except requests.exceptions.Timeout:
        print("[tubeforge] DeepSeek API 请求超时(120s)", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"[tubeforge] DeepSeek API 调用失败: {e}", file=sys.stderr)
        if hasattr(e, "response") and e.response is not None:
            print(f"[tubeforge] 响应: {e.response.text[:500]}", file=sys.stderr)
        sys.exit(1)


def extract_scenes(content: str) -> list:
    """从 LLM 响应中提取 JSON 场景数组。"""
    # 先尝试直接解析
    content = content.strip()
    try:
        scenes = json.loads(content)
        if isinstance(scenes, list):
            return scenes
    except json.JSONDecodeError:
        pass

    # 尝试提取 ```json ... ``` 块
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
    if m:
        try:
            scenes = json.loads(m.group(1).strip())
            if isinstance(scenes, list):
                return scenes
        except json.JSONDecodeError:
            pass

    # 尝试提取 [...] 数组
    m = re.search(r"\[.*?\]", content, re.DOTALL)
    if m:
        try:
            scenes = json.loads(m.group(0))
            if isinstance(scenes, list):
                return scenes
        except json.JSONDecodeError:
            pass

    print("[tubeforge] 无法从 AI 响应中提取场景 JSON", file=sys.stderr)
    print(f"[tubeforge] 原始响应前 500 字符:\n{content[:500]}", file=sys.stderr)
    sys.exit(1)


def validate_scenes(scenes: list) -> list:
    """校验并清洗场景数据。"""
    valid = []
    for i, sc in enumerate(scenes, 1):
        if not isinstance(sc, dict):
            continue
        text = str(sc.get("text", "")).strip()
        if not text:
            continue
        keywords = sc.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [keywords]
        keywords = [str(k) for k in keywords if k][:3]
        if not keywords:
            keywords = ["background visuals"]

        asset_type = str(sc.get("asset_type", "auto")).strip().lower()
        if asset_type not in ("video", "image", "auto"):
            asset_type = "auto"

        valid.append({
            "text": text,
            "keywords": keywords,
            "asset_type": asset_type,
        })
    return valid


# ---------------- 输出 project.yaml ----------------

def build_project_yaml(title: str, scenes: list) -> str:
    """构建 ClipForge 兼容的 project.yaml 内容。"""
    lines = [
        f"# TubeForge 生成 — {title}",
        f"title: {title}",
        "voice: zh-CN-YunxiNeural        # edge-tts 声音",
        "rate: \"+8%\"                     # 语速微调",
        "subtitle_mode: sentence         # sentence=按句均摊时间",
        "bgm: \"\"                         # 可选: BGM 文件路径",
        "bgm_volume: 0.12",
        "orientation: landscape",
        "",
        "scenes:",
    ]
    for sc in scenes:
        lines.append(f"  - text: {sc['text']}")
        kw_str = json.dumps(sc["keywords"], ensure_ascii=False)
        lines.append(f"    keywords: {kw_str}")
        lines.append(f"    asset_type: {sc['asset_type']}")
    return "\n".join(lines)


# ---------------- CLI ----------------

def cmd_run(args):
    """主命令: 选题 → 写稿 → 输出 project.yaml。"""
    topic = args.topic
    print(f"[tubeforge] 选题: {topic}", file=sys.stderr)
    print(f"[tubeforge] 正在调用 DeepSeek 写稿...", file=sys.stderr)

    raw = call_deepseek(topic)
    scenes = extract_scenes(raw)
    scenes = validate_scenes(scenes)

    if len(scenes) < 3:
        print(f"[tubeforge] 警告: 仅生成 {len(scenes)} 个场景，建议 8~20 个", file=sys.stderr)

    print(f"[tubeforge] 生成 {len(scenes)} 个场景", file=sys.stderr)

    # 从选题中提取短标题
    title = topic
    if len(title) > 60:
        title = title[:57] + "..."

    yaml_content = build_project_yaml(title, scenes)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(yaml_content, encoding="utf-8")
        print(f"[tubeforge] 已输出: {out_path}", file=sys.stderr)
    else:
        # 输出到 stdout，可 pipe 到文件
        print(yaml_content)


def main():
    ap = argparse.ArgumentParser(
        description="TubeForge — 选题 → AI 写稿 → 分场景 → project.yaml"
    )
    ap.add_argument("topic", nargs="?", help="视频选题")
    ap.add_argument("-o", "--output", help="输出路径（默认 stdout）")

    args = ap.parse_args()

    if not args.topic:
        # 交互模式
        topic = input("请输入选题: ").strip()
        if not topic:
            print("选题不能为空", file=sys.stderr)
            sys.exit(1)
        args.topic = topic

    cmd_run(args)


if __name__ == "__main__":
    main()
