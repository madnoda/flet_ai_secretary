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
V7時間割JSON -> ホスト側SQLite 変換

想定入力:
  ic_recorder_schedule_improved_v7.py が生成した JSON

主な保存先:
  daily_plans
  daily_plan_items

方針:
- plan_date ごとに1つの「その日の計画」を保存する。
- 同じ plan_date を再投入した場合は、その日の計画を置き換える。
- item_id / plan_id は UUID4 を使用する。
- 元JSONの start/end/task/note/category をなるべくそのまま保持する。
- JSON自体も raw_json として daily_plans に保存しておく。
  （将来スキーマを拡張したくなった時の保険）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


DB_SCHEMA_MAJOR = 0
DB_SCHEMA_MINOR = 2


def utcish_now() -> str:
    """ローカル時刻をタイムゾーン付きISO8601で返す。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            major INTEGER NOT NULL,
            minor INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_plans (
            plan_id TEXT PRIMARY KEY,
            plan_date TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            source_schema_version TEXT,
            generated_at TEXT,
            imported_at TEXT NOT NULL,
            source_file TEXT,
            generator TEXT,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_plan_items (
            item_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            task TEXT NOT NULL,
            note TEXT,
            category TEXT,
            source TEXT,
            checked INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (plan_id)
                REFERENCES daily_plans(plan_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_daily_plan_items_plan
            ON daily_plan_items(plan_id, sort_order);

        CREATE INDEX IF NOT EXISTS idx_daily_plan_items_start
            ON daily_plan_items(plan_id, start_time);
        """
    )

    row = conn.execute("SELECT major, minor FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version(major, minor) VALUES (?, ?)",
            (DB_SCHEMA_MAJOR, DB_SCHEMA_MINOR),
        )
    elif row["major"] != DB_SCHEMA_MAJOR:
        raise RuntimeError(
            f"DB major version mismatch: "
            f"DB={row['major']}.{row['minor']} / "
            f"program={DB_SCHEMA_MAJOR}.{DB_SCHEMA_MINOR}"
        )
    elif row["minor"] < DB_SCHEMA_MINOR:
        # 0.1 -> 0.2: 作業完了時刻を保存する列を追加。
        columns = {
            col["name"]
            for col in conn.execute("PRAGMA table_info(daily_plan_items)").fetchall()
        }
        if "completed_at" not in columns:
            conn.execute("ALTER TABLE daily_plan_items ADD COLUMN completed_at TEXT")
        conn.execute(
            "UPDATE schema_version SET minor = ?",
            (DB_SCHEMA_MINOR,),
        )


def load_plan_json(json_path: Path) -> dict[str, Any]:
    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    required = ("plan_date", "title", "slots")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"必須キーがありません: {', '.join(missing)}")

    if not isinstance(data["slots"], list):
        raise ValueError("'slots' は配列である必要があります")

    if not data["plan_date"]:
        raise ValueError("'plan_date' が空です")

    return data


def validate_slot(slot: dict[str, Any], index: int) -> None:
    for key in ("start", "end", "task"):
        if not slot.get(key):
            raise ValueError(f"slots[{index}] の '{key}' が空です")


def import_plan(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    source_path: Path,
) -> tuple[str, int, bool]:
    plan_date = str(data["plan_date"])
    title = str(data.get("title") or "1日の時間割")
    slots = data["slots"]

    for i, slot in enumerate(slots):
        if not isinstance(slot, dict):
            raise ValueError(f"slots[{i}] がオブジェクトではありません")
        validate_slot(slot, i)

    old = conn.execute(
        "SELECT plan_id FROM daily_plans WHERE plan_date = ?",
        (plan_date,),
    ).fetchone()

    replaced = old is not None

    # 同じ日の再生成を扱いやすくするため、現在は「その日の計画を丸ごと置換」。
    # 将来Android側の実績を保存する段階では、計画と実績を別テーブルに分離する。
    if old:
        conn.execute(
            "DELETE FROM daily_plans WHERE plan_date = ?",
            (plan_date,),
        )

    plan_id = uuid.uuid4().hex
    imported_at = utcish_now()

    source = data.get("source") or {}
    if not isinstance(source, dict):
        source = {}

    source_file = source_path.name
    transcripts = source.get("transcripts")
    if isinstance(transcripts, list) and transcripts:
        source_file = ", ".join(str(x) for x in transcripts)

    raw_json = json.dumps(data, ensure_ascii=False, indent=2)

    conn.execute(
        """
        INSERT INTO daily_plans (
            plan_id,
            plan_date,
            title,
            source_schema_version,
            generated_at,
            imported_at,
            source_file,
            generator,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            plan_date,
            title,
            str(data.get("schema_version") or ""),
            str(data.get("generated_at") or ""),
            imported_at,
            source_file,
            str(source.get("generator") or ""),
            raw_json,
        ),
    )

    for sort_order, slot in enumerate(slots):
        item_id = uuid.uuid4().hex
        now = utcish_now()

        conn.execute(
            """
            INSERT INTO daily_plan_items (
                item_id,
                plan_id,
                sort_order,
                start_time,
                end_time,
                task,
                note,
                category,
                source,
                checked,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                item_id,
                plan_id,
                sort_order,
                str(slot["start"]),
                str(slot["end"]),
                str(slot["task"]),
                str(slot.get("note") or ""),
                str(slot.get("category") or ""),
                str(slot.get("source") or ""),
                now,
                now,
            ),
        )

    return plan_id, len(slots), replaced


def print_plan(conn: sqlite3.Connection, plan_id: str) -> None:
    plan = conn.execute(
        """
        SELECT plan_date, title, generated_at, imported_at
        FROM daily_plans
        WHERE plan_id = ?
        """,
        (plan_id,),
    ).fetchone()

    if plan is None:
        return

    print()
    print(f"日付: {plan['plan_date']}")
    print(f"題名: {plan['title']}")
    if plan["generated_at"]:
        print(f"AI生成: {plan['generated_at']}")
    print(f"DB登録: {plan['imported_at']}")
    print()
    print("時間割:")

    rows = conn.execute(
        """
        SELECT sort_order, start_time, end_time, task, note, category
        FROM daily_plan_items
        WHERE plan_id = ?
        ORDER BY sort_order
        """,
        (plan_id,),
    ).fetchall()

    for row in rows:
        text = f"  {row['start_time']}〜{row['end_time']}  {row['task']}"
        if row["category"]:
            text += f"  [{row['category']}]"
        print(text)
        if row["note"]:
            print(f"      {row['note']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="V7で生成した時間割JSONをSQLiteへ保存します。"
    )
    parser.add_argument("json_file", type=Path, help="入力する時間割JSON")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("schedule_data/daily_schedule.db"),
        help="SQLite DBファイル (default: schedule_data/daily_schedule.db)",
    )
    parser.add_argument(
        "--no-print",
        action="store_true",
        help="登録後の時間割一覧を表示しない",
    )
    args = parser.parse_args()

    json_path = args.json_file.expanduser().resolve()
    db_path = args.db.expanduser().resolve()

    if not json_path.exists():
        raise FileNotFoundError(json_path)

    data = load_plan_json(json_path)

    conn = open_db(db_path)
    try:
        init_db(conn)
        with conn:
            plan_id, count, replaced = import_plan(conn, data, json_path)

        action = "置換" if replaced else "新規登録"
        print(f"[OK] {action}: {data['plan_date']} / {count} 件")
        print(f"[DB] {db_path}")
        print(f"[PLAN_ID] {plan_id}")

        if not args.no_print:
            print_plan(conn, plan_id)

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
