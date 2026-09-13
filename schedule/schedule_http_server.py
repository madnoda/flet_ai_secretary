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
時間割SQLite -> JSON HTTPサーバー

用途:
  Android/Fletクライアントから、ホスト側SQLiteに保存された
  「今日の時間割」を取得するための最小サーバー。

エンドポイント:
  GET /health
      -> サーバー生存確認

  GET /today
      -> 今日の日付の時間割

  GET /latest
      -> DB内で最も新しい plan_date の時間割

  GET /date/YYYY-MM-DD
      -> 指定日の時間割

  POST /plan/update
      -> クライアントで編集した時間割をSQLiteへ反映

  POST /item/add
      -> 新しい予定項目を追加

  POST /item/update
      -> 予定項目の時刻や内容を更新

  POST /item/complete
      -> 予定項目の完了/未完了と完了時刻を更新

返却JSON例:
{
  "ok": true,
  "plan": {
    "plan_id": "...",
    "plan_date": "2026-08-15",
    "title": "30分時間割 素案",
    "generated_at": "...",
    "imported_at": "...",
    "source_file": "...",
    "generator": "...",
    "items": [
      {
        "item_id": "...",
        "sort_order": 0,
        "start": "08:00",
        "end": "09:00",
        "task": "...",
        "note": "...",
        "category": "focus",
        "checked": false
      }
    ]
  }
}
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_DB = Path("schedule_data/daily_schedule.db")
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8766


def open_db_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"DBが見つかりません: {db_path}")
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn




def open_db_rw(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"DBが見つかりません: {db_path}")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_completion_schema(conn: sqlite3.Connection) -> None:
    """既存DBにも completed_at 列を安全に追加する。"""
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(daily_plan_items)").fetchall()
    }
    if "completed_at" not in columns:
        conn.execute("ALTER TABLE daily_plan_items ADD COLUMN completed_at TEXT")
        conn.commit()


def set_item_completion(conn: sqlite3.Connection, payload: dict) -> dict:
    """1項目の完了状態を更新し、完了時刻をサーバー時刻で記録する。"""
    plan_id = str(payload.get("plan_id") or "").strip()
    item_id = str(payload.get("item_id") or "").strip()
    completed = bool(payload.get("completed", True))
    if not plan_id or not item_id:
        raise ValueError("plan_id と item_id は必須です")

    row = conn.execute(
        """
        SELECT i.item_id, i.plan_id, i.task, i.checked, i.completed_at, p.plan_date
        FROM daily_plan_items AS i
        JOIN daily_plans AS p ON p.plan_id = i.plan_id
        WHERE i.plan_id = ? AND i.item_id = ?
        """,
        (plan_id, item_id),
    ).fetchone()
    if row is None:
        raise LookupError("指定された予定項目がDBにありません")

    now = now_iso()
    completed_at = (row["completed_at"] or now) if completed else None
    with conn:
        conn.execute(
            """
            UPDATE daily_plan_items
            SET checked = ?, completed_at = ?, updated_at = ?
            WHERE plan_id = ? AND item_id = ?
            """,
            (1 if completed else 0, completed_at, now, plan_id, item_id),
        )

    updated = conn.execute(
        """
        SELECT i.item_id, i.plan_id, i.task, i.checked, i.completed_at,
               i.updated_at, p.plan_date
        FROM daily_plan_items AS i
        JOIN daily_plans AS p ON p.plan_id = i.plan_id
        WHERE i.plan_id = ? AND i.item_id = ?
        """,
        (plan_id, item_id),
    ).fetchone()
    return dict(updated)




def _validate_hhmm(value: str, field_name: str) -> str:
    value = str(value or "").strip()
    try:
        hour_s, minute_s = value.split(":", 1)
        hour, minute = int(hour_s), int(minute_s)
    except Exception as exc:
        raise ValueError(f"{field_name} は HH:MM 形式で指定してください") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{field_name} は HH:MM 形式で指定してください")
    return f"{hour:02d}:{minute:02d}"


