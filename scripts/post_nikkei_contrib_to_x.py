"""
日経225寄与度ランキングを毎日大引け後にXに投稿するスクリプト。
moo-stock-blog.com の寄与度ページをPlaywrightで開き、
ベスト4・ワースト4を1ツイートで投稿。X の280文字制限に応じて自動で
4位→3位、騰落率なしへとフォールバック。
ページ上の「最終更新 HH:MM」表示も拾ってツイート文に入れる。
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone, timedelta

import tweepy
from playwright.sync_api import sync_playwright

JST = timezone(timedelta(hours=9))
PAGE_URL = "https://moo-stock-blog.com/%e6%97%a5%e7%b5%8c225%e5%af%84%e4%b8%8e%e5%ba%a6/"
TWEET_LIMIT = 280  # X の文字数上限（重み付き）

# ページ側で既に短縮されてるが、念のため辞書（ADR版から流用）
NAME_SHORT = {
    "ファーストリテイリング": "ファストリ",
    "東京エレクトロン": "東エレク",
    "ソフトバンクグループ": "ソフトバンク",
    "三菱UFJフィナンシャル・グループ": "三菱UFJ",
    "三井住友フィナンシャルグループ": "三井住友FG",
    "信越化学工業": "信越化学",
    "ダイキン工業": "ダイキン",
    "アドバンテスト": "アドバンテ",
    "リクルートホールディングス": "リクルート",
}


def parse_yen(text: str) -> float:
    m = re.search(r"([+-])?\s*¥?\s*([0-9][0-9,]*\.?[0-9]*)", text.replace(",", ""))
    if not m:
        return 0.0
    sign, num = m.groups()
    val = float(num)
    return -val if sign == "-" else val


def weighted_len(s: str) -> int:
    """X の文字カウント概算（ASCII=1, それ以外=2）。安全側に多めに数える。"""
    return sum(1 if ord(c) < 0x80 else 2 for c in s)


def extract_rankings(page) -> dict:
    page.wait_for_function(
        "document.querySelectorAll('.imp-rank-col.up .imp-rank-row').length >= 4 "
        "&& document.querySelectorAll('.imp-rank-col.down .imp-rank-row').length >= 4",
        timeout=45000,
    )

    def grab(side_class: str) -> list[dict]:
        rows = page.locator(f".imp-rank-col.{side_class} .imp-rank-row")
        items = []
        for i in range(rows.count()):
            row = rows.nth(i)
            code = (row.locator(".code").text_content() or "").strip()
            nm_full = (row.locator(".nm").text_content() or "").strip()
            name = nm_full.replace(code, "").strip()
            yen_txt = (row.locator(".yn").text_content() or "").strip()
            try:
                pct_txt = (row.locator(".pc").text_content() or "").strip()
            except Exception:
                pct_txt = ""
            items.append({
                "rank": (row.locator(".rk").text_content() or "").strip(),
                "name": name,
                "code": code,
                "yen": parse_yen(yen_txt),
                "yen_text": yen_txt,
                "pct_text": pct_txt,
            })
        return items

    best = grab("up")
    worst = grab("down")
    if len(best) < 4 or len(worst) < 4:
        raise RuntimeError(f"ランキング取得不足 best={len(best)} worst={len(worst)}")

    # ページ上の「最終更新 HH:MM」表示を取得
    update_time = ""
    try:
        raw = (page.locator(".imp-ranks-time[data-update-time]")
               .text_content(timeout=3000) or "").strip()
        m = re.search(r"(\d{1,2}:\d{2})", raw)
        if m:
            update_time = m.group(1)
    except Exception:
        pass

    return {"best": best, "worst": worst, "update_time": update_time}


def short_name(name: str) -> str:
    return NAME_SHORT.get(name, name)


def _build(data: dict, top_n: int, include_pct: bool) -> str:
    now = datetime.now(JST)
    date_str = f"{now.month}/{now.day}"
    ut = data.get("update_time", "")
    suffix = f" / 更新 {ut}" if ut else ""
    lines = [f"📊 日経225 寄与度ランキング ({date_str}{suffix})", ""]

    def fmt(s):
        nm = short_name(s["name"])
        if include_pct and s["pct_text"]:
            return f"{nm} {s['yen_text']} ({s['pct_text']})"
        return f"{nm} {s['yen_text']}"

    lines.append(f"🟢 ベスト{top_n}（押し上げ）")
    for i, s in enumerate(data["best"][:top_n], 1):
        lines.append(f"{i}. {fmt(s)}")
    lines.append("")
    lines.append(f"🔴 ワースト{top_n}（押し下げ）")
    for i, s in enumerate(data["worst"][:top_n], 1):
        lines.append(f"{i}. {fmt(s)}")
    lines.append("")
    lines.append("#日経平均 #日経225 #寄与度")
    return "\n".join(lines)


def build_tweet(data: dict) -> str:
    """ユーザ希望: 4位優先、入らなければ3位までで妥協。さらに入らなければ騰落率削除。"""
    candidates = [
        (4, True),   # 4位+騰落率 ← 第一希望
        (4, False),  # 4位のみ
        (3, True),   # 3位+騰落率 ← 第二希望
        (3, False),  # 3位のみ ← 最終フォールバック
    ]
    for top_n, pct in candidates:
        t = _build(data, top_n, pct)
        if weighted_len(t) <= TWEET_LIMIT:
            print(f"[info] 採用: top_n={top_n} pct={pct} len={weighted_len(t)}")
            return t
    # ここに来るのは異常時のみ
    return _build(data, 3, False)


def post_tweet(text: str):
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )
    client.create_tweet(text=text)


def main() -> int:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/121.0.0.0 Safari/537.36"),
                locale="ja-JP",
                viewport={"width": 1280, "height": 1600},
            )
            page = ctx.new_page()
            page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=45000)
            data = extract_rankings(page)
            browser.close()
    except Exception as e:
        print(f"[error] 取得失敗: {e}", file=sys.stderr)
        return 1

    text = build_tweet(data)
    print("--- TWEET PREVIEW ---")
    print(text)
    print(f"--- ({weighted_len(text)} weighted chars) ---")

    if os.environ.get("DRY_RUN") == "1":
        print("[DRY_RUN=1] 投稿スキップ")
        return 0

    try:
        post_tweet(text)
        print("✅ Posted")
        return 0
    except Exception as e:
        print(f"[error] 投稿失敗: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
