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

import asyncio
import socket
import struct
import json
import urllib.error
import urllib.request
import os
from pathlib import Path
import tomllib
from datetime import datetime, timedelta

import flet as ft

from audio_backend import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH, create_audio_backend
from vad_recorder import VadRecorder


# Android APKでは client_config.py にビルド時設定を埋め込む。
# Ubuntu/Windowsでソースから直接実行するときは、プロジェクト直下の
# config.toml（または AI_SECRETARY_CONFIG で指定したTOML）があれば上書きする。
try:
    from client_config import (
        AI_HOST as _BUILTIN_AI_HOST,
        VOICE_PORT as _BUILTIN_VOICE_PORT,
        SCHEDULE_PORT as _BUILTIN_SCHEDULE_PORT,
    )
except Exception:
    _BUILTIN_AI_HOST = "127.0.0.1"
    _BUILTIN_VOICE_PORT = 50000
    _BUILTIN_SCHEDULE_PORT = 8766


def _load_desktop_client_config() -> dict:
    candidates: list[Path] = []
    env_path = os.getenv("AI_SECRETARY_CONFIG")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    # ソースツリーでの実行時: src/main.py の1階層上。
    candidates.append(Path(__file__).resolve().parents[1] / "config.toml")
    for path in candidates:
        if path.exists():
            try:
                with path.open("rb") as f:
                    return tomllib.load(f).get("client", {})
            except Exception:
                pass
    return {}


_CLIENT_CONFIG = _load_desktop_client_config()
AI_HOST = str(_CLIENT_CONFIG.get("server_host", _BUILTIN_AI_HOST))
PORT = int(_CLIENT_CONFIG.get("voice_port", _BUILTIN_VOICE_PORT))
SCHEDULE_HOST = AI_HOST
SCHEDULE_PORT = int(_CLIENT_CONFIG.get("schedule_port", _BUILTIN_SCHEDULE_PORT))
SCHEDULE_TIMEOUT = 10.0
SOCKET_TIMEOUT = 120.0
MAX_REPLY_SECONDS = 120
MAX_TEXT_BYTES = 256 * 1024
TEXT_REQUEST_MARKER = 0xFFFFFFFF
SCHEDULE_NOTIFY_MINUTES = 5
SCHEDULE_NOTIFY_POLL_SECONDS = 20

SECRETARY_NORMAL = "secretary_normal.png"
SECRETARY_TALKING = "secretary_mouth_open.png"
SECRETARY_HAPPY = "secretary_happy.png"      # 今回は未使用。将来用
SECRETARY_WARNING = "secretary_warning.png"  # 今回は未使用。将来用
SECRETARY_WIDTH = 150
SECRETARY_TALK_INTERVAL = 0.25  # 秒。口の開閉切替周期

def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(min(65536, size - len(data)))
        if not chunk:
            raise ConnectionError(
                f"受信途中で接続が切れました: {len(data)}/{size} bytes"
            )
        data.extend(chunk)
    return bytes(data)


def recv_u32(sock: socket.socket) -> int:
    return struct.unpack("<I", recv_exact(sock, 4))[0]


def recv_text(sock: socket.socket) -> str:
    length = recv_u32(sock)
    if length > MAX_TEXT_BYTES:
        raise RuntimeError(f"テキストサイズが不正です: {length} bytes")
    if length == 0:
        return ""
    return recv_exact(sock, length).decode("utf-8", errors="replace")


def fetch_schedule_json(host: str, port: int, path: str = "/today") -> dict:
    """ホスト側の時間割HTTPサーバーからJSONを取得する。"""
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Flet-AI-Secretary/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=SCHEDULE_TIMEOUT) as res:
            raw = res.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"時間割HTTPエラー {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"時間割サーバーへ接続できません: {exc.reason}") from exc

    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"時間割サーバー応答が不正です: {data!r}")
    if not isinstance(data.get("plan"), dict):
        raise RuntimeError("時間割サーバー応答に plan がありません")
    return data




def fetch_reminders_json(host: str, port: int) -> dict:
    """今日の未送信通知予約を取得する。"""
    url = f"http://{host}:{port}/reminders/today"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Flet-AI-Secretary/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=SCHEDULE_TIMEOUT) as res:
            raw = res.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"通知予約HTTPエラー {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"時間割サーバーへ接続できません: {exc.reason}") from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"通知予約応答が不正です: {data!r}")
    return data


def mark_reminder_sent_json(host: str, port: int, reminder_id: str) -> dict:
    """通知予約を送信済みにする。"""
    url = f"http://{host}:{port}/reminder/sent"
    body = json.dumps({"reminder_id": reminder_id}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Flet-AI-Secretary/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=SCHEDULE_TIMEOUT) as res:
            raw = res.read()
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"通知送信済み更新HTTPエラー {exc.code}: {response_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"時間割サーバーへ接続できません: {exc.reason}") from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"通知送信済み更新応答が不正です: {data!r}")
    return data