def update_item(conn: sqlite3.Connection, payload: dict) -> dict:
    """予定1項目の開始・終了時刻や本文を更新する。"""
    plan_id = str(payload.get("plan_id") or "").strip()
    item_id = str(payload.get("item_id") or "").strip()
    if not plan_id or not item_id:
        raise ValueError("plan_id と item_id は必須です")

    row = conn.execute(
        """
        SELECT i.*, p.plan_date
        FROM daily_plan_items AS i
        JOIN daily_plans AS p ON p.plan_id = i.plan_id
        WHERE i.plan_id = ? AND i.item_id = ?
        """,
        (plan_id, item_id),
    ).fetchone()
    if row is None:
        raise LookupError("指定された予定項目がDBにありません")

    start = _validate_hhmm(payload.get("start", row["start_time"]), "start")
    end = _validate_hhmm(payload.get("end", row["end_time"]), "end")
    if start == end:
        raise ValueError("開始時刻と終了時刻を同じにはできません")

    task = str(payload.get("task", row["task"]) or "").strip()
    if not task:
        raise ValueError("task は空にできません")
    note = str(payload.get("note", row["note"] or ""))
    category = str(payload.get("category", row["category"] or ""))
    now = now_iso()

    with conn:
        conn.execute(
            """
            UPDATE daily_plan_items
            SET start_time = ?, end_time = ?, task = ?, note = ?, category = ?, updated_at = ?
            WHERE plan_id = ? AND item_id = ?
            """,
            (start, end, task, note, category, now, plan_id, item_id),
        )

    updated = conn.execute(
        """
        SELECT item_id, plan_id, sort_order, start_time, end_time, task, note,
               category, source, checked, completed_at, created_at, updated_at
        FROM daily_plan_items
        WHERE plan_id = ? AND item_id = ?
        """,
        (plan_id, item_id),
    ).fetchone()
    result = dict(updated)
    result["start"] = result.pop("start_time")
    result["end"] = result.pop("end_time")
    result["checked"] = bool(result.get("checked"))
    result["completed_at"] = result.get("completed_at") or ""
    return result


