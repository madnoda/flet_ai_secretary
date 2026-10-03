#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IC recorder -> Whisper -> corrected transcript -> Markdown plan -> JSON.

V7の基本方針
- LLMには、人間が読みやすいMarkdownの時間割作成だけを任せる。
- Markdownを正本として、そのまま保存する。
- JSONはPythonがMarkdownを解析して生成する。
- JSON変換が不完全でもMarkdownは必ず保存する。
- Whisper誤認識補正、OpenAI/LocalLLM共通化、llama-server自動起動はV5を継承する。

注意
- このプログラムは試作品です。実運用を通じて、仕様・JSON構造・補正辞書などを
  今後変更する可能性があります。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.getenv("AI_SECRETARY_CONFIG", str(ROOT / "config.toml"))).expanduser()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


CONFIG = load_config()
IC_CONFIG = CONFIG.get("ic_recorder", {})
PATHS_CONFIG = CONFIG.get("paths", {})
SERVICES_CONFIG = CONFIG.get("services", {})
LLM_CONFIG = CONFIG.get("llm", {})

LLM_CTX_SIZE = int(LLM_CONFIG.get("ctx_size", 24576))
LLM_NGL = int(LLM_CONFIG.get("ngl", 10))

IC_RECORDER_DIR = Path(os.getenv(
    "IC_RECORDER_DIR",
    str(IC_CONFIG.get("directory", Path.home() / "IC_RECORDER")),
)).expanduser()
IC_RECORDER_PATTERN = os.getenv("IC_RECORDER_PATTERN", str(IC_CONFIG.get("file_pattern", "*.mp3")))
TEXT_DIR = Path(os.getenv("WHISPER_TEXT_DIR", str(Path.home() / "whisper_ic_recorder")))
PLAN_DIR = Path(os.getenv("DAILY_PLAN_DIR", str(TEXT_DIR / "daily_plan")))
GLOSSARY_FILE = Path(os.getenv("SCHEDULE_GLOSSARY_FILE", str(TEXT_DIR / "schedule_glossary.json")))

WHISPER_ARGS = [
    "whisper", "--language", "Japanese", "--model", "large-v2",
    "--beam_size", "5", "--temperature", "0",
    "--no_speech_threshold", "0.25", "--logprob_threshold", "-1",
    "--compression_ratio_threshold", "2.0",
    "--condition_on_previous_text", "False",
]

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
LOCAL_BASE_URL = os.getenv("LLM_BASE_URL", str(SERVICES_CONFIG.get("local_llm_base_url", "http://127.0.0.1:8080/v1")))
LOCAL_MODEL = os.getenv("LLM_MODEL", "gpt-oss-20b")
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
LOCAL_TEMPERATURE = float(os.getenv("LOCAL_TEMPERATURE", "0.3"))
LOCAL_TOP_P = float(os.getenv("LOCAL_TOP_P", "0.9"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "12000"))

LLAMA_SERVER_CMD = [
    os.getenv("LLAMA_SERVER_BIN", str(Path(PATHS_CONFIG.get("llama_server", Path.home() / "llm/llama.cpp/build/bin/llama-server")).expanduser())),
    "-m", os.getenv("LLM_MODEL_FILE", str(Path(PATHS_CONFIG.get("llama_model", Path.home() / "llm/gpt-oss-20b-Q5_K_M.gguf")).expanduser())),
    "--jinja", "--reasoning-format", "auto", "--ctx-size", str(LLM_CTX_SIZE),
    "--temp", "1.0", "--top-p", "1.0",
    "-ngl", str(LLM_NGL), "--host", "0.0.0.0", "--port", "8080", "--timeout", "3600",
]

DEFAULT_GLOSSARY: dict[str, list[str]] = {
    "Ubuntu": ["オブンツ", "ウブンツ", "オーブンツ"],
    "勤怠管理": ["筋体管理", "身体管理", "勤体管理"],
    "マッチ箱思考機械": [
        "マッチ箱施行", "マッチ箱施工", "マッチ箱思考", "マッチボックス思考機械"
    ],
    "Flet": ["フィレット", "フレット"],
    "Python": ["パイソン"],
    "ラグランジュポイント": ["ラグランチポイント", "ラグランジェポイント"],
    "3Dシミュレーター": ["3 d シュミレーター", "3Dシュミレーター", "3 d シミュレーター"],
    "一次近似": ["一時禁止", "一次禁止"],
    "二次近似": ["二時禁止", "二次禁止"],
    "三次近似": ["三時禁止", "三次禁止"],
    "README": ["リードミー"],
}