def update_schedule_json(host: str, port: int, plan: dict) -> dict:
    """クライアントで編集した時間割をホスト側SQLiteへ反映する。"""
    url = f"http://{host}:{port}/plan/update"
    body = json.dumps(plan, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Flet-AI-Secretary/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=SCHEDULE_TIMEOUT) as res:
            raw = res.read()
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"時間割更新HTTPエラー {exc.code}: {response_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"時間割サーバーへ接続できません: {exc.reason}") from exc

    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"時間割更新応答が不正です: {data!r}")
    return data


def update_item_json(
    host: str, port: int, plan_id: str, item_id: str,
    start: str, end: str, task: str, note: str, category: str = ""
) -> dict:
    """予定1項目の時刻・内容を更新する。"""
    url = f"http://{host}:{port}/item/update"
    body = json.dumps(
        {
            "plan_id": plan_id, "item_id": item_id,
            "start": start, "end": end, "task": task,
            "note": note, "category": category,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Flet-AI-Secretary/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=SCHEDULE_TIMEOUT) as res:
            raw = res.read()
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"予定項目更新HTTPエラー {exc.code}: {response_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"時間割サーバーへ接続できません: {exc.reason}") from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"予定項目更新応答が不正です: {data!r}")
    return data



def add_item_json(
    host: str, port: int, plan_id: str,
    start: str, end: str, task: str, note: str = "",
    category: str = "focus", source: str = "manual",
) -> dict:
    """今日の時間割へ予定項目を1件追加する。"""
    url = f"http://{host}:{port}/item/add"
    body = json.dumps(
        {
            "plan_id": plan_id, "start": start, "end": end, "task": task,
            "note": note, "category": category, "source": source,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Flet-AI-Secretary/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=SCHEDULE_TIMEOUT) as res:
            raw = res.read()
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"予定項目追加HTTPエラー {exc.code}: {response_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"時間割サーバーへ接続できません: {exc.reason}") from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"予定項目追加応答が不正です: {data!r}")
    return data

def set_item_completion_json(
    host: str, port: int, plan_id: str, item_id: str, completed: bool
) -> dict:
    """予定項目の完了状態を更新し、完了時刻をサーバー側に記録する。"""
    url = f"http://{host}:{port}/item/complete"
    body = json.dumps(
        {"plan_id": plan_id, "item_id": item_id, "completed": completed},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Flet-AI-Secretary/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=SCHEDULE_TIMEOUT) as res:
            raw = res.read()
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"作業完了更新HTTPエラー {exc.code}: {response_body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"時間割サーバーへ接続できません: {exc.reason}") from exc

    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"作業完了更新応答が不正です: {data!r}")
    return data


def time_to_minutes(value: str) -> int:
    """HH:MM を0時からの分へ変換する。"""
    hour_text, minute_text = value.strip().split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(value)
    return hour * 60 + minute


def minutes_to_time(value: int) -> str:
    """0時からの分を HH:MM に戻す。24時間を越えた場合は翌日に丸める。"""
    value %= 24 * 60
    return f"{value // 60:02d}:{value % 60:02d}"


def item_duration_minutes(item: dict) -> int:
    """項目の start/end から所要時間を分で返す。"""
    start = time_to_minutes(str(item.get("time") or ""))
    end = time_to_minutes(str(item.get("end") or ""))
    duration = end - start
    if duration <= 0:
        duration += 24 * 60
    return duration


def schedule_plan_to_items(plan: dict) -> list[dict]:
    """HTTPサーバーの plan.items をFlet表示用itemsへ変換する。"""
    server_items = plan.get("items") or []
    if not isinstance(server_items, list):
        raise RuntimeError("plan.items が配列ではありません")

    items: list[dict] = []
    for index, row in enumerate(server_items):
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("item_id") or f"server-{index}")
        start = str(row.get("start") or "")
        end = str(row.get("end") or "")
        task = str(row.get("task") or "")
        note = str(row.get("note") or "")

        # 既存UIは time/title/memo/checked を参照するので、その形へ合わせる。
        # end/category 等も将来用に保持しておく。
        items.append(
            {
                "id": item_id,
                "title": task,
                "time": start,
                "end": end,
                "memo": note,
                "category": str(row.get("category") or ""),
                "checked": bool(row.get("checked", False)),
                "completed_at": str(row.get("completed_at") or ""),
                "sort_order": int(row.get("sort_order", index)),
                "server_item_id": item_id,
            }
        )

    items.sort(key=lambda x: x.get("sort_order", 0))
    return items


