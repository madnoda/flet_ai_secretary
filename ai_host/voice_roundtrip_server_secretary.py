#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Atsushi Noda
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import queue
import re
import socket
import struct
import threading
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
import resampy
import soundfile as sf
from faster_whisper import WhisperModel
from openai import OpenAI


HOST = "0.0.0.0"
PORT = 50000

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2

SAVE_DIR = Path("received_audio")
SAVE_DIR.mkdir(parents=True, exist_ok=True)

whisper = WhisperModel("small", device="cuda", compute_type="float16")

conversation_history = deque(maxlen=8)
history_lock = threading.Lock()

VOICEVOX_HOST = os.environ.get("VOICEVOX_HOST", "http://127.0.0.1:50021")
VOICEVOX_SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "30"))
VOICEVOX_SPEED = float(os.environ.get("VOICEVOX_SPEED", "1.0"))
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")
LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "gpt-oss-20b-Q5_K_M.gguf")
LLM_BACKEND = "local"
SCHEDULE_URL = os.environ.get("SCHEDULE_URL", "http://127.0.0.1:8766/today")
SCHEDULE_TIMEOUT = float(os.environ.get("SCHEDULE_TIMEOUT", "5.0"))
SCHEDULE_BASE_URL = SCHEDULE_URL.rsplit("/", 1)[0]
TEXT_REQUEST_MARKER = 0xFFFFFFFF
MAX_TEXT_REQUEST_BYTES = 64 * 1024


