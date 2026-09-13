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

"""
AI秘書サーバー群を1コマンドで起動するランチャー。

  python3 start_ai_secretary.py
  python3 start_ai_secretary.py --llm local
      -> ローカルLLM（既定）

  python3 start_ai_secretary.py --llm openai
      -> OpenAI API（OPENAI_API_KEY が必要）

Ctrl+C で、このランチャーが起動した子プロセスをまとめて終了する。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import tomllib

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("AI_SECRETARY_CONFIG", str(ROOT / "config.toml"))).expanduser()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


CONFIG = load_config()
PATHS_CONFIG = CONFIG.get("paths", {})
SERVICES_CONFIG = CONFIG.get("services", {})

DEFAULT_LLAMA_SERVER = Path(PATHS_CONFIG.get(
    "llama_server", Path.home() / "llm/llama.cpp/build/bin/llama-server"
)).expanduser()
DEFAULT_LLAMA_MODEL = Path(PATHS_CONFIG.get(
    "llama_model", Path.home() / "llm/gpt-oss-20b-Q5_K_M.gguf"
)).expanduser()
DEFAULT_VOICEVOX_RUN = Path(PATHS_CONFIG.get(
    "voicevox_run", Path.home() / "voicevox/run"
)).expanduser()
VOICEVOX_URL = str(SERVICES_CONFIG.get("voicevox_url", "http://127.0.0.1:50021")).rstrip("/")
LOCAL_LLM_BASE_URL = str(SERVICES_CONFIG.get("local_llm_base_url", "http://127.0.0.1:8080/v1")).rstrip("/")
SCHEDULE_URL = str(SERVICES_CONFIG.get("schedule_url", "http://127.0.0.1:8766/today"))

children: list[tuple[str, subprocess.Popen]] = []
stop_event = threading.Event()


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_ok(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def wait_until(label: str, check, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            print(f"✓ {label} 起動確認")
            return True
        # 起動途中で子プロセスが落ちた場合は早めに知らせる
        for name, proc in children:
            if proc.poll() is not None and name == label:
                print(f"✗ {label} が終了しました (code={proc.returncode})", file=sys.stderr)
                return False
        time.sleep(0.5)
    print(f"✗ {label} の起動確認がタイムアウトしました", file=sys.stderr)
    return False


def relay_output(name: str, proc: subprocess.Popen) -> None:
    if proc.stdout is None:
        return
    try:
        for line in proc.stdout:
            print(f"[{name}] {line}", end="", flush=True)
    except Exception:
        pass


def start_process(name: str, cmd: list[str], cwd: Path | None = None,
                  env: dict[str, str] | None = None) -> subprocess.Popen:
    print(f"▶ {name} 起動")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    children.append((name, proc))
    threading.Thread(target=relay_output, args=(name, proc), daemon=True).start()
    return proc


def terminate_children() -> None:
    stop_event.set()
    # 逆順に停止
    for name, proc in reversed(children):
        if proc.poll() is None:
            print(f"■ {name} 停止中...")
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                proc.terminate()

    deadline = time.monotonic() + 8
    for _, proc in reversed(children):
        if proc.poll() is None:
            remain = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=remain)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI秘書サーバー群をまとめて起動")
    p.add_argument("--llm", choices=("local", "openai"), default="local",
                   help="会話に使うLLM。既定: local")
    p.add_argument("--no-voicevox", action="store_true",
                   help="VOICEVOXは既に別途起動しているものとして起動しない")
    p.add_argument("--llama-server", default=str(DEFAULT_LLAMA_SERVER))
    p.add_argument("--llama-model", default=str(DEFAULT_LLAMA_MODEL))
    p.add_argument("--voicevox-run", default=str(DEFAULT_VOICEVOX_RUN))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    env = os.environ.copy()
    # 子プロセスも同じ設定を使えるよう、既存コードが読む環境変数へ橋渡しする。
    env.setdefault("VOICEVOX_HOST", VOICEVOX_URL)
    env.setdefault("LOCAL_LLM_BASE_URL", LOCAL_LLM_BASE_URL)
    env.setdefault("LLM_BASE_URL", LOCAL_LLM_BASE_URL)
    env.setdefault("SCHEDULE_URL", SCHEDULE_URL)

    if args.llm == "openai" and not env.get("OPENAI_API_KEY"):
        print(
            'エラー: --llm openai には OPENAI_API_KEY が必要です。\n'
            '先に export OPENAI_API_KEY="sk-proj-XXXX" を実行してください。',
            file=sys.stderr,
        )
        return 2

    print("=" * 60)
    print(f"AI秘書 一括起動 / LLM={args.llm}")
    print("=" * 60)

    # VOICEVOX
    if http_ok(f"{VOICEVOX_URL}/version"):
        print("✓ VOICEVOX は既に起動しています")
    elif args.no_voicevox:
        print("・VOICEVOX の自動起動を省略")
    else:
        vv = Path(args.voicevox_run).expanduser()
        if not vv.exists():
            print(f"エラー: VOICEVOX起動ファイルがありません: {vv}", file=sys.stderr)
            return 2
        start_process("VOICEVOX", [str(vv)], cwd=vv.parent, env=env)
        if not wait_until("VOICEVOX",
                          lambda: http_ok(f"{VOICEVOX_URL}/version"), 60):
            terminate_children()
            return 1

    # ローカルLLM
    if args.llm == "local":
        if http_ok(f"{LOCAL_LLM_BASE_URL.removesuffix('/v1')}/health"):
            print("✓ llama-server は既に起動しています")
        else:
            llama = Path(args.llama_server).expanduser()
            model = Path(args.llama_model).expanduser()
            if not llama.exists():
                print(f"エラー: llama-server がありません: {llama}", file=sys.stderr)
                terminate_children()
                return 2
            if not model.exists():
                print(f"エラー: GGUFモデルがありません: {model}", file=sys.stderr)
                terminate_children()
                return 2

            start_process(
                "llama-server",
                [
                    str(llama),
                    "-m", str(model),
                    "--jinja",
                    "--reasoning-format", "auto",
                    "--ctx-size", "24576",
                    "--temp", "1.0",
                    "--top-p", "1.0",
                    "-ngl", "10",
                    "--host", "0.0.0.0",
                    "--port", "8080",
                    "--timeout", "3600",
                ],
                cwd=llama.parent,
                env=env,
            )
            if not wait_until("llama-server",
                              lambda: http_ok(f"{LOCAL_LLM_BASE_URL.removesuffix('/v1')}/health"), 180):
                terminate_children()
                return 1

    # 時間割HTTPサーバー
    if port_open("127.0.0.1", 8766):
        print("✓ schedule server は既に起動しています")
    else:
        start_process(
            "schedule",
            [sys.executable, str(ROOT / "schedule" / "schedule_http_server.py")],
            cwd=ROOT / "schedule",
            env=env,
        )
        if not wait_until("schedule",
                          lambda: port_open("127.0.0.1", 8766), 20):
            terminate_children()
            return 1

    # 音声会話サーバー
    if port_open("127.0.0.1", 50000):
        print("エラー: 音声PORT 50000 は既に使用されています。", file=sys.stderr)
        terminate_children()
        return 1

    start_process(
        "voice",
        [
            sys.executable,
            str(ROOT / "ai_host" / "voice_roundtrip_server_secretary.py"),
            "--llm", args.llm,
        ],
        cwd=ROOT / "ai_host",
        env=env,
    )
    if not wait_until("voice", lambda: port_open("127.0.0.1", 50000), 30):
        terminate_children()
        return 1

    print()
    print("✓ AI秘書のサーバー群が起動しました。Ctrl+C でまとめて終了します。")
    print("  Androidクライアントから接続できます。")
    print()

    try:
        while True:
            time.sleep(1)
            for name, proc in children:
                rc = proc.poll()
                if rc is not None:
                    print(f"\n✗ {name} が終了しました (code={rc})", file=sys.stderr)
                    return 1
    except KeyboardInterrupt:
        print("\n終了します。")
        return 0
    finally:
        terminate_children()


if __name__ == "__main__":
    raise SystemExit(main())
