"""
日経225寄与度ランキングをページ更新後に自動配信するスクリプト。
moo-stock-blog.com の寄与度ページをPlaywrightで開き、
前回保存した state と比較して、ベスト4 / ワースト4 の構成銘柄に
変化があった時だけX投稿する。

ツイート仕様:
- 数値は小数点1桁(第2位四捨五入)
- URL を末尾に貼る(X側でt.co短縮: 23文字扱い)
- 4位優先、文字数オーバーなら3位にフォールバック
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import tweepy
from playwright.sync_api import sync_playwright

JST = timezone(timedelta(hours=9))
PAGE_URL = "https://moo-stock-blog.com/%e6%97%a5%e7%b5%8c225%e5%af%84%e4%b8%8e%e5%ba%a6/"
PUBLIC_URL = "https://moo-stock-blog.com/日経225寄与度/"   # ツイート末尾用(短縮形)
STATE_FILE = Path("data/last_state.json")
TWEET_LIMIT = 280

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


def parse_pct(text: str) -> float | None:
    """'+10.91%' → 10.91"""
    m = re.search(r"([+-]?)\s*([0-9]+(?:\.[0-9]+)?)", text)
    if not m:
        return None
    sign, num = m.groups()
    val = float(num)
    return -val if sign == "-" else val


URL_RE = re.compile(r"https?://\S+")


def weighted_len(s: str) -> int:
    """X の文字カウント: URL=23(t.co短縮), CJK等=2, ASCII=1"""
    counted = URL_RE.sub("X" * 23, s)
    return sum(1 if ord(c) < 0x80 else 2 for c in counted)


def fmt_yen_1dec(yen_value: float) -> str:
    """+¥215.0 / -¥54.7 形式 (小数点1桁)"""
    sign = "+" if yen_value >= 0 else "-"
    return f"{sign}¥{abs(yen_value):.1f}"


def fmt_pct_1dec(pct_value: float) -> str:
    """+10.9% / -2.4% 形式 (小数点1桁)"""
    sign = "+" if pct_value >= 0 else "-"
    return f"{sign}{abs(pct_value):.1f}%"


def extract_rankings(page) -> dict:
    # ページのJS処理が終わるまで待つ:
    #  - ランキング行が4位まで揃ってる
    #  - かつ「最終更新 HH:MM」がプレースホルダ「―」から実時刻に置き換わってる
    # Yahoo Financeは1銘柄/req のため遅め → 120秒待ちに設定
    page.wait_for_function(
        """() => {
            const best = document.querySelectorAll('.imp-rank-col.up .imp-rank-row').length;
            const worst = document.querySelectorAll('.imp-rank-col.down .imp-rank-row').length;
            if (best < 4 || worst < 4) return false;
            const t = document.querySelector('.imp-ranks-time[data-update-time]');
            if (t && /\\d{1,2}:\\d{2}/.test(t.textContent || '')) return true;
            // 時刻表示がまだ無くてもランキングが揃っていればOK (後方互換)
            return true;
        }""",
        timeout=120000,
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
                "pct_value": parse_pct(pct_txt),
            })
        return items

    best = grab("up")
    worst = grab("down")
    if len(best) < 4 or len(worst) < 4:
        raise RuntimeError(f"ランキング取得不足 best={len(best)} worst={len(worst)}")

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


def _build(data: dict, top_n: int, include_pct: bool, include_ts: bool) -> str:
    now = datetime.now(JST)
    date_str = f"{now.month}/{now.day}"
    if include_ts:
        ut = data.get("update_time", "")
        suffix = f" / 更新 {ut}" if ut else ""
    else:
        suffix = ""
    lines = [f"📊 日経225 寄与度ランキング ({date_str}{suffix})", ""]

    def fmt(s):
        nm = short_name(s["name"])
        yen_str = fmt_yen_1dec(s["yen"])
        if include_pct and s["pct_value"] is not None:
            pct_str = fmt_pct_1dec(s["pct_value"])
            return f"{nm} {yen_str} ({pct_str})"
        return f"{nm} {yen_str}"

    lines.append(f"🟢 ベスト{top_n}(押し上げ)")
    for i, s in enumerate(data["best"][:top_n], 1):
        lines.append(f"{i}. {fmt(s)}")
    lines.append("")
    lines.append(f"🔴 ワースト{top_n}(押し下げ)")
    for i, s in enumerate(data["worst"][:top_n], 1):
        lines.append(f"{i}. {fmt(s)}")
    lines.append("")
    lines.append("#日経平均 #日経225 #寄与度")
    lines.append(PUBLIC_URL)   # ← URL は最後
    return "\n".join(lines)


def build_tweet(data: dict) -> str:
    """4位優先で情報多い順に試し、入らなければ落としていく"""
    candidates = [
        # (top_n, include_pct, include_ts)
        (4, True, True),     # 4位+騰落率+更新時刻
        (4, True, False),    # 4位+騰落率
        (4, False, True),    # 4位+更新時刻
        (4, False, False),   # 4位のみ
        (3, True, True),     # 3位+全情報
        (3, True, False),
        (3, False, True),
        (3, False, False),
    ]
    for top_n, pct, ts in candidates:
        t = _build(data, top_n, pct, ts)
        L = weighted_len(t)
        if L <= TWEET_LIMIT:
            print(f"[info] 採用: top_n={top_n} pct={pct} ts={ts} len={L}")
            return t
        else:
            print(f"[info] スキップ: top_n={top_n} pct={pct} ts={ts} len={L} (上限超え)")
    return _build(data, 3, False, False)


def post_tweet(text: str):
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )
    client.create_tweet(text=text)


def composition_changed(new_data: dict, old_state: dict | None) -> bool:
    """変化検知: 以下のいずれかで「変化あり」と判定
    (a) ベスト4/ワースト4 の銘柄構成が入れ替わった
    (b) 銘柄は同じでも順位が変わった
    (c) いずれかの銘柄で寄与額が ±20% 以上動いた
    """
    if old_state is None:
        return True

    YEN_DELTA_RATIO = 0.20   # 寄与額が ±20% 以上動いたら変化扱い

    def codes_order(items):
        return [r["code"] for r in items[:4]]

    def yen_by_code(items):
        return {r["code"]: r.get("yen", 0.0) for r in items[:4]}

    for side in ("best", "worst"):
        new_order = codes_order(new_data[side])
        old_order = codes_order(old_state.get(side, []))

        # (a) 構成変化: 銘柄コードの集合が違う
        if set(new_order) != set(old_order):
            print(f"[info] {side} 構成変化: {set(new_order) ^ set(old_order)}")
            return True

        # (b) 順位変化: 銘柄は同じだが順位が違う
        if new_order != old_order:
            print(f"[info] {side} 順位変化: 旧={old_order} 新={new_order}")
            return True

        # (c) 寄与額の大きな変化
        new_yen = yen_by_code(new_data[side])
        old_yen = yen_by_code(old_state.get(side, []))
        for code, ny in new_yen.items():
            oy = old_yen.get(code, 0.0)
            if abs(oy) < 1.0:
                # 旧値がほぼ0なら新値が1円超で変化扱い
                if abs(ny) >= 1.0:
                    print(f"[info] {side} {code} 寄与額変化(旧≈0): {oy:.1f} → {ny:.1f}")
                    return True
                continue
            ratio = abs(ny - oy) / abs(oy)
            if ratio >= YEN_DELTA_RATIO:
                print(f"[info] {side} {code} 寄与額±{YEN_DELTA_RATIO*100:.0f}%超: {oy:.1f} → {ny:.1f} ({ratio*100:.0f}%)")
                return True

    return False


def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[warn] state読み込み失敗 (新規扱い): {e}", file=sys.stderr)
        return None


def save_state(data: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "best": data["best"][:4],
        "worst": data["worst"][:4],
        "saved_at": datetime.now(JST).isoformat(),
    }
    STATE_FILE.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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

    old_state = load_state()
    if not composition_changed(data, old_state):
        print("[info] 構成変化なし → 投稿スキップ")
        return 0

    text = build_tweet(data)
    print("--- TWEET PREVIEW ---")
    print(text)
    print(f"--- ({weighted_len(text)} weighted chars) ---")

    if os.environ.get("DRY_RUN") == "1":
        print("[DRY_RUN=1] 投稿スキップ(state も更新しない)")
        return 0

    try:
        post_tweet(text)
        print("✅ Posted")
    except Exception as e:
        print(f"[error] 投稿失敗 (state は更新しない): {e}", file=sys.stderr)
        return 2

    save_state(data)
    print(f"[info] state を保存: {STATE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