def recv_exact(conn: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(min(65536, size - len(data)))
        if not chunk:
            raise ConnectionError(
                f"受信途中で接続が切れました: {len(data)}/{size} bytes"
            )
        data.extend(chunk)
    return bytes(data)


def send_bytes(conn: socket.socket, data: bytes) -> None:
    conn.sendall(struct.pack("<I", len(data)))
    if data:
        conn.sendall(data)


def send_text(conn: socket.socket, text: str) -> None:
    send_bytes(conn, (text or "").encode("utf-8"))


def send_pcm_chunk(conn: socket.socket, pcm_data: bytes) -> None:
    send_bytes(conn, pcm_data)


def save_pcm_as_wav(pcm_data: bytes) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    wav_path = SAVE_DIR / f"received_{stamp}.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm_data)
    return wav_path


def transcribe_pcm16(pcm16_bytes: bytes) -> str:
    if not pcm16_bytes:
        return ""
    pcm = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = whisper.transcribe(pcm, language="ja", beam_size=1)
    return "".join(segment.text for segment in segments).strip()


def fetch_today_schedule() -> dict | None:
    """時間割HTTPサーバーから今日の最新時間割を取得する。"""
    try:
        response = requests.get(SCHEDULE_URL, timeout=SCHEDULE_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        plan = data.get("plan") if isinstance(data, dict) else None
        return plan if isinstance(plan, dict) else None
    except Exception as exc:
        print(f"⚠️ 時間割取得失敗: {type(exc).__name__}: {exc}")
        return None


def schedule_context_text() -> str:
    """LLMへ渡す、現在時刻と今日の時間割の読み取り専用コンテキスト。"""
    now = datetime.now().astimezone()
    plan = fetch_today_schedule()
    lines = [f"現在日時: {now.isoformat(timespec='minutes')}"]
    if not plan:
        lines.append("今日の時間割: 取得できませんでした。")
        return "\n".join(lines)

    lines.append(
        f"今日の時間割: {plan.get('plan_date', '')} / {plan.get('title', '')}"
    )
    items = plan.get("items") or []
    if not items:
        lines.append("予定項目はありません。")
    for item in items:
        if not isinstance(item, dict):
            continue
        checked = "完了" if item.get("checked") else "未完了"
        note = str(item.get("note") or "").strip()
        line = (
            f"- {item.get('start', '')}〜{item.get('end', '')} "
            f"{item.get('task', '')} [{checked}]"
        )
        if note:
            line += f" / {note}"
        lines.append(line)
    return "\n".join(lines)




def _normalize_text(text: str) -> str:
    return re.sub(r"[\s　『』「」\"'。、，,.!?！？・･()（）\[\]【】]", "", text or "")


def _extract_reminder_target_text(text: str) -> str:
    """通知依頼文から、予定名らしい部分だけをなるべく取り出す。"""
    target = text or ""
    # 時刻指定は予定名の類似判定には使わない。
    target = re.sub(r"(?:(?:午前|午後)\s*)?\d{1,2}時(?:\d{1,2}分)?(?:から)?", " ", target)
    target = re.sub(r"\d{1,3}分前", " ", target)
    # 依頼表現を除去。長いものから先に消す。
    for phrase in (
        "の作業の前に教えてください", "の作業の前に教えて",
        "の前に教えてください", "の前に教えて",
        "の前に知らせてください", "の前に知らせて",
        "の前に通知してください", "の前に通知して",
        "の前に声をかけてください", "の前に声をかけて",
        "の前に声かけて", "の前に声掛けて",
        "作業の前に教えてください", "作業の前に教えて",
        "前に教えてください", "前に教えて",
        "前に知らせてください", "前に知らせて",
        "前に通知してください", "前に通知して",
        "前に声かけて", "前に声掛けて",
    ):
        target = target.replace(phrase, " ")
    return _normalize_text(target)


def _task_similarity(user_target: str, task: str) -> float:
    a = _normalize_text(user_target)
    b = _normalize_text(task)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        shorter = min(len(a), len(b))
        longer = max(len(a), len(b))
        return 0.90 + 0.10 * (shorter / longer)
    return difflib.SequenceMatcher(None, a, b).ratio()


def _looks_like_completion_request(text: str) -> bool:
    normalized = _normalize_text(text)
    completion_words = ("完了", "終了", "終わった", "終りました", "終わりました")
    return any(word in normalized for word in completion_words)


def _extract_completion_target_text(text: str) -> str:
    target = text or ""
    for phrase in (
        "現在の作業", "現在作業", "今の作業", "いまの作業",
        "の作業を完了", "の作業完了", "の作業を終了", "の作業終了",
        "作業を完了", "作業完了", "作業を終了", "作業終了",
        "を完了", "完了", "を終了", "終了", "終わりました", "終わった",
    ):
        target = target.replace(phrase, " ")
    return _normalize_text(target)


def _parse_hhmm_minutes(value: str) -> int | None:
    try:
        hour_s, minute_s = value.split(":", 1)
        hour, minute = int(hour_s), int(minute_s)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    except Exception:
        pass
    return None


def _resolve_current_schedule_item(plan: dict) -> dict | None:
    items = [item for item in (plan.get("items") or []) if isinstance(item, dict)]
    if not items:
        return None
    now = datetime.now().astimezone()
    current_minutes = now.hour * 60 + now.minute

    # まず予定時刻の範囲内にいる項目を選ぶ。
    for item in items:
        start = _parse_hhmm_minutes(str(item.get("start") or ""))
        end = _parse_hhmm_minutes(str(item.get("end") or ""))
        if start is None or end is None:
            continue
        if end <= start:
            end += 24 * 60
        probe = current_minutes if current_minutes >= start else current_minutes + 24 * 60
        if start <= probe < end:
            return item

    # 時間割に隙間がある場合は、現在時刻以前で最も近い未完了項目を採用。
    candidates = []
    for item in items:
        if item.get("checked"):
            continue
        start = _parse_hhmm_minutes(str(item.get("start") or ""))
        if start is not None and start <= current_minutes:
            candidates.append((start, item))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    return None


def resolve_schedule_item_for_completion(user_text: str, plan: dict) -> tuple[dict | None, str | None]:
    items = [item for item in (plan.get("items") or []) if isinstance(item, dict)]
    normalized = _normalize_text(user_text)

    if any(key in normalized for key in ("現在の作業", "現在作業", "今の作業", "いまの作業")):
        item = _resolve_current_schedule_item(plan)
        if item is None:
            return None, "現在の作業を時間割から特定できませんでした。"
        return item, None

    task_matches = []
    for item in items:
        task = str(item.get("task") or "").strip()
        if task and _normalize_text(task) in normalized:
            task_matches.append(item)
    if len(task_matches) == 1:
        return task_matches[0], None
    if len(task_matches) > 1:
        return None, "同じ名前に近い作業が複数あります。開始時刻も指定してください。"

    target = _extract_completion_target_text(user_text)
    if target:
        scored = []
        for item in items:
            task = str(item.get("task") or "").strip()
            if task:
                scored.append((_task_similarity(target, task), item))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            best_score, best_item = scored[0]
            second_score = scored[1][0] if len(scored) > 1 else 0.0
            if best_score >= 0.68 and (best_score - second_score >= 0.08 or best_score >= 0.86):
                print(
                    f"✅ 完了予定を類似一致: '{target}' -> "
                    f"'{best_item.get('task', '')}' (score={best_score:.2f})"
                )
                return best_item, None

    hm = _parse_hour_minute(user_text)
    if hm is not None:
        hhmm = f"{hm[0]:02d}:{hm[1]:02d}"
        matches = [item for item in items if str(item.get("start") or "") == hhmm]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, f"{hm[0]}時{hm[1]:02d}分開始の作業が複数あります。作業名も指定してください。"

    return None, "どの作業を完了にするか特定できませんでした。作業名か『現在の作業完了』と話してください。"


def complete_schedule_item(user_text: str) -> str | None:
    """作業完了の音声指示ならDBを更新して確認文を返す。対象外ならNone。"""
    if not _looks_like_completion_request(user_text):
        return None

    plan = fetch_today_schedule()
    if not plan:
        return "今日の時間割を取得できなかったので、作業完了を記録できませんでした。"

    item, error = resolve_schedule_item_for_completion(user_text, plan)
    if item is None:
        return error or "完了する作業を特定できませんでした。"

    payload = {
        "plan_id": plan.get("plan_id", ""),
        "item_id": item.get("item_id", ""),
        "completed": True,
    }
    try:
        response = requests.post(
            f"{SCHEDULE_BASE_URL}/item/complete",
            json=payload,
            timeout=SCHEDULE_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        saved = data.get("item") or {}
    except Exception as exc:
        print(f"⚠️ 作業完了更新失敗: {type(exc).__name__}: {exc}")
        return "作業完了の記録に失敗しました。時間割サーバーの状態を確認してください。"

    task = str(saved.get("task") or item.get("task") or "作業")
    completed_at = str(saved.get("completed_at") or "")
    try:
        dt = datetime.fromisoformat(completed_at)
        time_text = f"{dt.hour}時{dt.minute:02d}分"
    except Exception:
        time_text = "現在時刻"
    return f"はい。{task}を完了にしました。完了時刻は{time_text}です。"


def _parse_hour_minute(text: str) -> tuple[int, int] | None:
    """「15時」「午後3時」「15時30分」「9時半」程度の時刻表現を拾う。"""
    m = re.search(r"(?:(午前|午後)\s*)?(\d{1,2})時(?!間)(?:(\d{1,2})分|(半))?", text or "")
    if not m:
        return None
    ampm, hour_s, minute_s, half = m.groups()
    hour = int(hour_s)
    minute = 30 if half else int(minute_s or 0)
    if minute > 59:
        return None
    if ampm == "午後" and 1 <= hour <= 11:
        hour += 12
    elif ampm == "午前" and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return hour, minute


def _find_hour_minute_expressions(text: str) -> list[tuple[int, int, str]]:
    """文中の時刻表現を出現順で返す。"""
    results: list[tuple[int, int, str]] = []
    pattern = re.compile(r"(?:(午前|午後)\s*)?(\d{1,2})時(?!間)(?:(\d{1,2})分|(半))?")
    for m in pattern.finditer(text or ""):
        ampm, hour_s, minute_s, half = m.groups()
        hour = int(hour_s)
        minute = 30 if half else int(minute_s or 0)
        if minute > 59:
            continue
        if ampm == "午後" and 1 <= hour <= 11:
            hour += 12
        elif ampm == "午前" and hour == 12:
            hour = 0
        if 0 <= hour <= 23:
            results.append((hour, minute, m.group(0)))
    return results


def _looks_like_time_change_request(text: str) -> bool:
    normalized = _normalize_text(text)
    if not any(word in normalized for word in ("変更", "変えて", "ずらして", "までにして", "からにして", "短く", "延ばして", "伸ばして", "時間にして")):
        return False
    return bool(_find_hour_minute_expressions(text)) or bool(
        re.search(r"\d+\s*時間(?:\s*\d+\s*分)?|\d+\s*分", text or "")
    )


def _extract_time_change_target_text(text: str) -> str:
    target = text or ""
    target = re.sub(r"(?:(?:午前|午後)\s*)?\d{1,2}時(?!間)(?:(?:\d{1,2})分|半)?", " ", target)
    target = re.sub(r"\d+\s*時間(?:\s*\d+\s*分)?|\d+\s*分", " ", target)
    for phrase in (
        "開始時刻", "終了時刻", "開始時間", "終了時間", "予定時刻", "作業時間", "所要時間",
        "から", "まで", "に変更して", "に変更", "変更して", "変えて", "ずらして",
        "までにして", "からにして", "にして", "短くして", "短く", "延ばして", "伸ばして",
        "の作業", "作業",
    ):
        target = target.replace(phrase, " ")
    return _normalize_text(target)


def resolve_schedule_item_for_time_change(user_text: str, plan: dict) -> tuple[dict | None, str | None]:
    items = [item for item in (plan.get("items") or []) if isinstance(item, dict)]
    normalized = _normalize_text(user_text)
    matches = []
    for item in items:
        task = str(item.get("task") or "").strip()
        if task and _normalize_text(task) in normalized:
            matches.append(item)
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "同じ名前の予定が複数あります。開始時刻も指定してください。"

    target = _extract_time_change_target_text(user_text)
    if target:
        scored = []
        for item in items:
            task = str(item.get("task") or "").strip()
            if task:
                scored.append((_task_similarity(target, task), item))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            best_score, best_item = scored[0]
            second_score = scored[1][0] if len(scored) > 1 else 0.0
            if best_score >= 0.68 and (best_score - second_score >= 0.08 or best_score >= 0.86):
                print(f"🕒 時刻変更予定を類似一致: '{target}' -> '{best_item.get('task', '')}' (score={best_score:.2f})")
                return best_item, None
    return None, "どの予定の時間を変更するか特定できませんでした。作業名を指定してください。"


def _format_japanese_time(hhmm: str) -> str:
    try:
        h, m = (int(x) for x in hhmm.split(":", 1))
        return f"{h}時" if m == 0 else f"{h}時{m:02d}分"
    except Exception:
        return hhmm


def change_schedule_item_time(user_text: str) -> str | None:
    """音声による予定の開始・終了時刻または所要時間変更を処理する。"""
    if not _looks_like_time_change_request(user_text):
        return None
    plan = fetch_today_schedule()
    if not plan:
        return "今日の時間割を取得できなかったので、予定時刻を変更できませんでした。"
    item, error = resolve_schedule_item_for_time_change(user_text, plan)
    if item is None:
        return error or "変更する予定を特定できませんでした。"

    old_start = str(item.get("start") or "")
    old_end = str(item.get("end") or "")
    new_start, new_end = old_start, old_end
    expressions = _find_hour_minute_expressions(user_text)
    normalized = _normalize_text(user_text)

    # 「9時30分から10時まで」のような明示範囲。
    if len(expressions) >= 2 and "から" in user_text and "まで" in user_text:
        h1, m1, _ = expressions[0]
        h2, m2, _ = expressions[1]
        new_start, new_end = f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"
    elif expressions:
        h, m, _ = expressions[0]
        hhmm = f"{h:02d}:{m:02d}"
        if any(key in normalized for key in ("終了時刻", "終了時間", "まで")):
            new_end = hhmm
        elif any(key in normalized for key in ("開始時刻", "開始時間", "から")):
            new_start = hhmm
        else:
            # 「○○を9時半に変更」は開始時刻変更として扱い、所要時間を維持。
            old_start_m = _parse_hhmm_minutes(old_start)
            old_end_m = _parse_hhmm_minutes(old_end)
            if old_start_m is None or old_end_m is None:
                return "現在の予定時刻を解釈できなかったので変更できませんでした。"
            if old_end_m <= old_start_m:
                old_end_m += 24 * 60
            duration = old_end_m - old_start_m
            new_start = hhmm
            new_end_m = h * 60 + m + duration
            new_end = f"{(new_end_m // 60) % 24:02d}:{new_end_m % 60:02d}"
    else:
        # 「1時間にして」「45分に短くして」は開始時刻を維持して終了だけ変更。
        duration_m = None
        m = re.search(r"(\d+)\s*時間(?:\s*(\d+)\s*分)?", user_text or "")
        if m:
            duration_m = int(m.group(1)) * 60 + int(m.group(2) or 0)
        else:
            m = re.search(r"(\d+)\s*分", user_text or "")
            if m:
                duration_m = int(m.group(1))
        if not duration_m or duration_m <= 0:
            return "変更後の時刻または作業時間を聞き取れませんでした。"
        start_m = _parse_hhmm_minutes(old_start)
        if start_m is None:
            return "現在の開始時刻を解釈できなかったので変更できませんでした。"
        end_m = start_m + duration_m
        new_end = f"{(end_m // 60) % 24:02d}:{end_m % 60:02d}"

    payload = {
        "plan_id": plan.get("plan_id", ""),
        "item_id": item.get("item_id", ""),
        "start": new_start,
        "end": new_end,
        "task": item.get("task", ""),
        "note": item.get("note", ""),
        "category": item.get("category", ""),
    }
    try:
        response = requests.post(f"{SCHEDULE_BASE_URL}/item/update", json=payload, timeout=SCHEDULE_TIMEOUT)
        response.raise_for_status()
        saved = (response.json().get("item") or {})
    except Exception as exc:
        print(f"⚠️ 予定時刻更新失敗: {type(exc).__name__}: {exc}")
        return "予定時刻の変更に失敗しました。時間割サーバーの状態を確認してください。"

    task = str(saved.get("task") or item.get("task") or "予定")
    start_text = _format_japanese_time(str(saved.get("start") or new_start))
    end_text = _format_japanese_time(str(saved.get("end") or new_end))
    return f"はい。{task}を{start_text}から{end_text}までに変更しました。"



def _looks_like_add_schedule_request(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(word in normalized for word in (
        "追加して", "追加", "予定を入れて", "予定入れて", "予定を作って", "予定作って",
        "新しい予定", "新規予定",
    ))


def _extract_add_schedule_task(text: str) -> str:
    """追加命令から作業名らしい部分を取り出す。"""
    target = text or ""
    target = re.sub(r"(?:(?:午前|午後)\s*)?\d{1,2}時(?!間)(?:(?:\d{1,2})分|半)?", " ", target)
    target = re.sub(r"\d+\s*時間(?:\s*\d+\s*分)?|\d+\s*分(?:間)?", " ", target)
    for phrase in (
        "新しい予定として", "新規予定として", "予定として", "新しい予定", "新規予定",
        "予定を追加して", "予定追加して", "予定を追加", "予定追加",
        "予定を入れて", "予定入れて", "予定を作って", "予定作って",
        "を追加して", "追加して", "を追加", "追加",
        "から", "まで", "の予定", "予定", "もう一つ", "もうひとつ", "新たに",
    ):
        target = target.replace(phrase, " ")
    target = re.sub(r"[、。,.!！?？]", " ", target)
    target = re.sub(r"\s+", " ", target).strip()
    target = re.sub(r"\s*を$", "", target).strip()
    return target


def add_schedule_item_by_voice(user_text: str) -> str | None:
    """音声命令から新しい予定項目を追加する。開始時刻の指定を必須とする。"""
    if not _looks_like_add_schedule_request(user_text):
        return None

    plan = fetch_today_schedule()
    if not plan:
        return "今日の時間割を取得できなかったので、予定を追加できませんでした。"

    expressions = _find_hour_minute_expressions(user_text)
    if not expressions:
        return "予定を追加する開始時刻も指定してください。例えば、10時半から30分、コイル巻き作業を追加して、と話してください。"

    start_h, start_m, _ = expressions[0]
    start = f"{start_h:02d}:{start_m:02d}"
    end = ""

    # 「10時半から11時まで」のように2時刻があれば範囲指定。
    if len(expressions) >= 2:
        end_h, end_m, _ = expressions[1]
        end = f"{end_h:02d}:{end_m:02d}"
    else:
        # 1時刻だけなら「30分」「1時間」などの所要時間を探す。
        duration_m = None
        m = re.search(r"(\d+)\s*時間(?:\s*(\d+)\s*分)?", user_text or "")
        if m:
            duration_m = int(m.group(1)) * 60 + int(m.group(2) or 0)
        else:
            # 時刻表現に含まれる「30分」を誤採用しないよう、時刻表現を消してから探す。
            without_times = re.sub(
                r"(?:(?:午前|午後)\s*)?\d{1,2}時(?!間)(?:(?:\d{1,2})分|半)?",
                " ", user_text or "",
            )
            m = re.search(r"(\d+)\s*分(?:間)?", without_times)
            if m:
                duration_m = int(m.group(1))
        if not duration_m or duration_m <= 0:
            return "追加する予定の終了時刻か所要時間も指定してください。"
        end_total = start_h * 60 + start_m + duration_m
        end = f"{(end_total // 60) % 24:02d}:{end_total % 60:02d}"

    if start == end:
        return "開始時刻と終了時刻が同じなので、予定を追加できませんでした。"

    task = _extract_add_schedule_task(user_text)
    if not task:
        return "追加する作業名を聞き取れませんでした。作業名も指定してください。"

    payload = {
        "plan_id": plan.get("plan_id", ""),
        "start": start,
        "end": end,
        "task": task,
        "note": "",
        "category": "focus",
        "source": "voice",
    }
    try:
        response = requests.post(f"{SCHEDULE_BASE_URL}/item/add", json=payload, timeout=SCHEDULE_TIMEOUT)
        response.raise_for_status()
        saved = response.json().get("item") or {}
    except Exception as exc:
        print(f"⚠️ 予定追加失敗: {type(exc).__name__}: {exc}")
        return "予定の追加に失敗しました。時間割サーバーの状態を確認してください。"

    task = str(saved.get("task") or task)
    start_text = _format_japanese_time(str(saved.get("start") or start))
    end_text = _format_japanese_time(str(saved.get("end") or end))
    return f"はい。{task}を{start_text}から{end_text}までの予定として追加しました。"

def _parse_lead_minutes(text: str) -> int:
    """明示がなければ5分前。『10分前に』等も扱う。"""
    m = re.search(r"(\d{1,3})分前", text or "")
    if not m:
        return 5
    return max(0, min(1440, int(m.group(1))))


def _looks_like_reminder_request(text: str) -> bool:
    normalized = _normalize_text(text)
    if "前" not in normalized:
        return False
    return any(word in normalized for word in ("教えて", "知らせて", "おしえて", "通知して", "声かけて", "声掛けて"))


def resolve_schedule_item_for_reminder(user_text: str, plan: dict) -> tuple[dict | None, str | None]:
    """音声文から今日の予定項目を特定する。完全一致→類似一致→開始時刻の順で探す。"""
    items = [item for item in (plan.get("items") or []) if isinstance(item, dict)]
    normalized_user = _normalize_text(user_text)

    # タスク名が発話中に含まれていれば最優先。例: 「AI秘書まとめの前に教えて」
    task_matches = []
    for item in items:
        task = str(item.get("task") or "").strip()
        if task and _normalize_text(task) in normalized_user:
            task_matches.append(item)
    if len(task_matches) == 1:
        return task_matches[0], None
    if len(task_matches) > 1:
        return None, "同じ名前に近い予定が複数あります。開始時刻も指定してください。"

    # Whisperの小さな誤認識や送り仮名の違いを許容する。
    # 例: 「コイル巻作業」→「コイル巻き作業」、
    #     「検報告書作成」→「実験報告書作成」。
    target = _extract_reminder_target_text(user_text)
    if target:
        scored = []
        for item in items:
            task = str(item.get("task") or "").strip()
            if not task:
                continue
            score = _task_similarity(target, task)
            scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            best_score, best_item = scored[0]
            second_score = scored[1][0] if len(scored) > 1 else 0.0
            # 0.68以上かつ次点との差が十分なら採用。
            # 短い日本語の予定名でも1文字程度の誤りを拾える水準。
            if best_score >= 0.68 and (best_score - second_score >= 0.08 or best_score >= 0.86):
                print(
                    f"🔎 通知予定を類似一致: '{target}' -> "
                    f"'{best_item.get('task', '')}' (score={best_score:.2f})"
                )
                return best_item, None

    # 「15時の作業の前に教えて」など。
    hm = _parse_hour_minute(user_text)
    if hm is not None:
        hhmm = f"{hm[0]:02d}:{hm[1]:02d}"
        matches = [item for item in items if str(item.get("start") or "") == hhmm]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, f"{hm[0]}時{hm[1]:02d}分開始の予定が複数あります。予定名も指定してください。"
        return None, f"今日の時間割には{hm[0]}時{hm[1]:02d}分開始の予定がありません。"

    return None, "どの予定を知らせればよいか特定できませんでした。予定名か開始時刻を指定してください。"


def register_schedule_reminder(user_text: str) -> str | None:
    """通知依頼なら予約して確認文を返す。通知依頼でなければNone。"""
    if not _looks_like_reminder_request(user_text):
        return None

    plan = fetch_today_schedule()
    if not plan:
        return "今日の時間割を取得できなかったので、通知を予約できませんでした。"

    item, error = resolve_schedule_item_for_reminder(user_text, plan)
    if item is None:
        return error or "通知する予定を特定できませんでした。"

    lead_minutes = _parse_lead_minutes(user_text)
    payload = {
        "plan_id": plan.get("plan_id", ""),
        "item_id": item.get("item_id", ""),
        "lead_minutes": lead_minutes,
        "source_text": user_text,
    }
    try:
        response = requests.post(
            f"{SCHEDULE_BASE_URL}/reminder/add",
            json=payload,
            timeout=SCHEDULE_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        reminder = data.get("reminder") or {}
    except Exception as exc:
        print(f"⚠️ 通知予約失敗: {type(exc).__name__}: {exc}")
        return "通知の予約に失敗しました。時間割サーバーの状態を確認してください。"

    start = str(reminder.get("start_time") or item.get("start") or "")
    task = str(reminder.get("task") or item.get("task") or "予定")
    try:
        hour, minute = (int(x) for x in start.split(":", 1))
        time_text = f"{hour}時" if minute == 0 else f"{hour}時{minute:02d}分"
    except Exception:
        time_text = start
    if lead_minutes == 0:
        return f"はい。{time_text}からの{task}を、開始時刻にお知らせします。"
    return f"はい。{time_text}からの{task}を、{lead_minutes}分前にお知らせします。"


def _build_chat_messages(user_text: str) -> list[dict]:
    schedule_context = schedule_context_text()
    messages = [
        {
            "role": "system",
            "content": (
                "あなたは個人用AI秘書です。"
                "返答は日本語で、親しみはあるが落ち着いた自然な会話口調にしてください。"
                "原則として2〜4文程度で簡潔に答えてください。"
                "箇条書きや見出しは使わず、音声で聞いて自然な返答にしてください。"
                "直前までの会話文脈を踏まえて返答してください。"
                "予定の開始・終了時刻の変更は専用処理で反映されます。"
                "専用処理で扱えない複雑な予定変更については、勝手に変更したと断言しないでください。"
                "下に現在日時と今日の時間割を与えます。予定について聞かれた場合は、"
                "必ずこの時間割を根拠に答え、存在しない予定を作らないでください。"
                "『1時』のような時刻表現は会話文脈と時間割から自然に解釈してください。"
                "完了済みかどうかも必要に応じて反映してください。\n\n"
                + schedule_context
            ),
        }
    ]
    with history_lock:
        messages.extend(list(conversation_history))
    messages.append({"role": "user", "content": user_text})
    return messages


def _openai_reply(messages: list[dict]) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "--llm openai を使うには環境変数 OPENAI_API_KEY を設定してください。"
        )
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.6,
    )
    return (resp.choices[0].message.content or "").strip()


def _detect_local_model_id() -> str:
    """llama-server が公開しているモデルIDを取得。失敗時は設定値を使う。"""
    try:
        response = requests.get(
            f"{LOCAL_LLM_BASE_URL.rstrip('/')}/models",
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        models = data.get("data") if isinstance(data, dict) else None
        if isinstance(models, list) and models:
            model_id = str(models[0].get("id") or "").strip()
            if model_id:
                return model_id
    except Exception:
        pass
    return LOCAL_LLM_MODEL


def _local_llm_reply(messages: list[dict]) -> str:
    # llama.cpp の llama-server は OpenAI互換 /v1/chat/completions を提供する。
    # APIキーは不要だが、OpenAI Pythonクライアント側の都合でダミー値を渡す。
    client = OpenAI(
        base_url=LOCAL_LLM_BASE_URL.rstrip("/"),
        api_key="local-llama-cpp",
        timeout=3600.0,
    )
    resp = client.chat.completions.create(
        model=_detect_local_model_id(),
        messages=messages,
        temperature=0.6,
    )
    content = resp.choices[0].message.content
    return (content or "").strip()


def llm_reply(user_text: str) -> str:
    messages = _build_chat_messages(user_text)
    if LLM_BACKEND == "openai":
        reply = _openai_reply(messages)
    else:
        reply = _local_llm_reply(messages)

    if not reply:
        raise RuntimeError(f"{LLM_BACKEND} LLMから空の返答が返りました。")

    with history_lock:
        conversation_history.append({"role": "user", "content": user_text})
        conversation_history.append({"role": "assistant", "content": reply})
    return reply


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def split_sentences(text: str) -> list[str]:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [normalize_ws(line) for line in text.split("\n") if normalize_ws(line)]
    parts: list[str] = []
    pat = re.compile(r".*?(?:[。．！？!?]+[」』）\]\)]*|$)")

    for line in lines:
        pos = 0
        while pos < len(line):
            m = pat.match(line[pos:])
            if not m:
                break
            chunk = normalize_ws(m.group(0))
            pos += m.end()
            if not chunk:
                continue
            if len(chunk) <= 80:
                parts.append(chunk)
                continue
            sub_parts = re.split(r"(?<=[、，])", chunk)
            buf = ""
            for sp in sub_parts:
                sp = normalize_ws(sp)
                if not sp:
                    continue
                if not buf or len(buf) + len(sp) <= 80:
                    buf += sp
                else:
                    parts.append(buf)
                    buf = sp
            if buf:
                parts.append(buf)
    return parts




def _spoken_time(hour_text: str, minute_text: str) -> str:
    hour = int(hour_text)
    minute = int(minute_text)
    if minute == 0:
        return f"{hour}時"
    return f"{hour}時{minute}分"


def prepare_text_for_voicevox(text: str) -> str:
    """画面表示用文字列は変えず、VOICEVOXへ渡す直前だけ時刻を自然な日本語へする。"""
    out = text or ""

    # 12:00〜12:30 / 12:00-12:30 など。
    range_pat = re.compile(
        r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)\s*"
        r"[〜~～\-–—−]\s*([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)"
    )

    def repl_range(m: re.Match) -> str:
        start = _spoken_time(m.group(1), m.group(2))
        end = _spoken_time(m.group(3), m.group(4))
        return f"{start}から{end}は"

    out = range_pat.sub(repl_range, out)

    # 単独の 08:30 のような表記も読む。範囲変換後には残ったものだけが対象。
    single_pat = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)")
    out = single_pat.sub(lambda m: _spoken_time(m.group(1), m.group(2)), out)
    return out


def synthesize_voicevox_wav(
    text: str,
    speaker: int = VOICEVOX_SPEAKER,
    speed: float = VOICEVOX_SPEED,
) -> bytes:
    query_response = requests.post(
        f"{VOICEVOX_HOST.rstrip('/')}/audio_query",
        params={"text": text, "speaker": speaker},
        timeout=60,
    )
    query_response.raise_for_status()
    query = query_response.json()
    query["speedScale"] = float(speed)

    synthesis_response = requests.post(
        f"{VOICEVOX_HOST.rstrip('/')}/synthesis",
        params={"speaker": speaker},
        data=json.dumps(query),
        headers={"Content-Type": "application/json"},
        timeout=120,
    )
    synthesis_response.raise_for_status()
    return synthesis_response.content


def voicevox_wav_to_16k_mono_pcm(wav_bytes: bytes) -> bytes:
    pcm, rate = sf.read(io.BytesIO(wav_bytes), dtype="int16", always_2d=True)
    mono = pcm.mean(axis=1).astype(np.int16)
    if rate != SAMPLE_RATE:
        pcm_float = mono.astype(np.float32) / 32768.0
        pcm_float = resampy.resample(pcm_float, rate, SAMPLE_RATE)
        mono = (np.clip(pcm_float, -1.0, 1.0) * 32767).astype(np.int16)
    return mono.tobytes()


def send_reply_stream(
    conn: socket.socket,
    reply_text: str,
    speaker: int = VOICEVOX_SPEAKER,
    speed: float = VOICEVOX_SPEED,
) -> None:
    voice_text = prepare_text_for_voicevox(reply_text)
    sentences = split_sentences(voice_text)
    if not sentences:
        send_pcm_chunk(conn, b"")
        return

    q_audio: queue.Queue = queue.Queue(maxsize=2)
    sentinel = object()

    def producer() -> None:
        try:
            for idx, sentence in enumerate(sentences):
                try:
                    print(f"VOICEVOX 文{idx + 1}/{len(sentences)} 合成中: {sentence}")
                    wav_bytes = synthesize_voicevox_wav(sentence, speaker=speaker, speed=speed)
                    pcm_bytes = voicevox_wav_to_16k_mono_pcm(wav_bytes)
                    q_audio.put((idx, sentence, pcm_bytes, None))
                except Exception as exc:
                    q_audio.put((idx, sentence, None, exc))
        finally:
            q_audio.put(sentinel)

    threading.Thread(target=producer, daemon=True).start()

    sent_count = 0
    while True:
        item = q_audio.get()
        if item is sentinel:
            break
        idx, sentence, pcm_bytes, error = item
        if error is not None:
            print(f"VOICEVOX 文{idx + 1} 合成エラー: {error}")
            continue
        if not pcm_bytes:
            continue
        seconds = len(pcm_bytes) / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)
        print(f"返送 文{idx + 1}/{len(sentences)}: {len(pcm_bytes)} bytes ({seconds:.2f} 秒)")
        send_pcm_chunk(conn, pcm_bytes)
        sent_count += 1

    send_pcm_chunk(conn, b"")
    print(f"音声ストリーム返送完了: {sent_count} 文")


def handle_client(conn: socket.socket, addr) -> None:
    print(f"\n接続: {addr[0]}:{addr[1]}")

    pcm_length = struct.unpack("<I", recv_exact(conn, 4))[0]

    # Protocol v2拡張: 0xFFFFFFFF は、クライアントからの
    # 「この文章をそのままVOICEVOXで通知してほしい」という要求。
    # 通常の音声会話プロトコルは従来どおりで互換性を保つ。
    if pcm_length == TEXT_REQUEST_MARKER:
        text_length = struct.unpack("<I", recv_exact(conn, 4))[0]
        if text_length <= 0 or text_length > MAX_TEXT_REQUEST_BYTES:
            raise ValueError(f"不正な通知テキストサイズです: {text_length} bytes")
        notification_text = recv_exact(conn, text_length).decode("utf-8", errors="replace").strip()
        print(f"🔔 予定通知要求: {notification_text}")
        send_text(conn, "")
        send_text(conn, notification_text)
        send_reply_stream(conn, notification_text)
        return

    print(f"受信予定PCMサイズ: {pcm_length} bytes")
    max_length = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * 60
    if pcm_length <= 0 or pcm_length > max_length:
        raise ValueError(f"不正なPCMサイズです: {pcm_length} bytes")

    pcm_data = recv_exact(conn, pcm_length)
    seconds = len(pcm_data) / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)
    print(f"受信完了: {len(pcm_data)} bytes / 約 {seconds:.2f} 秒")

    wav_path = save_pcm_as_wav(pcm_data)
    print(f"WAV保存: {wav_path.resolve()}")

    print("Whisper認識中...")
    text = transcribe_pcm16(pcm_data)
    print(f"📝 認識結果: {text or '（空）'}")

    # Protocol v2: 認識文を先に送る。
    send_text(conn, text)

    if not text:
        # AI返答も空、続いて音声終端。
        send_text(conn, "")
        send_pcm_chunk(conn, b"")
        return

    add_schedule_reply = add_schedule_item_by_voice(text)
    if add_schedule_reply is not None:
        reply = add_schedule_reply
        print(f"➕ 予定追加応答: {reply}")
    else:
        time_change_reply = change_schedule_item_time(text)
        if time_change_reply is not None:
            reply = time_change_reply
            print(f"🕒 予定時刻変更応答: {reply}")
        else:
            completion_reply = complete_schedule_item(text)
            if completion_reply is not None:
                reply = completion_reply
                print(f"✅ 作業完了応答: {reply}")
            else:
                reminder_reply = register_schedule_reminder(text)
                if reminder_reply is not None:
                    reply = reminder_reply
                    print(f"🔔 通知予約応答: {reply}")
                else:
                    reply = llm_reply(text)
                    print(f"🤖 応答: {reply}")

    # Protocol v2: AI返答文を音声より先に送る。
    send_text(conn, reply)

    print("VOICEVOX文分割・ストリーミング返送開始...")
    send_reply_stream(conn, reply)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="1日の時間割 + AI秘書 音声会話サーバー v2"
    )
    parser.add_argument(
        "--llm",
        choices=("local", "openai"),
        default="local",
        help="会話に使うLLM。既定値: local",
    )
    return parser.parse_args()


def main() -> None:
    global LLM_BACKEND
    args = parse_args()
    LLM_BACKEND = args.llm

    print("=== 1日の時間割 + AI秘書 音声会話サーバー v2 ===")
    print(f"待受: {HOST}:{PORT}")
    print(f"PCM形式: {SAMPLE_RATE} Hz / 16 bit / mono")
    print("Whisper: small / CUDA / float16")
    if LLM_BACKEND == "local":
        print(f"LLM: local / {LOCAL_LLM_BASE_URL} / model={LOCAL_LLM_MODEL}")
    else:
        print(f"LLM: OpenAI / model={OPENAI_MODEL}")
    print(f"時間割: {SCHEDULE_URL}")
    print(f"VOICEVOX: {VOICEVOX_HOST} / speaker={VOICEVOX_SPEAKER}")
    print("Protocol v2: recognized text + reply text + streaming PCM")
    print("終了: Ctrl+C")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(5)
        while True:
            conn, addr = server.accept()
            with conn:
                try:
                    handle_client(conn, addr)
                except Exception as error:
                    print(f"処理エラー: {type(error).__name__}: {error}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n終了します")
