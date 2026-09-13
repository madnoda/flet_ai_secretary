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
ICレコーダ -> 時間割JSON -> SQLite登録 -> schedule_http_server 起動
を1コマンドで行うランチャー。

ローカルLLM（デフォルト）:
    python3 update_schedule_from_ic.py
    python3 update_schedule_from_ic.py --llm local

OpenAI API:
    export OPENAI_API_KEY="sk-proj-XXXX"
    python3 update_schedule_from_ic.py --llm openai

生成JSON名は ic_recorder_schedule.py の出力から自動取得するため、
record_YYMMDD_NNN_plan.json を手で指定する必要はない。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
IC_SCRIPT = ROOT / "ic_recorder" / "ic_recorder_schedule.py"
IMPORT_SCRIPT = ROOT / "schedule" / "json_to_schedule_sqlite.py"
SERVER_SCRIPT = ROOT / "schedule" / "schedule_http_server.py"
DEFAULT_PLAN_DIR = Path(os.getenv("DAILY_PLAN_DIR", str(Path.home() / "whisper_ic_recorder" / "daily_plan")))

PLAN_LINE_RE = re.compile(r"^\[PLAN\]\s+written:\s+(.+\.json)\s*$")


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ICレコーダから今日のスケジュールDB作成までを一括実行"
    )
    p.add_argument("--llm", choices=("local", "openai"), default="local",
                   help="時間割生成に使うLLM。既定: local")
    p.add_argument("--no-server", action="store_true",
                   help="SQLite登録後に schedule_http_server.py を起動しない")
    p.add_argument("--db", type=Path,
                   default=ROOT / "schedule" / "schedule_data" / "daily_schedule.db",
                   help="登録先SQLite DB")
    p.add_argument("--all-text", action="store_true",
                   help="ic_recorder_schedule.py に --all-text を渡す")
    p.add_argument("--input-text", type=Path, nargs="+",
                   help="ICレコーダを読まず、指定txtを処理（テスト/再生成用）")
    return p.parse_args()


def run_ic_schedule(args: argparse.Namespace) -> Path:
    if args.llm == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            '--llm openai には OPENAI_API_KEY が必要です。\n'
            '先に export OPENAI_API_KEY="sk-proj-XXXX" を実行してください。'
        )

    plan_dir = Path(os.getenv("DAILY_PLAN_DIR", str(DEFAULT_PLAN_DIR))).expanduser()
    plan_dir.mkdir(parents=True, exist_ok=True)

    # ログ取得に失敗した時のフォールバック用。
    before = {
        p.resolve(): p.stat().st_mtime_ns
        for p in plan_dir.glob("*.json")
        if p.is_file()
    }
    started_ns = time.time_ns()

    cmd = [sys.executable, str(IC_SCRIPT), "--llm", args.llm]
    if args.all_text:
        cmd.append("--all-text")
    if args.input_text:
        cmd.append("--input-text")
        cmd.extend(str(p.expanduser().resolve()) for p in args.input_text)

    print("=" * 64)
    print(f"[1/3] ICレコーダ -> 時間割生成 / LLM={args.llm}")
    print("=" * 64)
    print("$ " + " ".join(cmd), flush=True)

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT / "ic_recorder"),
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    json_candidates: list[Path] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        m = PLAN_LINE_RE.match(line.strip())
        if m:
            p = Path(m.group(1)).expanduser()
            if not p.is_absolute():
                p = (ROOT / "ic_recorder" / p).resolve()
            else:
                p = p.resolve()
            json_candidates.append(p)

    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ic_recorder_schedule.py が終了コード {rc} で失敗しました")

    # 最優先: 今回の実行ログが示した最後のJSON。
    for p in reversed(json_candidates):
        if p.exists() and p.suffix.lower() == ".json":
            print(f"[AUTO] 今回生成されたJSON: {p}")
            return p

    # フォールバック: 実行前後で新規/更新されたJSONのうち最新。
    changed: list[Path] = []
    for p in plan_dir.glob("*.json"):
        if not p.is_file():
            continue
        rp = p.resolve()
        mtime = p.stat().st_mtime_ns
        if rp not in before or mtime > before[rp] or mtime >= started_ns:
            changed.append(rp)
    if changed:
        p = max(changed, key=lambda x: x.stat().st_mtime_ns)
        print(f"[AUTO] 更新時刻からJSONを特定: {p}")
        return p

    raise RuntimeError(
        "今回生成された時間割JSONを特定できませんでした。"
        "新しいICレコーダ音声/文字起こしが無かった可能性があります。"
    )


def import_json(json_path: Path, db_path: Path) -> None:
    print()
    print("=" * 64)
    print("[2/3] JSON -> SQLite登録")
    print("=" * 64)
    cmd = [
        sys.executable,
        str(IMPORT_SCRIPT),
        str(json_path),
        "--db", str(db_path.expanduser().resolve()),
    ]
    print("$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT / "schedule"), check=True, env=os.environ.copy())


def start_schedule_server() -> int:
    print()
    print("=" * 64)
    print("[3/3] schedule_http_server 起動")
    print("=" * 64)

    if port_open("127.0.0.1", 8766):
        print("✓ schedule_http_server は既に起動しています (port 8766)")
        return 0

    cmd = [sys.executable, str(SERVER_SCRIPT)]
    print("$ " + " ".join(cmd), flush=True)
    print("Ctrl+C で schedule_http_server を終了します。")
    try:
        return subprocess.call(cmd, cwd=str(ROOT / "schedule"), env=os.environ.copy())
    except KeyboardInterrupt:
        print("\n終了します。")
        return 0


def main() -> int:
    args = parse_args()
    try:
        json_path = run_ic_schedule(args)
        import_json(json_path, args.db)
        if args.no_server:
            print("\n✓ 時間割生成とSQLite登録が完了しました。")
            return 0
        return start_schedule_server()
    except subprocess.CalledProcessError as ex:
        print(f"エラー: コマンドが失敗しました (code={ex.returncode})", file=sys.stderr)
        return ex.returncode or 1
    except Exception as ex:
        print(f"エラー: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