LOCAL_EXTRA_PROMPT = """
ローカルLLM向けの追加規則:
- 文字起こし中の [00:31.000 --> 00:48.000] のような角括弧内は、Whisper音声ファイル内の経過時間であり、一日の予定時刻ではありません。時間割の時刻として絶対に使わないでください。
- 「10時から14時まで会議」のように、発話本文で明示された時刻だけを固定予定として扱ってください。
- 入力にない朝食、メール確認、給与計算、契約書作成、提出、レビュー、関係者など、もっともらしい一般的作業を補ってはいけません。
- 「今日やる」「今日中」「優先」など明確に今日の作業とされたものを優先し、「今後やりたい」「そのうち」「将来」「できれば後で」は今日の必須作業と混同しないでください。
- 1つの作業を不必要に細かい30分枠へ分割しないでください。作業量に応じて30分、1時間、1時間30分など連続枠を使って構いません。
- 固定予定は時間割にも必ず同じ開始・終了時刻で入れてください。固定予定と他作業を重複させないでください。
- 時間表の時刻表記は必ず ASCII の HH:MM〜HH:MM を使ってください。特殊ハイフン（‑、–、−）は使わないでください。
- 判断できないものは作り込まず「注意点」に残してください。
"""

LOCAL_REVIEW_PROMPT = """
次の「補正済み文字起こし」と「Markdown時間割の初稿」を照合し、初稿を修正してください。
出力は修正済みMarkdownだけにしてください。JSONや説明文、コードフェンスは不要です。

最重要チェック:
1. 文字起こしにない作業を削除する。
2. Whisperの [00:00.000 --> ...] は音声内経過時間なので予定時刻として使わない。
3. 発話本文で明示された固定時刻・会議時間をそのまま守る。
4. 今日やる作業を落とさず、将来案を勝手に今日の予定へ昇格させない。
5. 時間割の重複をなくす。
6. 時刻表記を HH:MM〜HH:MM に統一する。
7. 入力にない人物、締切、工程、日常作業を創作しない。
8. 08:00〜18:00を無理に全部埋めなくてよい。予備時間で埋める必要もない。
"""

SYSTEM_PROMPT = """Reasoning: low

あなたは、研究開発者の一日の作業計画を作る秘書AIです。
入力は、早朝散歩中にICレコーダーへ話した日本語のWhisper文字起こしです。
内容には誤認識、言い直し、未整理な話が含まれます。

目的:
- 今日やることを抽出する
- 30分単位の柔軟な時間割の素案を作る
- 午前中には集中力が必要な作業、新規に始める作業、研究開発の本丸を置く
- 午後には確認、連絡、惰性でも進められる作業を置く
- 時刻が明確に決まっている会議や予定があれば最優先で固定する
- 厳密な予定表ではなく、気分や進捗で延長・短縮できる「行動支援」として作る

重要な規則:
- 入力で「今日やる」と述べた内容と、将来の計画・検討材料を区別する
- 入力にない人物、締切、依頼、レビュー担当、作業工程を創作しない
- 時刻指定のない休憩や作業を「固定予定」に入れない
- 重要作業を「今日の重点」に書くだけでなく、原則として時間割にも入れる
- 全時間を無理に埋める必要はないが、主要作業が時間割から抜けないようにする
- 不明確な点やAIの推測は「注意点」に明記する

出力形式:
Markdownだけを出力してください。JSONやコードフェンスは付けないでください。
次の見出しを必ず含めてください。

# 30分時間割 素案

## 固定予定
- 明確な時刻がある予定。なければ「なし」。

## 今日の重点
- 今日の本丸を1〜3個。

## 時間割
| 時間 | 作業 | メモ |
| ---- | ---- | ---- |
| 08:00〜08:30 | ... | ... |

時間割は原則として08:00〜18:00を30分刻みまたは1時間刻みで書いてください。
必要なら「予備」「休憩」「昼休み」を入れてください。
1時間枠の場合も、開始時刻と終了時刻を必ず明記してください。

## 軽作業・待ちタスク
- 返事待ち、権限待ち、確認待ち、短時間で済む作業。

## 今日の最小成功ライン
- 全部できなくても、ここまでできればよい、という条件を3〜5個。

## 注意点
- 未確定情報、問い合わせが必要なもの、AIが推測したものを明示してください。
"""