class VoiceTextStream:
    """AIホストへ通知文を送り、VOICEVOX音声ストリームを受け取る。"""

    def __init__(self, text: str, host: str, port: int):
        text_bytes = text.encode("utf-8")
        self.sock = socket.create_connection((host, port), timeout=SOCKET_TIMEOUT)
        self.sock.settimeout(SOCKET_TIMEOUT)
        self.closed = False
        self.sock.sendall(struct.pack("<I", TEXT_REQUEST_MARKER))
        self.sock.sendall(struct.pack("<I", len(text_bytes)))
        self.sock.sendall(text_bytes)
        self.recognized_text = recv_text(self.sock)
        self.reply_text = recv_text(self.sock)
        self.max_reply = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * MAX_REPLY_SECONDS

    def next_pcm(self) -> bytes | None:
        if self.closed:
            return None
        reply_length = recv_u32(self.sock)
        if reply_length == 0:
            self.close()
            return None
        if reply_length > self.max_reply:
            self.close()
            raise RuntimeError(f"AI応答音声サイズが不正です: {reply_length} bytes")
        return recv_exact(self.sock, reply_length)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.sock.close()
            except Exception:
                pass


class VoiceReplyStream:
    """
    Protocol v2 client.

    Client -> Server:
      <u32 pcm_length><pcm>

    Server -> Client:
      <u32 recognized_utf8_length><recognized_utf8>
      <u32 reply_utf8_length><reply_utf8>
      (<u32 pcm_length><pcm>)*
      <u32 0>
    """

    def __init__(self, pcm_data: bytes, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=SOCKET_TIMEOUT)
        self.sock.settimeout(SOCKET_TIMEOUT)
        self.closed = False

        self.sock.sendall(struct.pack("<I", len(pcm_data)))
        self.sock.sendall(pcm_data)

        self.recognized_text = recv_text(self.sock)
        self.reply_text = recv_text(self.sock)
        self.max_reply = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * MAX_REPLY_SECONDS

    def next_pcm(self) -> bytes | None:
        if self.closed:
            return None
        reply_length = recv_u32(self.sock)
        if reply_length == 0:
            self.close()
            return None
        if reply_length > self.max_reply:
            self.close()
            raise RuntimeError(f"AI応答音声サイズが不正です: {reply_length} bytes")
        return recv_exact(self.sock, reply_length)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.sock.close()
            except Exception:
                pass