def add_item(conn: sqlite3.Connection, payload: dict) -> dict:
    """今日の時間割へ予定項目を1件追加する。開始時刻に応じて sort_order を挿入する。"""
    plan_id = str(payload.get("plan_id") or "").strip()
    if not plan_id:
        raise ValueError("plan_id は必須です")

    plan = conn.execute(
        "SELECT plan_id, plan_date FROM daily_plans WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    if plan is None:
        raise LookupError("指定された plan_id がDBにありません")

    start = _validate_hhmm(payload.get("start"), "start")
    end = _validate_hhmm(payload.get("end"), "end")
    if start == end:
        raise ValueError("開始時刻と終了時刻を同じにはできません")

    task = str(payload.get("task") or "").strip()
    if not task:
        raise ValueError("task は必須です")
    note = str(payload.get("note") or "")
    category = str(payload.get("category") or "focus")
    source = str(payload.get("source") or "manual")

    rows = conn.execute(
        """
        SELECT sort_order, start_time
        FROM daily_plan_items
        WHERE plan_id = ?
        ORDER BY sort_order
        """,
        (plan_id,),
    ).fetchall()
    insert_order = len(rows)
    for row in rows:
        if str(row["start_time"]) > start:
            insert_order = int(row["sort_order"])
            break

    item_id = uuid.uuid4().hex
    now = now_iso()
    with conn:
        conn.execute(
            """
            UPDATE daily_plan_items
            SET sort_order = sort_order + 1, updated_at = ?
            WHERE plan_id = ? AND sort_order >= ?
            """,
            (now, plan_id, insert_order),
        )
        conn.execute(
            """
            INSERT INTO daily_plan_items(
                item_id, plan_id, sort_order, start_time, end_time, task, note,
                category, source, checked, completed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
            """,
            (item_id, plan_id, insert_order, start, end, task, note,
             category, source, now, now),
        )

    row = conn.execute(
        """
        SELECT item_id, plan_id, sort_order, start_time, end_time, task, note,
               category, source, checked, completed_at, created_at, updated_at
        FROM daily_plan_items
        WHERE item_id = ? AND plan_id = ?
        """,
        (item_id, plan_id),
    ).fetchone()
    result = dict(row)
    result["start"] = result.pop("start_time")
    result["end"] = result.pop("end_time")
    result["checked"] = bool(result.get("checked"))
    result["completed_at"] = result.get("completed_at") or ""
    return result

def update_plan(conn: sqlite3.Connection, payload: dict) -> dict:
    """クライアントから送られた1日分の最新状態をDBへ反映する。"""
    plan_id = str(payload.get("plan_id") or "")
    plan_date = str(payload.get("plan_date") or "")
    items = payload.get("items")

    if not plan_id:
        raise ValueError("plan_id がありません")
    if not plan_date:
        raise ValueError("plan_date がありません")
    try:
        date.fromisoformat(plan_date)
    except ValueError as exc:
        raise ValueError("plan_date は YYYY-MM-DD 形式で指定してください") from exc
    if not isinstance(items, list):
        raise ValueError("items は配列で指定してください")

    plan = conn.execute(
        "SELECT plan_id, plan_date FROM daily_plans WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    if plan is None:
        raise LookupError("指定された plan_id がDBにありません")
    if plan["plan_date"] != plan_date:
        raise ValueError("plan_id と plan_date が一致しません")

    db_items = conn.execute(
        "SELECT item_id FROM daily_plan_items WHERE plan_id = ?",
        (plan_id,),
    ).fetchall()
    expected_ids = {row["item_id"] for row in db_items}
    received_ids = {str(item.get("item_id") or "") for item in items if isinstance(item, dict)}

    if received_ids != expected_ids or len(items) != len(expected_ids):
        raise ValueError("items の item_id 一覧がDBと一致しません")

    now = now_iso()
    with conn:
        title = payload.get("title")
        if isinstance(title, str) and title.strip():
            conn.execute(
                "UPDATE daily_plans SET title = ? WHERE plan_id = ?",
                (title.strip(), plan_id),
            )

        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"items[{index}] がオブジェクトではありません")

            item_id = str(item.get("item_id") or "")
            start = str(item.get("start") or "").strip()
            end = str(item.get("end") or "").strip()
            task = str(item.get("task") or "").strip()
            note = str(item.get("note") or "")
            category = str(item.get("category") or "")
            checked = 1 if bool(item.get("checked", False)) else 0

            if not start or not end or not task:
                raise ValueError(f"items[{index}] の start/end/task は必須です")

            conn.execute(
                """
                UPDATE daily_plan_items
                SET sort_order = ?,
                    start_time = ?,
                    end_time = ?,
                    task = ?,
                    note = ?,
                    category = ?,
                    checked = ?,
                    completed_at = CASE
                        WHEN ? = 1 THEN COALESCE(completed_at, ?)
                        ELSE NULL
                    END,
                    updated_at = ?
                WHERE item_id = ? AND plan_id = ?
                """,
                (
                    index, start, end, task, note, category, checked,
                    checked, now, now, item_id, plan_id,
                ),
            )

    updated = fetch_plan_by_date(conn, plan_date)
    if updated is None:
        raise RuntimeError("更新後の時間割を取得できませんでした")
    return updated




def ensure_reminder_schema(conn: sqlite3.Connection) -> None:
    """通知予約テーブルを必要に応じて追加する。既存DBを壊さない簡易マイグレーション。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schedule_reminders (
            reminder_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            plan_date TEXT NOT NULL,
            notify_at TEXT NOT NULL,
            lead_minutes INTEGER NOT NULL DEFAULT 5,
            source_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            sent_at TEXT,
            UNIQUE(plan_id, item_id, notify_at),
            FOREIGN KEY(plan_id) REFERENCES daily_plans(plan_id) ON DELETE CASCADE,
            FOREIGN KEY(item_id) REFERENCES daily_plan_items(item_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_schedule_reminders_due
            ON schedule_reminders(plan_date, sent_at, notify_at);
        """
    )
    conn.commit()