@dataclass
class Correction:
    original: str
    corrected: str
    count: int


def shell_quote(cmd: Iterable[str]) -> str:
    return " ".join(f'"{c}"' if " " in c else c for c in cmd)


def load_glossary(path: Path) -> dict[str, list[str]]:
    glossary = {key: list(values) for key, values in DEFAULT_GLOSSARY.items()}
    if not path.exists():
        return glossary
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"glossary must be a JSON object: {path}")
    for canonical, variants in data.items():
        if isinstance(variants, str):
            variants = [variants]
        if not isinstance(canonical, str) or not isinstance(variants, list):
            raise ValueError(f"invalid glossary entry: {canonical!r}")
        glossary[canonical] = [str(v) for v in variants]
    return glossary


def save_glossary_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_GLOSSARY, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[GLOSSARY] template written: {path}")


def correct_transcript(text: str, glossary: dict[str, list[str]]) -> tuple[str, list[Correction]]:
    variant_to_canonical = {
        variant: canonical
        for canonical, variants in glossary.items()
        for variant in variants
        if variant and variant != canonical
    }
    if not variant_to_canonical:
        return text, []
    variants = sorted(variant_to_canonical, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(v) for v in variants))
    counts: dict[tuple[str, str], int] = {}

    def replace(match: re.Match[str]) -> str:
        original = match.group(0)
        canonical = variant_to_canonical[original]
        key = (original, canonical)
        counts[key] = counts.get(key, 0) + 1
        return canonical

    corrected = pattern.sub(replace, text)
    changes = [Correction(src, dst, count) for (src, dst), count in counts.items()]
    return corrected, changes


def transcribe_new_files() -> list[Path]:
    if not IC_RECORDER_DIR.exists():
        raise FileNotFoundError(f"No IC RECORDER: {IC_RECORDER_DIR}")
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in TEXT_DIR.glob("record*.txt") if not p.name.endswith("_corrected.txt")}
    created: list[Path] = []
    for mp3_path in sorted(IC_RECORDER_DIR.glob(IC_RECORDER_PATTERN)):
        out_stem = f"record_{mp3_path.stem}"
        if out_stem in existing:
            continue
        out_path = TEXT_DIR / f"{out_stem}.txt"
        cmd = WHISPER_ARGS.copy()
        cmd.insert(1, str(mp3_path))
        print(shell_quote(cmd))
        with out_path.open("w", encoding="utf-8") as f:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(line)
                f.write(line)
            rc = proc.wait()
        if rc != 0:
            out_path.unlink(missing_ok=True)
            raise subprocess.CalledProcessError(rc, cmd)
        st = mp3_path.stat()
        os.utime(out_path, (st.st_mtime, st.st_mtime))
        created.append(out_path)
    return created


def read_and_correct(paths: list[Path], glossary: dict[str, list[str]]) -> tuple[str, list[dict[str, Any]]]:
    parts: list[str] = []
    reports: list[dict[str, Any]] = []
    for path in paths:
        original = path.read_text(encoding="utf-8", errors="replace")
        corrected, changes = correct_transcript(original, glossary)
        corrected_path = path.with_name(path.stem + "_corrected.txt")
        corrected_path.write_text(corrected, encoding="utf-8")
        parts.append(f"\n\n===== {path.name} =====\n\n{corrected}")
        reports.append({
            "source_file": path.name,
            "corrected_file": corrected_path.name,
            "corrections": [c.__dict__ for c in changes],
        })
        for c in changes:
            print(f"[CORRECT] {c.original} -> {c.corrected} ({c.count})")
    return "".join(parts).strip(), reports


def glossary_prompt(glossary: dict[str, list[str]]) -> str:
    return "\n".join(f"- {canonical}: {', '.join(variants)}" for canonical, variants in glossary.items())


def infer_plan_date(paths: list[Path]) -> str | None:
    dates: list[date] = []
    for p in paths:
        m = re.search(r"(?:^|_)(\d{2})(\d{2})(\d{2})(?:_|$)", p.stem)
        if m:
            try:
                dates.append(date(2000 + int(m[1]), int(m[2]), int(m[3])))
            except ValueError:
                pass
    return max(dates).isoformat() if dates else None