async def main(page: ft.Page):
    page.title = "1日の時間割 + AI秘書 音声会話テスト"
    page.padding = 12

    audio = create_audio_backend(page)
    await audio.initialize()
    vad = VadRecorder()

    running = False
    conversation_task: asyncio.Task | None = None
    last_reply_pcm: bytes | None = None
    notified_schedule_keys: set[str] = set()

    # ------------------------------------------------------------
    # 1日の時間割
    # ホスト側SQLite -> HTTP /today から取得する。
    # クライアント側で編集・並べ替えした内容はHTTP経由でホストDBへ書き戻す。
    # ------------------------------------------------------------
    items: list[dict] = []
    current_plan_id = ""
    current_plan_date = ""
    current_plan_title = ""

    # ------------------------------------------------------------
    # 会話UI
    # ------------------------------------------------------------
    host_field = ft.TextField(label="AIコンピュータ", value=AI_HOST, width=260)
    port_field = ft.TextField(label="音声PORT", value=str(PORT), width=120)
    schedule_host_field = ft.TextField(
        label="時間割サーバー",
        value=SCHEDULE_HOST,
        width=260,
    )
    schedule_port_field = ft.TextField(
        label="時間割PORT",
        value=str(SCHEDULE_PORT),
        width=120,
    )

    state_text = ft.Text("停止中", size=14, weight=ft.FontWeight.BOLD)
    recognized_text = ft.Text("あなた：―", size=12, selectable=True)
    reply_text = ft.Text("秘書：―", size=12, selectable=True)
    detail_text = ft.Text(
        f"backend: {audio.name} / VAD: {vad.detector_name}",
        size=10,
        color=ft.Colors.GREY_700,
        selectable=True,
    )

    conversation_visible = False
    toggle_log_button = ft.IconButton(
        icon=ft.Icons.EXPAND_MORE,
        tooltip="会話表示",
    )

    recognized_text.visible = False
    reply_text.visible = False
    detail_text.visible = False

    def toggle_conversation_log(e):
        nonlocal conversation_visible

        conversation_visible = not conversation_visible

        recognized_text.visible = conversation_visible
        reply_text.visible = conversation_visible
        detail_text.visible = conversation_visible

        toggle_log_button.icon = (
            ft.Icons.EXPAND_LESS
            if conversation_visible
            else ft.Icons.EXPAND_MORE
        )

        page.update()

    toggle_log_button.on_click = toggle_conversation_log

    start_button = ft.Button("会話開始", icon=ft.Icons.MIC)
    stop_button = ft.Button("停止", icon=ft.Icons.STOP, disabled=True)
    replay_button = ft.Button("再生", icon=ft.Icons.VOLUME_UP, disabled=True)
    settings_button = ft.IconButton(icon=ft.Icons.SETTINGS, tooltip="音声接続設定")

    secretary_image = ft.Image(
        src=SECRETARY_NORMAL,
        width=SECRETARY_WIDTH,
        fit=ft.BoxFit.CONTAIN,
    )

    def set_secretary(image_name: str) -> None:
        secretary_image.src = image_name

    async def animate_secretary_talking(stop_event: asyncio.Event) -> None:
        """VOICEVOX再生中、normal と mouth_open を交互に表示する。"""
        mouth_open = False
        try:
            while not stop_event.is_set():
                set_secretary(SECRETARY_TALKING if mouth_open else SECRETARY_NORMAL)
                page.update()
                mouth_open = not mouth_open
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=SECRETARY_TALK_INTERVAL)
                except asyncio.TimeoutError:
                    pass
        finally:
            set_secretary(SECRETARY_NORMAL)
            page.update()

    playback_active = asyncio.Event()
    PLAYBACK_MIC_GUARD_SECONDS = 0.8

    async def play_with_secretary_animation(pcm: bytes) -> None:
        """音声再生と秘書の口パクを並行実行する。

        秘書自身のスピーカー音をVADがユーザー発話として拾わないよう、
        再生前にマイク入力を停止し、再生後もしばらくVAD再開を遅らせる。
        """
        playback_active.set()
        try:
            # conversation_loop が VAD 待機中でも、ここで録音を停止する。
            # VAD側は「再生による一時停止」と判定して再開する。
            try:
                await audio.stop_capture()
            except Exception:
                pass

            stop_event = asyncio.Event()
            animation_task = asyncio.create_task(animate_secretary_talking(stop_event))
            try:
                await audio.play(pcm)
            finally:
                stop_event.set()
                try:
                    await animation_task
                except asyncio.CancelledError:
                    pass
                set_secretary(SECRETARY_NORMAL)
                page.update()

            # Android端末のスピーカー残響・AudioTrack終了直後の音を拾わないための猶予。
            await asyncio.sleep(PLAYBACK_MIC_GUARD_SECONDS)
        finally:
            playback_active.clear()

    def set_voice_state(label: str, detail: str | None = None) -> None:
        state_text.value = label
        if detail is not None:
            detail_text.value = detail
        page.update()

    def vad_status(kind: str, detector_name: str) -> None:
        if kind == "waiting":
            set_secretary(SECRETARY_NORMAL)
            set_voice_state(
                "🎤 話しかけてください",
                f"backend: {audio.name} / VAD: {detector_name} / 100ms有声開始・800ms無音確定",
            )
        elif kind == "recording":
            set_secretary(SECRETARY_NORMAL)
            set_voice_state(
                "🔴 聞いています…",
                f"backend: {audio.name} / VAD: {detector_name} / 話し終わると自動送信",
            )

    def open_settings(e):
        def close_dialog(ev=None):
            page.pop_dialog()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("音声接続設定"),
            content=ft.Column(
                [
                    host_field,
                    port_field,
                    ft.Divider(),
                    schedule_host_field,
                    schedule_port_field,
                    ft.Text(
                        "音声: Protocol v2 / 時間割: HTTP GET /today",
                        size=11,
                    ),
                ],
                tight=True,
            ),
            actions=[ft.TextButton("閉じる", on_click=close_dialog)],
        )
        page.show_dialog(dialog)

    settings_button.on_click = open_settings

    # ------------------------------------------------------------
    # 時間割UI
    # ------------------------------------------------------------
    reorder_list = ft.ReorderableListView(expand=True, spacing=5)
    schedule_status = ft.Text("時間割を取得しています…", size=11)
    refresh_schedule_button = ft.Button(
        "時間割更新",
        icon=ft.Icons.REFRESH,
    )
    add_schedule_button = ft.Button(
        "予定追加",
        icon=ft.Icons.ADD,
    )

    async def save_schedule_to_server(status_prefix: str = "時間割") -> None:
        """現在のクライアント側時間割をまとめてホストDBへ保存する。"""
        if not current_plan_id:
            schedule_status.value = "保存できません: plan_id がありません"
            page.update()
            return

        host = (schedule_host_field.value or SCHEDULE_HOST).strip()
        try:
            port = int(schedule_port_field.value or SCHEDULE_PORT)
        except ValueError:
            schedule_status.value = "保存できません: 時間割PORTが不正です"
            page.update()
            return

        payload = {
            "plan_id": current_plan_id,
            "plan_date": current_plan_date,
            "title": current_plan_title,
            "items": [
                {
                    "item_id": str(item.get("server_item_id") or item["id"]),
                    "sort_order": index,
                    "start": str(item.get("time") or ""),
                    "end": str(item.get("end") or ""),
                    "task": str(item.get("title") or ""),
                    "note": str(item.get("memo") or ""),
                    "category": str(item.get("category") or ""),
                    "checked": bool(item.get("checked", False)),
                }
                for index, item in enumerate(items)
            ],
        }

        try:
            await asyncio.to_thread(update_schedule_json, host, port, payload)
            schedule_status.value = f"{status_prefix}をホストへ保存しました"
        except Exception as exc:
            schedule_status.value = (
                f"ホスト保存エラー: {type(exc).__name__}: {exc}"
            )
        page.update()

    def recalculate_times_after_reorder(first_start: str, durations: dict[str, int]) -> None:
        """並べ替え後、各項目の所要時間を保ったまま開始・終了時刻を詰め直す。"""
        cursor = time_to_minutes(first_start)
        for index, item in enumerate(items):
            item_id = str(item["id"])
            duration = durations[item_id]
            item["time"] = minutes_to_time(cursor)
            cursor += duration
            item["end"] = minutes_to_time(cursor)
            item["sort_order"] = index

    async def checkbox_changed(e: ft.Event):
        item_id = str(e.control.data)
        item = next((x for x in items if str(x["id"]) == item_id), None)
        if item is None:
            return

        completed = bool(e.control.value)
        old_checked = bool(item.get("checked", False))
        item["checked"] = completed
        schedule_status.value = (
            f'{item["title"]}: '
            f'{"完了を記録中…" if completed else "完了取り消し中…"}'
        )
        page.update()

        host = (schedule_host_field.value or SCHEDULE_HOST).strip()
        try:
            port = int(schedule_port_field.value or SCHEDULE_PORT)
            data = await asyncio.to_thread(
                set_item_completion_json,
                host,
                port,
                current_plan_id,
                str(item.get("server_item_id") or item["id"]),
                completed,
            )
            server_item = data.get("item") or {}
            item["completed_at"] = str(server_item.get("completed_at") or "")
            if completed:
                schedule_status.value = f'{item["title"]}: 作業完了を記録しました'
            else:
                schedule_status.value = f'{item["title"]}: 完了を取り消しました'
        except Exception as exc:
            # サーバー更新に失敗した場合はUIを元の状態へ戻す。
            item["checked"] = old_checked
            e.control.value = old_checked
            schedule_status.value = f"完了更新エラー: {type(exc).__name__}: {exc}"
        page.update()

    def open_add_schedule_dialog(e=None):
        title_field = ft.TextField(label="タイトル", autofocus=True)
        start_field = ft.TextField(label="開始時刻", hint_text="例: 10:30")
        end_field = ft.TextField(label="終了時刻", hint_text="例: 11:00")
        memo_field = ft.TextField(
            label="メモ", multiline=True, min_lines=2, max_lines=4,
        )

        def close_dialog(ev=None):
            page.pop_dialog()

        async def add_item_from_dialog(ev):
            new_title = (title_field.value or "").strip()
            if not new_title:
                title_field.error_text = "タイトルを入力してください"
                title_field.update()
                return
            new_start = (start_field.value or "").strip()
            new_end = (end_field.value or "").strip()
            try:
                time_to_minutes(new_start)
            except Exception:
                start_field.error_text = "時刻は HH:MM 形式で入力してください"
                start_field.update()
                return
            try:
                time_to_minutes(new_end)
            except Exception:
                end_field.error_text = "時刻は HH:MM 形式で入力してください"
                end_field.update()
                return
            if new_start == new_end:
                end_field.error_text = "開始時刻と終了時刻を同じにはできません"
                end_field.update()
                return
            if not current_plan_id:
                schedule_status.value = "予定を追加できません: plan_id がありません"
                page.update()
                return

            host = (schedule_host_field.value or SCHEDULE_HOST).strip()
            try:
                port = int(schedule_port_field.value or SCHEDULE_PORT)
                await asyncio.to_thread(
                    add_item_json, host, port, current_plan_id,
                    new_start, new_end, new_title,
                    (memo_field.value or "").strip(), "focus", "manual",
                )
            except Exception as exc:
                schedule_status.value = f"予定追加エラー: {type(exc).__name__}: {exc}"
                page.update()
                return

            close_dialog()
            await load_schedule_from_server(show_success=False)
            schedule_status.value = f'{new_title} を {new_start}〜{new_end} に追加しました'
            page.update()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("予定を追加"),
            content=ft.Container(
                width=420,
                content=ft.Column(
                    [title_field, start_field, end_field, memo_field],
                    tight=True, scroll=ft.ScrollMode.AUTO,
                ),
            ),
            actions=[
                ft.TextButton("キャンセル", on_click=close_dialog),
                ft.FilledButton("追加", on_click=add_item_from_dialog),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dialog)

    add_schedule_button.on_click = open_add_schedule_dialog

    def row_clicked(e: ft.Event):
        item_id = str(e.control.data)
        item = next((item for item in items if str(item["id"]) == item_id), None)
        if item is None:
            return

        title_field = ft.TextField(label="タイトル", value=item["title"], autofocus=True)
        start_field = ft.TextField(label="開始時刻", value=item["time"], hint_text="例: 08:30")
        end_field = ft.TextField(label="終了時刻", value=item["end"], hint_text="例: 09:30")
        memo_field = ft.TextField(
            label="メモ",
            value=item["memo"],
            multiline=True,
            min_lines=2,
            max_lines=4,
        )

        def close_dialog(ev=None):
            page.pop_dialog()

        async def save_item(ev):
            new_title = (title_field.value or "").strip()
            if not new_title:
                title_field.error_text = "タイトルを入力してください"
                title_field.update()
                return

            new_start = (start_field.value or "").strip()
            new_end = (end_field.value or "").strip()
            try:
                time_to_minutes(new_start)
            except Exception:
                start_field.error_text = "時刻は HH:MM 形式で入力してください"
                start_field.update()
                return
            try:
                time_to_minutes(new_end)
            except Exception:
                end_field.error_text = "時刻は HH:MM 形式で入力してください"
                end_field.update()
                return
            if new_start == new_end:
                end_field.error_text = "開始時刻と終了時刻を同じにはできません"
                end_field.update()
                return

            host = (schedule_host_field.value or SCHEDULE_HOST).strip()
            try:
                port = int(schedule_port_field.value or SCHEDULE_PORT)
                data = await asyncio.to_thread(
                    update_item_json, host, port, current_plan_id,
                    str(item.get("server_item_id") or item["id"]),
                    new_start, new_end, new_title,
                    (memo_field.value or "").strip(),
                    str(item.get("category") or ""),
                )
                saved = data.get("item") or {}
            except Exception as exc:
                schedule_status.value = f"予定更新エラー: {type(exc).__name__}: {exc}"
                page.update()
                return

            item["title"] = str(saved.get("task") or new_title)
            item["time"] = str(saved.get("start") or new_start)
            item["end"] = str(saved.get("end") or new_end)
            item["memo"] = str(saved.get("note") or (memo_field.value or "").strip())
            schedule_status.value = f'{item["title"]} を {item["time"]}〜{item["end"]} に更新しました'
            close_dialog()
            rebuild_schedule()
            page.update()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("項目を編集"),
            content=ft.Container(
                width=420,
                content=ft.Column(
                    [title_field, start_field, end_field, memo_field],
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                ),
            ),
            actions=[
                ft.TextButton("キャンセル", on_click=close_dialog),
                ft.FilledButton("保存", on_click=save_item),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dialog)

    def rebuild_schedule():
        reorder_list.controls.clear()
        for item in items:
            row_content = ft.Row(
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[
                    ft.Checkbox(
                        value=item["checked"],
                        data=item["id"],
                        on_change=checkbox_changed,
                    ),
                    ft.Column(
                        expand=True,
                        spacing=2,
                        controls=[
                            ft.Text(item["title"], size=18, weight=ft.FontWeight.BOLD),
                            ft.Text(f' {item["time"]}〜{item["end"]}', size=12, color=ft.Colors.BLUE),
                            ft.Text(item["memo"], size=12, color=ft.Colors.GREY_700),
                        ],
                    ),
                    ft.Icon(ft.Icons.EDIT_OUTLINED),
                ],
            )
            reorder_list.controls.append(
                ft.Container(
                    key=f'item-{item["id"]}',
                    data=item["id"],
                    content=row_content,
                    padding=10,
                    border=ft.Border.all(1, ft.Colors.GREY_400),
                    border_radius=8,
                    on_click=row_clicked,
                    ink=True,
                )
            )

    async def load_schedule_from_server(show_success: bool = True) -> None:
        nonlocal items, current_plan_id, current_plan_date, current_plan_title

        refresh_schedule_button.disabled = True
        schedule_status.value = "ホストから今日の時間割を取得中…"
        page.update()

        host = (schedule_host_field.value or SCHEDULE_HOST).strip()
        try:
            port = int(schedule_port_field.value or SCHEDULE_PORT)
        except ValueError:
            refresh_schedule_button.disabled = False
            schedule_status.value = "時間割PORTが不正です"
            page.update()
            return

        try:
            data = await asyncio.to_thread(
                fetch_schedule_json,
                host,
                port,
                "/today",
            )
            plan = data["plan"]
            new_items = schedule_plan_to_items(plan)

            items.clear()
            items.extend(new_items)

            current_plan_id = str(plan.get("plan_id") or "")
            current_plan_date = str(plan.get("plan_date") or "")
            current_plan_title = str(plan.get("title") or "")

            rebuild_schedule()
            if show_success:
                schedule_status.value = (
                    f"{current_plan_date} / {len(items)}件をホストから取得しました"
                )
            else:
                schedule_status.value = (
                    f"{current_plan_date} / {len(items)}件"
                )

        except Exception as exc:
            schedule_status.value = (
                f"時間割取得エラー: {type(exc).__name__}: {exc}"
            )
        finally:
            refresh_schedule_button.disabled = False
            page.update()

    async def refresh_schedule(e):
        await load_schedule_from_server(show_success=True)

    refresh_schedule_button.on_click = refresh_schedule

    async def on_reorder(e: ft.OnReorderEvent):
        if not items:
            return

        # 並べ替え前に「一日の開始時刻」と「各項目の所要時間」を保存する。
        # 順番を変えた後は、その所要時間を変えずに時刻だけ詰め直す。
        first_start = str(items[0].get("time") or "")
        try:
            durations = {
                str(item["id"]): item_duration_minutes(item)
                for item in items
            }
        except Exception as exc:
            schedule_status.value = f"並べ替えエラー: 時刻形式を確認してください ({exc})"
            page.update()
            return

        old_index = e.old_index
        new_index = e.new_index
        if old_index < new_index:
            new_index -= 1
        moved_item = items.pop(old_index)
        items.insert(new_index, moved_item)

        recalculate_times_after_reorder(first_start, durations)
        schedule_status.value = (
            f'{moved_item["title"]} を {old_index}番目から {new_index}番目へ移動'
        )
        rebuild_schedule()
        page.update()
        await save_schedule_to_server(f'{moved_item["title"]} の並べ替え')

    reorder_list.on_reorder = on_reorder
    rebuild_schedule()

    async def speak_schedule_notification(text: str) -> None:
        """予定通知文をAIホストのVOICEVOXで合成し、そのまま再生する。"""
        nonlocal last_reply_pcm
        host = (host_field.value or AI_HOST).strip()
        port = int(port_field.value or PORT)
        stream: VoiceTextStream | None = None
        try:
            stream = await asyncio.to_thread(VoiceTextStream, text, host, port)
            reply_text.value = f"秘書：{stream.reply_text or text}"
            page.update()
            parts: list[bytes] = []
            while True:
                pcm = await asyncio.to_thread(stream.next_pcm)
                if pcm is None:
                    break
                parts.append(pcm)
                set_voice_state("🔔 予定のお知らせ", text)
                await play_with_secretary_animation(pcm)
            if parts:
                last_reply_pcm = b"".join(parts)
                replay_button.disabled = False
                page.update()
        except Exception as exc:
            print(f"予定通知エラー: {type(exc).__name__}: {exc}")
        finally:
            if stream:
                stream.close()

    async def schedule_notification_loop() -> None:
        """音声で予約された通知だけを確認し、指定時刻になったら一度だけ読み上げる。"""
        while True:
            try:
                host = (schedule_host_field.value or SCHEDULE_HOST).strip()
                port = int(schedule_port_field.value or SCHEDULE_PORT)
                data = await asyncio.to_thread(fetch_reminders_json, host, port)
                now = datetime.now().astimezone()

                for reminder in data.get("reminders") or []:
                    if not isinstance(reminder, dict):
                        continue
                    reminder_id = str(reminder.get("reminder_id") or "")
                    notify_at_text = str(reminder.get("notify_at") or "")
                    plan_date = str(reminder.get("plan_date") or "")
                    start_text = str(reminder.get("start_time") or "")
                    if not reminder_id or not notify_at_text or not plan_date or not start_text:
                        continue
                    try:
                        notify_at = datetime.strptime(
                            notify_at_text, "%Y-%m-%dT%H:%M"
                        ).replace(tzinfo=now.tzinfo)
                        start_time = datetime.strptime(
                            f"{plan_date} {start_text}", "%Y-%m-%d %H:%M"
                        ).replace(tzinfo=now.tzinfo)
                    except ValueError:
                        continue

                    # 開始時刻を過ぎた古い通知は、遅れて読み上げず消化済みにする。
                    if now >= start_time:
                        await asyncio.to_thread(
                            mark_reminder_sent_json, host, port, reminder_id
                        )
                        continue

                    if now < notify_at:
                        continue

                    task = str(reminder.get("task") or "予定")
                    text = f"{start_time.hour}時{start_time.minute:02d}分から、{task}ですよ。"
                    if start_time.minute == 0:
                        text = f"{start_time.hour}時から、{task}ですよ。"
                    await speak_schedule_notification(text)
                    await asyncio.to_thread(
                        mark_reminder_sent_json, host, port, reminder_id
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"予定通知確認エラー: {type(exc).__name__}: {exc}")
            await asyncio.sleep(SCHEDULE_NOTIFY_POLL_SECONDS)

    # ------------------------------------------------------------
    # 音声会話
    # ------------------------------------------------------------
    async def conversation_loop() -> None:
        nonlocal running, last_reply_pcm
        stream: VoiceReplyStream | None = None
        try:
            while running:
                try:
                    result = await vad.record_utterance(audio, vad_status)
                except RuntimeError as exc:
                    # 予定通知などの再生が始まると、play_with_secretary_animation() が
                    # 意図的にマイクを停止する。その場合はエラー終了せず、
                    # 再生＋残響ガードが終わってからVAD待機をやり直す。
                    if playback_active.is_set():
                        while running and playback_active.is_set():
                            await asyncio.sleep(0.05)
                        if running:
                            continue
                        break
                    raise
                if not running:
                    break

                set_secretary(SECRETARY_NORMAL)
                set_voice_state(
                    "🧠 認識・応答を待っています…",
                    f"発話 {result.seconds:.2f} 秒 / {len(result.pcm)} bytes をAIコンピュータへ送信",
                )

                host = (host_field.value or AI_HOST).strip()
                port = int(port_field.value or PORT)

                stream = await asyncio.to_thread(VoiceReplyStream, result.pcm, host, port)
                recognized_text.value = f"あなた：{stream.recognized_text or '（認識結果なし）'}"
                reply_text.value = f"秘書：{stream.reply_text or '（返答なし）'}"
                page.update()

                # 音声側で作業完了が処理された場合も、サーバーの最新状態を
                # 再取得して当該チェックボックスへ即時反映する。
                if stream.recognized_text:
                    await load_schedule_from_server(show_success=False)

                if not stream.recognized_text:
                    stream.close()
                    stream = None
                    set_voice_state("…認識結果が空でした。もう一度どうぞ")
                    await asyncio.sleep(0.2)
                    continue

                reply_parts: list[bytes] = []
                part_no = 0
                while running:
                    reply_pcm = await asyncio.to_thread(stream.next_pcm)
                    if reply_pcm is None:
                        break

                    part_no += 1
                    reply_parts.append(reply_pcm)
                    reply_seconds = len(reply_pcm) / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)

                    # VOICEVOX再生中は normal / mouth_open を交互に切り替える。
                    set_voice_state(
                        "🔊 秘書が話しています…",
                        f"AI応答 文{part_no}: {reply_seconds:.2f} 秒 / {len(reply_pcm)} bytes",
                    )
                    await play_with_secretary_animation(reply_pcm)

                if stream:
                    stream.close()
                    stream = None

                if reply_parts:
                    last_reply_pcm = b"".join(reply_parts)
                    replay_button.disabled = False
                    page.update()

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            running = False
            set_secretary(SECRETARY_WARNING)
            set_voice_state(f"エラー: {type(exc).__name__}", str(exc))
        finally:
            if stream:
                stream.close()
            try:
                await audio.stop_capture()
            except Exception:
                pass
            running = False
            start_button.disabled = False
            stop_button.disabled = True
            settings_button.disabled = False
            page.update()

    async def start_conversation(e):
        nonlocal running, conversation_task
        if running:
            return

        allowed = await audio.ensure_record_permission()
        if not allowed:
            set_voice_state(
                "マイク許可が必要です",
                "Androidでは許可した後、もう一度［会話開始］を押してください。",
            )
            return

        running = True
        start_button.disabled = True
        stop_button.disabled = False
        settings_button.disabled = True
        set_secretary(SECRETARY_NORMAL)
        page.update()
        conversation_task = asyncio.create_task(conversation_loop())

    async def stop_conversation(e):
        nonlocal running, conversation_task
        running = False
        await audio.stop_capture()
        if conversation_task and not conversation_task.done():
            conversation_task.cancel()
        conversation_task = None
        set_secretary(SECRETARY_NORMAL)
        set_voice_state(
            "停止中",
            f"backend: {audio.name} / VAD: {vad.detector_name}",
        )
        start_button.disabled = False
        stop_button.disabled = True
        settings_button.disabled = False
        page.update()

    async def replay(e):
        if not last_reply_pcm:
            return
        set_voice_state("🔊 直前の返答を再生中…")
        await play_with_secretary_animation(last_reply_pcm)
        set_voice_state("🎤 話しかけてください")

    start_button.on_click = start_conversation
    stop_button.on_click = stop_conversation
    replay_button.on_click = replay

    # ------------------------------------------------------------
    # 画面レイアウト
    # ------------------------------------------------------------
    voice_panel = ft.Container(
        padding=8,
        border=ft.Border.all(1, ft.Colors.GREY_300),
        border_radius=8,
        content=ft.Column(
            spacing=4,
            controls=[
                ft.Row(
                    [
                        state_text,
                        start_button,
                        stop_button,
                        replay_button,
                        toggle_log_button,
                        settings_button,
                    ],
                    wrap=True,
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                recognized_text,
                reply_text,
                detail_text,
            ],
        ),
    )

    # リストを下層、秘書PNGを上層に置く。
    # 画像は操作を横取りしない。
    secretary_overlay = ft.Container(
        content=secretary_image,
        right=8,
        bottom=64,
        ignore_interactions=True,
    )

    main_stack = ft.Stack(
        expand=True,
        controls=[reorder_list, secretary_overlay],
    )

    page.add(
        voice_panel,
        ft.Row(
            [
                schedule_status,
                ft.Row([add_schedule_button, refresh_schedule_button], spacing=6),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        ft.Divider(height=8),
        main_stack,
    )

    # 起動時にホストから今日の時間割を取得する。
    await load_schedule_from_server(show_success=False)

    # 音声で予約された予定通知だけをバックグラウンドで確認する。
    asyncio.create_task(schedule_notification_loop())


if __name__ == "__main__":
    ft.run(main, assets_dir="assets")
