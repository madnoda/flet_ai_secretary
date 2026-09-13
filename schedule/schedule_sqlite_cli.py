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
時間割SQLite 内容確認用CLI

json_to_schedule_sqlite.py が生成したDBを読むための確認ツール。

できること:
  - DBスキーマバージョン表示
  - 登録済み日付一覧
  - 最新日の時間割表示
  - 指定日の時間割表示
  - daily_plans / daily_plan_items の生データ表示
  - 指定日の raw_json 表示

この段階では「読み取り専用」です。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


DEFAULT_DB = Path("schedule_data/daily_schedule.db")


def open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"DBが見つかりません: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def print_schema_version(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT major, minor FROM schema_version LIMIT 1"
    ).fetchone()

    if row is None:
        print("schema_version: 未登録")
    else:
        print(f"schema_version: {row['major']}.{row['minor']}")


def list_plans(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT
            p.plan_id,
            p.plan_date,
            p.title,
            p.generated_at,
            p.imported_at,
            COUNT(i.item_id) AS item_count
        FROM daily_plans AS p
        LEFT JOIN daily_plan_items AS i
          ON i.plan_id = p.plan_id
        GROUP BY
            p.plan_id,
            p.plan_date,
            p.title,
            p.generated_at,
            p.imported_at
        ORDER BY p.plan_date DESC
        """
    ).fetchall()

    if not rows:
        print("登録済みの時間割はありません。")
        return

    print("登録済み時間割:")
    for row in rows:
        print(
            f"  {row['plan_date']}  "
            f"{row['item_count']:>2}件  "
            f"{row['title']}"
        )
        print(f"      plan_id: {row['plan_id']}")
        if row["generated_at"]:
            print(f"      AI生成: {row['generated_at']}")
        if row["imported_at"]:
            print(f"      DB登録: {row['imported_at']}")


def resolve_plan(
    conn: sqlite3.Connection,
    plan_date: str | None,
) -> sqlite3.Row | None:
    if plan_date:
        return conn.execute(
            """
            SELECT *
            FROM daily_plans
            WHERE plan_date = ?
            """,
            (plan_date,),
        ).fetchone()

    return conn.execute(
        """
        SELECT *
        FROM daily_plans
        ORDER BY plan_date DESC
        LIMIT 1
        """
    ).fetchone()


def show_plan(
    conn: sqlite3.Connection,
    plan_date: str | None,
    show_notes: bool = True,
) -> None:
    plan = resolve_plan(conn, plan_date)
    if plan is None:
        if plan_date:
            print(f"{plan_date} の時間割はありません。")
        else:
            print("時間割が登録されていません。")
        return

    print(f"日付: {plan['plan_date']}")
    print(f"題名: {plan['title']}")
    print(f"plan_id: {plan['plan_id']}")
    if plan["generated_at"]:
        print(f"AI生成: {plan['generated_at']}")
    if plan["imported_at"]:
        print(f"DB登録: {plan['imported_at']}")
    if plan["source_file"]:
        print(f"元データ: {plan['source_file']}")
    if plan["generator"]:
        print(f"生成器: {plan['generator']}")

    print()
    print("時間割:")

    rows = conn.execute(
        """
        SELECT
            sort_order,
            start_time,
            end_time,
            task,
            note,
            category,
            checked
        FROM daily_plan_items
        WHERE plan_id = ?
        ORDER BY sort_order
        """,
        (plan["plan_id"],),
    ).fetchall()

    if not rows:
        print("  （項目なし）")
        return

    for row in rows:
        mark = "✓" if row["checked"] else " "
        category = f" [{row['category']}]" if row["category"] else ""
        print(
            f"  [{mark}] "
            f"{row['start_time']}〜{row['end_time']}  "
            f"{row['task']}{category}"
        )
        if show_notes and row["note"]:
            print(f"      {row['note']}")


def show_table(conn: sqlite3.Connection, table_name: str) -> None:
    allowed = {"schema_version", "daily_plans", "daily_plan_items"}
    if table_name not in allowed:
        raise ValueError(
            f"表示できるテーブルは {', '.join(sorted(allowed))} です"
        )

    rows = conn.execute(
        f"SELECT * FROM {table_name}"
    ).fetchall()

    print(f"TABLE: {table_name}")

    if not rows:
        print("（0件）")
        return

    columns = rows[0].keys()
    for index, row in enumerate(rows, 1):
        print()
        print(f"--- row {index} ---")
        for col in columns:
            value = row[col]
            if col == "raw_json" and isinstance(value, str):
                print(f"{col}: <{len(value)} chars>")
            else:
                print(f"{col}: {value}")


def show_raw_json(
    conn: sqlite3.Connection,
    plan_date: str | None,
) -> None:
    plan = resolve_plan(conn, plan_date)
    if plan is None:
        if plan_date:
            print(f"{plan_date} の時間割はありません。")
        else:
            print("時間割が登録されていません。")
        return

    raw = plan["raw_json"]
    try:
        data = json.loads(raw)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        print(raw)


def print_summary(conn: sqlite3.Connection) -> None:
    print_schema_version(conn)

    plan_count = conn.execute(
        "SELECT COUNT(*) FROM daily_plans"
    ).fetchone()[0]

    item_count = conn.execute(
        "SELECT COUNT(*) FROM daily_plan_items"
    ).fetchone()[0]

    print(f"daily_plans: {plan_count} 件")
    print(f"daily_plan_items: {item_count} 件")

    latest = conn.execute(
        """
        SELECT plan_date, title
        FROM daily_plans
        ORDER BY plan_date DESC
        LIMIT 1
        """
    ).fetchone()

    if latest:
        print(
            f"最新: {latest['plan_date']} / {latest['title']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="時間割SQLiteの内容確認用CLI"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="SQLite DBファイル "
             "(default: schedule_data/daily_schedule.db)",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "summary",
        help="DB概要を表示",
    )

    sub.add_parser(
        "list",
        help="登録済み時間割の日付一覧",
    )

    show = sub.add_parser(
        "show",
        help="指定日または最新日の時間割を表示",
    )
    show.add_argument(
        "date",
        nargs="?",
        help="YYYY-MM-DD。省略すると最新日",
    )
    show.add_argument(
        "--no-notes",
        action="store_true",
        help="メモを表示しない",
    )

    table = sub.add_parser(
        "table",
        help="テーブルの生データを表示",
    )
    table.add_argument(
        "name",
        choices=[
            "schema_version",
            "daily_plans",
            "daily_plan_items",
        ],
    )

    raw = sub.add_parser(
        "raw-json",
        help="指定日または最新日の元JSONを表示",
    )
    raw.add_argument(
        "date",
        nargs="?",
        help="YYYY-MM-DD。省略すると最新日",
    )

    args = parser.parse_args()
    db_path = args.db.expanduser().resolve()

    conn = open_db(db_path)
    try:
        if args.command in (None, "summary"):
            print_summary(conn)

        elif args.command == "list":
            list_plans(conn)

        elif args.command == "show":
            show_plan(
                conn,
                args.date,
                show_notes=not args.no_notes,
            )

        elif args.command == "table":
            show_table(conn, args.name)

        elif args.command == "raw-json":
            show_raw_json(conn, args.date)

        else:
            parser.print_help()

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