def add_reminder(conn: sqlite3.Connection, payload: dict) -> dict:
    plan_id = str(payload.get("plan_id") or "").strip()
    item_id = str(payload.get("item_id") or "").strip()
    source_text = str(payload.get("source_text") or "").strip()
    try:
        lead_minutes = int(payload.get("lead_minutes", 5))
    except (TypeError, ValueError) as exc:
        raise ValueError("lead_minutes は整数で指定してください") from exc
    if not plan_id or not item_id:
        raise ValueError("plan_id と item_id は必須です")
    if not 0 <= lead_minutes <= 1440:
        raise ValueError("lead_minutes は 0〜1440 の範囲で指定してください")

    row = conn.execute(
        """
        SELECT p.plan_date, i.start_time, i.task
        FROM daily_plan_items AS i
        JOIN daily_plans AS p ON p.plan_id = i.plan_id
        WHERE i.plan_id = ? AND i.item_id = ?
        """,
        (plan_id, item_id),
    ).fetchone()
    if row is None:
        raise LookupError("指定された予定項目がDBにありません")

    start_dt = datetime.strptime(
        f"{row['plan_date']} {row['start_time']}", "%Y-%m-%d %H:%M"
    )
    notify_dt = start_dt - timedelta(minutes=lead_minutes)
    notify_at = notify_dt.strftime("%Y-%m-%dT%H:%M")
    reminder_id = uuid.uuid4().hex
    now = now_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO schedule_reminders(
                reminder_id, plan_id, item_id, plan_date, notify_at,
                lead_minutes, source_text, created_at, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(plan_id, item_id, notify_at) DO UPDATE SET
                lead_minutes = excluded.lead_minutes,
                source_text = excluded.source_text,
                created_at = excluded.created_at,
                sent_at = NULL
            """,
            (
                reminder_id, plan_id, item_id, row["plan_date"], notify_at,
                lead_minutes, source_text, now,
            ),
        )
    saved = conn.execute(
        """
        SELECT r.reminder_id, r.plan_id, r.item_id, r.plan_date, r.notify_at,
               r.lead_minutes, r.source_text, r.created_at, r.sent_at,
               i.start_time, i.end_time, i.task
        FROM schedule_reminders AS r
        JOIN daily_plan_items AS i ON i.item_id = r.item_id
        WHERE r.plan_id = ? AND r.item_id = ? AND r.notify_at = ?
        """,
        (plan_id, item_id, notify_at),
    ).fetchone()
    return dict(saved)


def fetch_today_reminders(conn: sqlite3.Connection) -> list[dict]:
    ensure_reminder_schema(conn)
    rows = conn.execute(
        """
        SELECT r.reminder_id, r.plan_id, r.item_id, r.plan_date, r.notify_at,
               r.lead_minutes, r.source_text, r.created_at, r.sent_at,
               i.start_time, i.end_time, i.task, i.checked
        FROM schedule_reminders AS r
        JOIN daily_plan_items AS i ON i.item_id = r.item_id AND i.plan_id = r.plan_id
        WHERE r.plan_date = ? AND r.sent_at IS NULL
        ORDER BY r.notify_at, i.sort_order
        """,
        (date.today().isoformat(),),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_reminder_sent(conn: sqlite3.Connection, payload: dict) -> dict:
    reminder_id = str(payload.get("reminder_id") or "").strip()
    if not reminder_id:
        raise ValueError("reminder_id は必須です")
    with conn:
        cur = conn.execute(
            "UPDATE schedule_reminders SET sent_at = ? WHERE reminder_id = ?",
            (now_iso(), reminder_id),
        )
    if cur.rowcount != 1:
        raise LookupError("指定された通知予約がありません")
    row = conn.execute(
        "SELECT * FROM schedule_reminders WHERE reminder_id = ?",
        (reminder_id,),
    ).fetchone()
    return dict(row)


def fetch_plan_by_date(
    conn: sqlite3.Connection,
    plan_date: str,
) -> dict | None:
    plan = conn.execute(
        """
        SELECT
            plan_id,
            plan_date,
            title,
            source_schema_version,
            generated_at,
            imported_at,
            source_file,
            generator
        FROM daily_plans
        WHERE plan_date = ?
        """,
        (plan_date,),
    ).fetchone()

    if plan is None:
        return None

    items = conn.execute(
        """
        SELECT
            item_id,
            sort_order,
            start_time,
            end_time,
            task,
            note,
            category,
            source,
            checked,
            completed_at,
            created_at,
            updated_at
        FROM daily_plan_items
        WHERE plan_id = ?
        ORDER BY sort_order
        """,
        (plan["plan_id"],),
    ).fetchall()

    return {
        "plan_id": plan["plan_id"],
        "plan_date": plan["plan_date"],
        "title": plan["title"],
        "source_schema_version": plan["source_schema_version"],
        "generated_at": plan["generated_at"],
        "imported_at": plan["imported_at"],
        "source_file": plan["source_file"],
        "generator": plan["generator"],
        "items": [
            {
                "item_id": row["item_id"],
                "sort_order": row["sort_order"],
                "start": row["start_time"],
                "end": row["end_time"],
                "task": row["task"],
                "note": row["note"] or "",
                "category": row["category"] or "",
                "source": row["source"] or "",
                "checked": bool(row["checked"]),
                "completed_at": row["completed_at"] or "",
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in items
        ],
    }


def fetch_latest_plan(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        """
        SELECT plan_date
        FROM daily_plans
        ORDER BY plan_date DESC
        LIMIT 1
        """
    ).fetchone()

    if row is None:
        return None

    return fetch_plan_by_date(conn, row["plan_date"])


class ScheduleRequestHandler(BaseHTTPRequestHandler):
    server_version = "ScheduleHTTP/0.3"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _get_conn(self) -> sqlite3.Connection:
        # 各リクエストごとに接続。
        # 単純で安全、かつLAN内の低頻度アクセスには十分。
        return open_db_ro(self.server.db_path)

    def _get_rw_conn(self) -> sqlite3.Connection:
        return open_db_rw(self.server.db_path)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length が不正です") from exc
        if length <= 0:
            raise ValueError("JSON本文がありません")
        if length > 1024 * 1024:
            raise ValueError("JSON本文が大きすぎます")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON本文はオブジェクトである必要があります")
        return data

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "schedule_http_server",
                    "version": "0.3",
                },
            )
            return

        conn = None
        try:
            conn = self._get_conn()

            if path == "/today":
                target_date = date.today().isoformat()
                plan = fetch_plan_by_date(conn, target_date)

                if plan is None:
                    self._send_json(
                        404,
                        {
                            "ok": False,
                            "error": "plan_not_found",
                            "plan_date": target_date,
                        },
                    )
                    return

                self._send_json(
                    200,
                    {
                        "ok": True,
                        "plan": plan,
                    },
                )
                return

            if path == "/reminders/today":
                conn.close()
                conn = self._get_rw_conn()
                reminders = fetch_today_reminders(conn)
                self._send_json(200, {"ok": True, "reminders": reminders})
                return

            if path == "/latest":
                plan = fetch_latest_plan(conn)

                if plan is None:
                    self._send_json(
                        404,
                        {
                            "ok": False,
                            "error": "plan_not_found",
                        },
                    )
                    return

                self._send_json(
                    200,
                    {
                        "ok": True,
                        "plan": plan,
                    },
                )
                return

            if path.startswith("/date/"):
                target_date = path[len("/date/"):]

                # 最小限の形式チェック
                try:
                    date.fromisoformat(target_date)
                except ValueError:
                    self._send_json(
                        400,
                        {
                            "ok": False,
                            "error": "invalid_date",
                            "message": "日付は YYYY-MM-DD 形式で指定してください",
                        },
                    )
                    return

                plan = fetch_plan_by_date(conn, target_date)

                if plan is None:
                    self._send_json(
                        404,
                        {
                            "ok": False,
                            "error": "plan_not_found",
                            "plan_date": target_date,
                        },
                    )
                    return

                self._send_json(
                    200,
                    {
                        "ok": True,
                        "plan": plan,
                    },
                )
                return

            self._send_json(
                404,
                {
                    "ok": False,
                    "error": "not_found",
                    "available": [
                        "/health",
                        "/today",
                        "/latest",
                        "/date/YYYY-MM-DD",
                        "POST /plan/update",
                        "POST /item/add",
                        "POST /item/update",
                        "POST /item/complete",
                        "/reminders/today",
                        "POST /reminder/add",
                        "POST /reminder/sent",
                    ],
                },
            )

        except FileNotFoundError as exc:
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": "database_not_found",
                    "message": str(exc),
                },
            )
        except Exception as exc:
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
            )
        finally:
            if conn is not None:
                conn.close()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path not in {"/plan/update", "/item/add", "/item/update", "/item/complete", "/reminder/add", "/reminder/sent"}:
            self._send_json(
                404,
                {
                    "ok": False,
                    "error": "not_found",
                    "available": ["/plan/update", "/item/add", "/item/update", "/item/complete", "/reminder/add", "/reminder/sent"],
                },
            )
            return

        conn = None
        try:
            payload = self._read_json_body()
            conn = self._get_rw_conn()
            ensure_completion_schema(conn)
            ensure_reminder_schema(conn)
            if path == "/plan/update":
                plan = update_plan(conn, payload)
                self._send_json(200, {"ok": True, "plan": plan})
            elif path == "/item/add":
                item = add_item(conn, payload)
                self._send_json(200, {"ok": True, "item": item})
            elif path == "/item/update":
                item = update_item(conn, payload)
                self._send_json(200, {"ok": True, "item": item})
            elif path == "/item/complete":
                item = set_item_completion(conn, payload)
                self._send_json(200, {"ok": True, "item": item})
            elif path == "/reminder/add":
                reminder = add_reminder(conn, payload)
                self._send_json(200, {"ok": True, "reminder": reminder})
            else:
                reminder = mark_reminder_sent(conn, payload)
                self._send_json(200, {"ok": True, "reminder": reminder})
        except LookupError as exc:
            self._send_json(404, {"ok": False, "error": "not_found", "message": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"ok": False, "error": "invalid_request", "message": str(exc)})
        except FileNotFoundError as exc:
            self._send_json(500, {"ok": False, "error": "database_not_found", "message": str(exc)})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": type(exc).__name__, "message": str(exc)})
        finally:
            if conn is not None:
                conn.close()

    def log_message(self, fmt: str, *args) -> None:
        print(
            f"[HTTP] {self.client_address[0]} "
            f"{self.command} {self.path} - "
            + (fmt % args)
        )


class ScheduleHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,
        RequestHandlerClass,
        db_path: Path,
    ):
        super().__init__(server_address, RequestHandlerClass)
        self.db_path = db_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="時間割SQLiteをJSONで返す小さなHTTPサーバー"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="SQLite DBファイル "
             "(default: schedule_data/daily_schedule.db)",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"bind host (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port (default: {DEFAULT_PORT})",
    )
    args = parser.parse_args()

    db_path = args.db.expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    # GET系でも completed_at を返せるよう、サーバー起動時に既存DBを移行する。
    migration_conn = open_db_rw(db_path)
    try:
        ensure_completion_schema(migration_conn)
        ensure_reminder_schema(migration_conn)
    finally:
        migration_conn.close()

    server = ScheduleHTTPServer(
        (args.host, args.port),
        ScheduleRequestHandler,
        db_path,
    )

    print("=== Schedule JSON HTTP Server ===")
    print(f"DB: {db_path}")
    print(f"Listen: http://{args.host}:{args.port}")
    print()
    print("Endpoints:")
    print("  GET /health")
    print("  GET /today")
    print("  GET /latest")
    print("  GET /date/YYYY-MM-DD")
    print("  POST /plan/update")
    print("  POST /item/add")
    print("  POST /item/update")
    print("  POST /item/complete")
    print()
    print("終了: Ctrl+C")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止します")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
