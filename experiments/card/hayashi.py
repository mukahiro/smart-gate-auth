#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AuthApp (カード認証) - Raspberry Pi 5 + PaSoRi RC-S300 + pyscard

現時点の機能:
  1. 学生証(FeliCa)のUIDを読み取る
  2. 新規カード登録(学籍番号・氏名とUIDを紐付けてSQLiteに保存)
  3. 認証(登録済みUIDと照合し、入室/退出を自動判定してログに記録)
  4. バックエンドAPI(Hono)へ入退室ログをJSON送信(X-API-Keyヘッダー対応、失敗時リトライ)

未実装(今後追加予定):
  - LCD・ブザーでのフィードバック -> notify() が差し込み場所
  - 顔認証との統合
  - APIのエンドポイント/フィールド名が確定したら payload のキー名を調整

事前準備(追加分):
  pip install requests --break-system-packages
  環境変数で送信先を指定できます(未設定時はデフォルト値):
    export AUTH_API_BASE_URL="http://localhost:3000"
    export AUTH_API_LOG_ENDPOINT="/api/logs"
    export AUTH_API_KEY="任意の共有シークレット"

DB: card_auth.db (SQLite)
  students テーブル: uid, student_number, name, registered_at
  logs テーブル: id, student_number, uid, action(enter/exit), timestamp
  status テーブル: student_number, current_status(in/out)  ※直近の状態保持