def make_user_prompt(transcript: str, glossary: dict[str, list[str]], plan_date: str | None, local_mode: bool = False) -> str:
    local_rules = LOCAL_EXTRA_PROMPT if local_mode else ""
    return f"""対象日: {plan_date or '不明'}

正規表記用語集:
{glossary_prompt(glossary)}

以下の補正済み文字起こしだけを根拠として、一日の時間割をMarkdownで作成してください。
今日の作業と、将来計画の材料として話している項目を区別してください。

{local_rules}

--- 補正済み文字起こし ---
{transcript}
"""


def strip_markdown_wrappers(raw: str) -> str:
    text = (raw or "").replace("\r", "").strip()
    text = re.sub(r"<\|.*?\|>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:markdown|md)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    # gpt-ossの推論が本文先頭へ混ざった場合は、最初の見出しから採用する。
    heading = re.search(r"(?m)^#\s+30分時間割", text)
    if heading:
        text = text[heading.start():]
    return text.strip() + "\n"


def call_openai(prompt: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.responses.create(
        model=OPENAI_MODEL,
        instructions=SYSTEM_PROMPT,
        input=prompt,
        temperature=OPENAI_TEMPERATURE,
        max_output_tokens=LLM_MAX_TOKENS,
    )
    return resp.output_text.strip()


def call_local_llm(prompt: str) -> str:
    from openai import OpenAI
    client = OpenAI(base_url=LOCAL_BASE_URL, api_key=os.getenv("LLM_API_KEY", "local-llm"))
    resp = client.chat.completions.create(
        model=LOCAL_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=LOCAL_TEMPERATURE,
        top_p=LOCAL_TOP_P,
        max_tokens=LLM_MAX_TOKENS,
        extra_body={"chat_template_kwargs": {"reasoning_effort": "low"}},
    )
    choice = resp.choices[0]
    content = choice.message.content
    if isinstance(content, str) and content.strip():
        return content
    data = choice.message.model_dump() if hasattr(choice.message, "model_dump") else {}
    for key in ("reasoning_content", "reasoning", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise RuntimeError("local LLM returned an empty response")


def review_local_markdown(transcript: str, draft_markdown: str, plan_date: str | None) -> str:
    """ローカルLLMの初稿を、元文字起こしと再照合してMarkdownのまま修正する。"""
    prompt = f"""対象日: {plan_date or '不明'}

{LOCAL_REVIEW_PROMPT}

--- 補正済み文字起こし ---
{transcript}

--- Markdown初稿 ---
{draft_markdown}
"""
    return strip_markdown_wrappers(call_local_llm(prompt))


def normalize_heading(text: str) -> str:
    return re.sub(r"[\s　・:：]+", "", text).strip()


def split_sections(markdown: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in markdown.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = normalize_heading(match.group(1))
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line)
    return sections


def clean_inline_markdown(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"`(.*?)`", r"\1", text)
    return text.strip()


def parse_bullets(lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        match = re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+?)\s*$", line)
        if not match:
            continue
        item = clean_inline_markdown(match.group(1))
        if item and item not in {"なし", "無し"}:
            items.append(item)
    return items


def split_title_reason(item: str) -> tuple[str, str]:
    for sep in (" — ", " – ", " - ", "：", ":"):
        if sep in item:
            title, reason = item.split(sep, 1)
            return title.strip(), reason.strip()
    return item.strip(), ""


def parse_time_range(value: str) -> tuple[str | None, str | None]:
    text = value.strip()
    for ch in ("～", "~", "－", "–", "—", "‑", "‒", "−", "﹘", "﹣", "－"):
        text = text.replace(ch, "〜" if ch in ("～", "~") else "-")
    text = text.replace("：", ":")
    text = re.sub(r"\s+", "", text)
    full = re.search(r"(?P<sh>\d{1,2}):(?P<sm>\d{2})\s*[〜-]\s*(?P<eh>\d{1,2}):(?P<em>\d{2})", text)
    if full:
        return f"{int(full['sh']):02d}:{int(full['sm']):02d}", f"{int(full['eh']):02d}:{int(full['em']):02d}"
    single = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if single:
        start = int(single[1]) * 60 + int(single[2])
        end = start + 30
        return f"{start // 60:02d}:{start % 60:02d}", f"{end // 60:02d}:{end % 60:02d}"
    return None, None


def parse_markdown_table(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.count("|") < 3:
            continue
        cells = [clean_inline_markdown(c) for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells):
            continue
        if cells[0] in {"時間", "時刻"} and cells[1] in {"作業", "予定", "内容"}:
            continue
        rows.append(cells)
    return rows


def infer_category(task: str, note: str = "") -> str:
    text = task + " " + note
    if re.search(r"休憩|昼食|昼休み|朝食", text):
        return "break"
    if re.search(r"予備|バッファ", text):
        return "buffer"
    if re.search(r"メール|DM|連絡|電話|返信", text, re.IGNORECASE):
        return "communication"
    if re.search(r"会議|打合せ|ミーティング", text):
        return "meeting"
    if re.search(r"確認|整理|予約|購入|申請|提出", text):
        return "admin"
    return "focus"


def parse_fixed_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in parse_bullets(lines):
        match = re.match(r"^(.+?\d{1,2}:\d{2}(?:\s*[〜~\-]\s*\d{1,2}:\d{2})?)\s+(.+)$", item)
        if not match:
            continue
        start, end = parse_time_range(match.group(1))
        if not start or not end:
            continue
        title, note = split_title_reason(match.group(2))
        events.append({"start": start, "end": end, "title": title, "note": note})
    return events


def markdown_to_plan(markdown: str, plan_date: str | None) -> tuple[dict[str, Any], list[str]]:
    sections = split_sections(markdown)
    warnings: list[str] = []

    priority_items = parse_bullets(sections.get("今日の重点", []))
    priorities = [
        {"title": title, "reason": reason}
        for title, reason in (split_title_reason(item) for item in priority_items)
    ]

    slots: list[dict[str, Any]] = []
    for cells in parse_markdown_table(sections.get("時間割", [])):
        start, end = parse_time_range(cells[0])
        if not start or not end:
            warnings.append(f"時間を解析できない行を除外: {' | '.join(cells)}")
            continue
        task = cells[1].strip() if len(cells) >= 2 else ""
        note = cells[2].strip() if len(cells) >= 3 else ""
        if not task:
            warnings.append(f"作業名が空の行を除外: {' | '.join(cells)}")
            continue
        slots.append({
            "start": start,
            "end": end,
            "task": task,
            "note": note,
            "category": infer_category(task, note),
            "source": "markdown",
        })

    slots.sort(key=lambda s: s["start"])
    for prev, current in zip(slots, slots[1:]):
        if current["start"] < prev["end"]:
            warnings.append(
                f"時間枠の重複: {prev['start']}〜{prev['end']} と {current['start']}〜{current['end']}"
            )

    light_tasks = parse_bullets(sections.get("軽作業待ちタスク", []))
    minimum_success = parse_bullets(sections.get("今日の最小成功ライン", []))
    uncertainty_items = parse_bullets(sections.get("注意点", []))
    uncertainties = [
        {"text": title, "reason": reason}
        for title, reason in (split_title_reason(item) for item in uncertainty_items)
    ]

    plan = {
        "schema_version": "1.0-v7-prototype",
        "plan_date": plan_date,
        "title": "30分時間割 素案",
        "fixed_events": parse_fixed_events(sections.get("固定予定", [])),
        "priorities": priorities,
        "slots": slots,
        "light_tasks": light_tasks,
        "minimum_success": minimum_success,
        "uncertainties": uncertainties,
        "parse_warnings": warnings,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    return plan, warnings


def output_stem(paths: list[Path]) -> str:
    if len(paths) == 1:
        return paths[0].stem + "_plan"
    return "daily_plan_" + time.strftime("%Y%m%d_%H%M%S")


def write_outputs(
    paths: list[Path],
    markdown: str,
    plan: dict[str, Any],
    correction_report: list[dict[str, Any]],
    formats: set[str],
) -> list[Path]:
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    stem = output_stem(paths)
    plan["source"] = {
        "transcripts": [p.name for p in paths],
        "correction_report": correction_report,
        "markdown_file": f"{stem}.md",
        "generator": Path(__file__).name,
        "status": "prototype",
    }
    written: list[Path] = []
    # Markdownは正本なので、--format jsonでも保存する。
    md_path = PLAN_DIR / f"{stem}.md"
    md_path.write_text(markdown, encoding="utf-8")
    written.append(md_path)
    if "json" in formats:
        json_path = PLAN_DIR / f"{stem}.json"
        json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(json_path)
    for path in written:
        print(f"[PLAN] written: {path}")
    return written


def wait_local_server(base_url: str, timeout_sec: int = 120) -> bool:
    url = base_url.rstrip("/") + "/models"
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            with urllib.request.urlopen(url, timeout=5) as res:
                if 200 <= res.status < 300:
                    return True
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2)
    return False


def start_llama_server(extra_wait_sec: int = 5) -> subprocess.Popen:
    print("[LLM] starting llama-server:")
    print(shell_quote(LLAMA_SERVER_CMD))
    proc = subprocess.Popen(
        LLAMA_SERVER_CMD,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )
    if not wait_local_server(LOCAL_BASE_URL, timeout_sec=180):
        stop_llama_server(proc)
        raise RuntimeError("llama-server did not become ready")
    if extra_wait_sec > 0:
        time.sleep(extra_wait_sec)
    return proc


def stop_llama_server(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ICレコーダーからMarkdown時間割を生成し、PythonでJSONへ変換する試作版V7"
    )
    parser.add_argument("--llm", choices=["none", "openai", "local"], default="local")
    parser.add_argument("--all-text", action="store_true")
    parser.add_argument("--input-text", type=Path, nargs="+", help="Whisperを実行せず、指定txtを処理")
    parser.add_argument(
        "--format", choices=["md", "both"], default="both",
        help="md: Markdownのみ、both: MarkdownとJSON。Markdownは常に正本として保存",
    )
    parser.add_argument("--glossary", type=Path, default=GLOSSARY_FILE)
    parser.add_argument("--write-glossary-template", action="store_true")
    parser.add_argument("--no-start-server", action="store_true")
    parser.add_argument("--server-extra-wait", type=int, default=5)
    parser.add_argument(
        "--no-local-review", action="store_true",
        help="ローカルLLMのMarkdown初稿に対する2段階目の自己レビューを省略する",
    )
    parser.add_argument(
        "--parse-markdown", type=Path,
        help="LLMを呼ばず、既存MarkdownをJSONへ変換するテスト用",
    )
    args = parser.parse_args()

    try:
        if args.write_glossary_template:
            save_glossary_template(args.glossary)

        if args.parse_markdown:
            md_path = args.parse_markdown.expanduser().resolve()
            markdown = md_path.read_text(encoding="utf-8")
            plan, warnings = markdown_to_plan(markdown, None)
            out_path = md_path.with_suffix(".parsed.json")
            out_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[PLAN] written: {out_path}")
            for warning in warnings:
                print(f"[PARSE WARNING] {warning}")
            return 0

        glossary = load_glossary(args.glossary)
        if args.input_text:
            target_txts = [p.expanduser().resolve() for p in args.input_text]
        else:
            new_txts = transcribe_new_files()
            target_txts = sorted(TEXT_DIR.glob("record*.txt")) if args.all_text else new_txts
            target_txts = [p for p in target_txts if not p.name.endswith("_corrected.txt")]
        if not target_txts:
            print("[INFO] transcript files not found. plan generation skipped.")
            return 0

        transcript, correction_report = read_and_correct(target_txts, glossary)
        if args.llm == "none":
            print("[INFO] correction completed; --llm none, plan generation skipped.")
            return 0

        plan_date = infer_plan_date(target_txts)
        prompt = make_user_prompt(transcript, glossary, plan_date, local_mode=(args.llm == "local"))
        llama_proc: subprocess.Popen | None = None
        try:
            if args.llm == "openai":
                raw = call_openai(prompt)
            else:
                if args.no_start_server:
                    if not wait_local_server(LOCAL_BASE_URL, 30):
                        raise RuntimeError("local LLM server is not responding")
                else:
                    llama_proc = start_llama_server(args.server_extra_wait)
                raw = call_local_llm(prompt)

            markdown = strip_markdown_wrappers(raw)
            if args.llm == "local" and not args.no_local_review:
                print("[LLM] reviewing local Markdown draft against transcript...")
                markdown = review_local_markdown(transcript, markdown, plan_date)
            if "## 時間割" not in markdown:
                raise ValueError("LLM output does not contain the required '## 時間割' heading")

            plan, warnings = markdown_to_plan(markdown, plan_date)
            for warning in warnings:
                print(f"[PARSE WARNING] {warning}")
            formats = {"md", "json"} if args.format == "both" else {"md"}
            write_outputs(target_txts, markdown, plan, correction_report, formats)
            return 0
        finally:
            if args.llm == "local" and not args.no_start_server:
                stop_llama_server(llama_proc)
    except Exception as ex:
        print(f"Error: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