"""

import os
import sqlite3
import sys
from datetime import datetime

import requests
from smartcard.System import readers
from smartcard.util import toHexString
from smartcard.CardType import AnyCardType
from smartcard.CardRequest import CardRequest

GET_UID = [0xFF, 0xCA, 0x00, 0x00, 0x00]
DB_PATH = "card_auth.db"

# ==== バックエンドAPI設定 ====
# 環境変数から読み込む(未設定時はデフォルト値を使用)。
# 決定後は環境変数で上書きするか、下のデフォルト値を直接書き換えてください。
API_BASE_URL = os.environ.get("AUTH_API_BASE_URL", "http://localhost:3000")
API_LOG_ENDPOINT = os.environ.get("AUTH_API_LOG_ENDPOINT", "/api/logs")
API_KEY = os.environ.get("AUTH_API_KEY", "")  # 未設定なら送信ヘッダーに含めない
API_TIMEOUT_SEC = 3
API_MAX_RETRIES = 2


# ---------- リーダー ----------

def pick_reader():
    available = readers()
    if not available:
        raise RuntimeError("No PC/SC readers found. Check driver and USB connection.")
    print("Readers:")
    for i, r in enumerate(available):
        print(f"  [{i}] {r}")
    return available[0]


def read_uid(connection):
    data, sw1, sw2 = connection.transmit(GET_UID)
    if (sw1, sw2) == (0x90, 0x00) and data:
        return toHexString(data).replace(" ", ""), None
    return None, (sw1, sw2)


# ---------- DB ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            uid TEXT PRIMARY KEY,
            student_number TEXT NOT NULL,
            name TEXT NOT NULL,
            registered_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_number TEXT NOT NULL,
            uid TEXT NOT NULL,
            action TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS status (
            student_number TEXT PRIMARY KEY,
            current_status TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def find_student_by_uid(conn, uid):
    cur = conn.cursor()
    cur.execute("SELECT uid, student_number, name FROM students WHERE uid = ?", (uid,))
    row = cur.fetchone()
    if row:
        return {"uid": row[0], "student_number": row[1], "name": row[2]}
    return None


def register_student(conn, uid):
    print(f"未登録のカードです。(UID={uid})")
    student_number = input("学籍番号を入力してください: ").strip()
    name = input("氏名を入力してください: ").strip()
    if not student_number or not name:
        print("登録をキャンセルしました。")
        return None
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO students (uid, student_number, name, registered_at) VALUES (?, ?, ?, ?)",
            (uid, student_number, name, datetime.now().isoformat(timespec="seconds")),
        )
        cur.execute(
            "INSERT OR REPLACE INTO status (student_number, current_status) VALUES (?, 'out')",
            (student_number,),
        )
        conn.commit()
        print(f"登録完了: {name} ({student_number})")
        return {"uid": uid, "student_number": student_number, "name": name}
    except sqlite3.IntegrityError as e:
        print(f"登録エラー: {e}")
        return None


def toggle_status(conn, student_number):
    """in/outを切り替えて新しい状態を返す"""
    cur = conn.cursor()
    cur.execute("SELECT current_status FROM status WHERE student_number = ?", (student_number,))
    row = cur.fetchone()
    current = row[0] if row else "out"
    new_status = "in" if current == "out" else "out"
    cur.execute(
        "INSERT OR REPLACE INTO status (student_number, current_status) VALUES (?, ?)",
        (student_number, new_status),
    )
    conn.commit()
    return new_status


def log_event(conn, student_number, uid, action):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO logs (student_number, uid, action, timestamp) VALUES (?, ?, ?, ?)",
        (student_number, uid, action, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


# ---------- 今後つなぎ込む差し込み口(スタブ) ----------

def send_to_api(event: dict):
    """
    バックエンドAPI(Hono)へJSON送信する。
    event 例: {"student_number": "20240001", "name": "山田太郎",
               "action": "enter", "timestamp": "..."}

    フィールド名がバックエンド側と決まったら、下のpayloadの
    キー名(studentNumber等)を合わせてください。今は暫定でそのまま渡しています。
    """
    url = f"{API_BASE_URL}{API_LOG_ENDPOINT}"
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY

    payload = {
        "studentNumber": event["student_number"],
        "name": event["name"],
        "action": event["action"],
        "timestamp": event["timestamp"],
    }

    last_error = None
    for attempt in range(1, API_MAX_RETRIES + 2):  # 初回 + リトライ
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=API_TIMEOUT_SEC)
            if resp.status_code in (200, 201):
                print(f"[API送信成功] {payload}")
                return True
            else:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.exceptions.RequestException as e:
            last_error = str(e)

        print(f"[API送信失敗] 試行{attempt}回目: {last_error}")

    # 送信に失敗してもカード認証自体の処理は止めない。
    # 失敗分は後で再送できるよう、ローカルログには既に記録済み(handle_card内)。
    print("[API送信] 最終的に失敗しました。ログはローカルDBに保存されています。")
    return False


def notify(message: str, is_error: bool = False):
    """
    LCD・ブザーへのフィードバック処理。今はコンソール出力のみ。
    """
    prefix = "[!]" if is_error else "[OK]"
    print(f"{prefix} {message}")


# ---------- メイン ----------

def handle_card(conn, uid):
    student = find_student_by_uid(conn, uid)

    if student is None:
        register_student(conn, uid)
        return

    action = toggle_status(conn, student["student_number"])
    action_jp = "入室" if action == "in" else "退出"
    log_event(conn, student["student_number"], uid, action)

    event = {
        "student_number": student["student_number"],
        "name": student["name"],
        "action": action,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }

    notify(f"{student['name']}さん {action_jp}しました")
    send_to_api(event)


def main():
    conn = init_db()
    reader = pick_reader()
    print(f"Using reader: {reader}")
    print("学生証をかざしてください。(Ctrl+Cで終了)")

    try:
        while True:
            card_request = CardRequest(timeout=None, cardType=AnyCardType(), newcardonly=True)
            card_service = card_request.waitforcard()
            connection = card_service.connection
            connection.connect()

            uid, status = read_uid(connection)
            if uid:
                handle_card(conn, uid)
            else:
                sw1, sw2 = status
                notify(f"GET UID failed: SW1=0x{sw1:02X} SW2=0x{sw2:02X}", is_error=True)

            connection.disconnect()
            print("-" * 40)
    except KeyboardInterrupt:
        print("\n終了しました。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
