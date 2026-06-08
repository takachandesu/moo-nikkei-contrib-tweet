"""
日経225 寄与度ランキング ベスト/ワースト 自動配信スクリプト (v3)

仕組み:
1. 内蔵された225銘柄リスト (code, name, PAF) を使用
2. Yahoo Finance Chart API を stock-proxy.php 経由で並列15で取得
3. 寄与額を計算: (close - prev) × PAF / 30
4. ベスト4/ワースト4 を抽出 → 前回 state と比較 → 変化があればX投稿

依存:
- requests (HTTP)
- tweepy (X API)

旧版で使っていた Playwright は不要 (ページを読まなくなったため)。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import concurrent.futures
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import tweepy

# ============================================================
#  設定
# ============================================================
JST = timezone(timedelta(hours=9))
PROXY_URL = "https://moo-stock-blog.com/stock-proxy.php"
PUBLIC_URL = "https://moo-stock-blog.com/日経225寄与度/"
DIVISOR = 30.0      # 日経の除数(ダイビザー)
PARALLEL = 15       # Yahoo並列取得数
TIMEOUT = 8         # 1リクエストあたりのタイムアウト(秒)
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

# ============================================================
#  225銘柄リスト (code, 表示名, PAF)
# ============================================================
# 225 銘柄
STOCKS = [
    ("1332", "ニッスイ", 1.0),
    ("1605", "INPEX", 0.4),
    ("1721", "コムシスHD", 1.0),
    ("1801", "大成建設", 0.2),
    ("1802", "大林組", 1.0),
    ("1803", "清水建設", 1.0),
    ("1808", "長谷工コーポレーション", 0.2),
    ("1812", "鹿島建設", 0.5),
    ("1925", "大和ハウス工業", 1.0),
    ("1928", "積水ハウス", 1.0),
    ("1963", "日揮HD", 1.0),
    ("2002", "日清製粉G本社", 1.0),
    ("2269", "明治HD", 0.4),
    ("2282", "日本ハム", 0.5),
    ("2413", "エムスリー", 2.4),
    ("2432", "ディー・エヌ・エー", 0.3),
    ("2501", "サッポロHD", 1.0),
    ("2502", "アサヒGHD", 3.0),
    ("2503", "キリンHD", 1.0),
    ("2768", "双日", 0.1),
    ("2801", "キッコーマン", 5.0),
    ("2802", "味の素", 2.0),
    ("285A", "キオクシアHD", 0.7),
    ("2871", "ニチレイ", 1.0),
    ("2914", "日本たばこ産業", 1.0),
    ("3086", "J.フロントリテイリング", 0.5),
    ("3092", "ZOZO", 3.0),
    ("3099", "三越伊勢丹HD", 1.0),
    ("3289", "東急不動産HD", 1.0),
    ("3382", "セブン&アイ・HD", 3.0),
    ("3401", "帝人", 0.2),
    ("3402", "東レ", 1.0),
    ("3405", "クラレ", 1.0),
    ("3407", "旭化成", 1.0),
    ("3436", "SUMCO", 0.1),
    ("3659", "ネクソン", 2.0),
    ("3697", "SHIFT", 1.0),
    ("3861", "王子HD", 1.0),
    ("4004", "レゾナック・HD", 0.1),
    ("4005", "住友化学", 1.0),
    ("4021", "日産化学", 1.0),
    ("4042", "東ソー", 0.5),
    ("4043", "トクヤマ", 0.2),
    ("4061", "デンカ", 0.2),
    ("4062", "イビデン", 2.0),
    ("4063", "信越化学工業", 5.0),
    ("4151", "協和キリン", 1.0),
    ("4183", "三井化学", 0.4),
    ("4188", "三菱ケミカルG", 0.5),
    ("4208", "UBE", 0.1),
    ("4307", "野村総合研究所", 1.0),
    ("4324", "電通G", 1.0),
    ("4385", "メルカリ", 1.0),
    ("4452", "花王", 1.0),
    ("4502", "武田薬品工業", 1.0),
    ("4503", "アステラス製薬", 5.0),
    ("4506", "住友ファーマ", 1.0),
    ("4507", "塩野義製薬", 3.0),
    ("4519", "中外製薬", 3.0),
    ("4523", "エーザイ", 1.0),
    ("4543", "テルモ", 8.0),
    ("4568", "第一三共", 3.0),
    ("4578", "大塚HD", 1.0),
    ("4661", "オリエンタルランド", 1.0),
    ("4689", "LINEヤフー", 0.4),
    ("4704", "トレンドマイクロ", 1.0),
    ("4751", "サイバーエージェント", 0.8),
    ("4755", "楽天G", 1.0),
    ("4901", "富士フイルムHD", 3.0),
    ("4902", "コニカミノルタ", 1.0),
    ("4911", "資生堂", 1.0),
    ("5019", "出光興産", 2.0),
    ("5020", "ENEOSHD", 1.0),
    ("5101", "横浜ゴム", 0.5),
    ("5108", "ブリヂストン", 2.0),
    ("5201", "AGC", 0.2),
    ("5214", "日本電気硝子", 0.3),
    ("5233", "太平洋セメント", 0.1),
    ("5301", "東海カーボン", 1.0),
    ("5332", "TOTO", 0.5),
    ("5333", "NGK", 1.0),
    ("5401", "日本製鉄", 0.5),
    ("5406", "神戸製鋼所", 0.1),
    ("5411", "JFEHD", 0.1),
    ("543A", "ARCHION", 1.0),
    ("5631", "日本製鋼所", 0.2),
    ("5706", "三井金属", 0.1),
    ("5711", "三菱マテリアル", 0.1),
    ("5713", "住友金属鉱山", 0.5),
    ("5714", "DOWAHD", 0.2),
    ("5801", "古河電気工業", 0.1),
    ("5802", "住友電気工業", 1.0),
    ("5803", "フジクラ", 6.0),
    ("5831", "しずおかフィナンシャルG", 1.0),
    ("6098", "リクルートHD", 3.0),
    ("6103", "オークマ", 0.4),
    ("6113", "アマダ", 1.0),
    ("6146", "ディスコ", 0.2),
    ("6178", "日本郵政", 1.0),
    ("6273", "SMC", 0.1),
    ("6301", "小松製作所", 1.0),
    ("6302", "住友重機械工業", 0.2),
    ("6305", "日立建機", 1.0),
    ("6326", "クボタ", 1.0),
    ("6361", "荏原製作所", 1.0),
    ("6367", "ダイキン工業", 1.0),
    ("6471", "日本精工", 1.0),
    ("6472", "NTN", 1.0),
    ("6473", "ジェイテクト", 1.0),
    ("6479", "ミネベアミツミ", 1.0),
    ("6501", "日立製作所", 1.0),
    ("6503", "三菱電機", 1.0),
    ("6504", "富士電機", 0.2),
    ("6506", "安川電機", 1.0),
    ("6526", "ソシオネクスト", 1.0),
    ("6532", "ベイカレント", 1.0),
    ("6645", "オムロン", 1.0),
    ("6701", "日本電気", 0.5),
    ("6702", "富士通", 1.0),
    ("6723", "ルネサスエレクトロニクス", 1.0),
    ("6724", "セイコーエプソン", 2.0),
    ("6752", "パナソニックHD", 1.0),
    ("6753", "シャープ", 1.0),
    ("6758", "ソニーG", 5.0),
    ("6762", "TDK", 15.0),
    ("6770", "アルプスアルパイン", 1.0),
    ("6841", "横河電機", 1.0),
    ("6857", "アドバンテスト", 7.2),
    ("6861", "キーエンス", 0.1),
    ("6902", "デンソー", 4.0),
    ("6920", "レーザーテック", 0.4),
    ("6954", "ファナック", 5.0),
    ("6963", "ローム", 1.0),
    ("6971", "京セラ", 8.0),
    ("6976", "太陽誘電", 1.0),
    ("6981", "村田製作所", 2.4),
    ("6988", "日東電工", 5.0),
    ("7004", "カナデビア", 0.2),
    ("7011", "三菱重工業", 1.0),
    ("7012", "川崎重工業", 0.5),
    ("7013", "IHI", 0.7),
    ("7186", "横浜フィナンシャルG", 1.0),
    ("7201", "日産自動車", 1.0),
    ("7202", "いすゞ自動車", 0.5),
    ("7203", "トヨタ自動車", 5.0),
    ("7211", "三菱自動車工業", 0.1),
    ("7261", "マツダ", 0.2),
    ("7267", "本田技研工業", 6.0),
    ("7269", "スズキ", 4.0),
    ("7270", "SUBARU", 1.0),
    ("7272", "ヤマハ発動機", 3.0),
    ("7453", "良品計画", 2.0),
    ("7532", "パン・パシフィック・インターナショナルHD", 1.0),
    ("7731", "ニコン", 1.0),
    ("7733", "オリンパス", 4.0),
    ("7735", "SCREENHD", 0.8),
    ("7741", "HOYA", 0.5),
    ("7751", "キヤノン", 1.5),
    ("7752", "リコー", 1.0),
    ("7832", "バンダイナムコHD", 3.0),
    ("7911", "TOPPANHD", 0.5),
    ("7912", "大日本印刷", 1.0),
    ("7951", "ヤマハ", 3.0),
    ("7974", "任天堂", 1.0),
    ("8001", "伊藤忠商事", 5.0),
    ("8002", "丸紅", 1.0),
    ("8015", "豊田通商", 3.0),
    ("8031", "三井物産", 2.0),
    ("8035", "東京エレクトロン", 3.0),
    ("8053", "住友商事", 1.0),
    ("8058", "三菱商事", 3.0),
    ("8233", "高島屋", 1.0),
    ("8252", "丸井G", 1.0),
    ("8253", "クレディセゾン", 1.0),
    ("8267", "イオン", 3.0),
    ("8304", "あおぞら銀行", 0.1),
    ("8306", "三菱UFJフィナンシャル・G", 1.0),
    ("8308", "りそなHD", 0.1),
    ("8309", "三井住友トラストG", 0.2),
    ("8316", "三井住友フィナンシャルG", 0.3),
    ("8331", "千葉銀行", 1.0),
    ("8354", "ふくおかフィナンシャルG", 0.2),
    ("8411", "みずほフィナンシャルG", 0.1),
    ("8591", "オリックス", 1.0),
    ("8601", "大和証券G本社", 1.0),
    ("8604", "野村HD", 1.0),
    ("8630", "SOMPOHD", 0.6),
    ("8697", "日本取引所G", 2.0),
    ("8725", "MS&ADインシュアランスGHD", 0.9),
    ("8750", "第一ライフG", 0.4),
    ("8766", "東京海上HD", 1.5),
    ("8795", "T&DHD", 0.2),
    ("8801", "三井不動産", 3.0),
    ("8802", "三菱地所", 1.0),
    ("8804", "東京建物", 0.5),
    ("8830", "住友不動産", 2.0),
    ("9001", "東武鉄道", 0.2),
    ("9005", "東急", 0.5),
    ("9007", "小田急電鉄", 0.5),
    ("9008", "京王電鉄", 1.0),
    ("9009", "京成電鉄", 1.5),
    ("9020", "東日本旅客鉄道", 0.3),
    ("9021", "西日本旅客鉄道", 0.2),
    ("9022", "東海旅客鉄道", 0.5),
    ("9064", "ヤマトHD", 1.0),
    ("9101", "日本郵船", 0.3),
    ("9104", "商船三井", 0.3),
    ("9107", "川崎汽船", 0.9),
    ("9147", "NIPPONEXPRESSHD", 0.3),
    ("9201", "日本航空", 1.0),
    ("9202", "ANAHD", 0.1),
    ("9432", "NTT", 10.0),
    ("9433", "KDDI", 12.0),
    ("9434", "ソフトバンク", 10.0),
    ("9501", "東京電力HD", 0.1),
    ("9502", "中部電力", 0.1),
    ("9503", "関西電力", 0.1),
    ("9531", "東京瓦斯", 0.2),
    ("9532", "大阪瓦斯", 0.2),
    ("9602", "東宝", 0.5),
    ("9735", "セコム", 2.0),
    ("9766", "コナミG", 1.0),
    ("9843", "ニトリHD", 2.5),
    ("9983", "ファーストリテイリング", 2.4),
    ("9984", "ソフトバンクG", 24.0),
]


URL_RE = re.compile(r"https?://\S+")


def weighted_len(s: str) -> int:
    """X の文字カウント: URL=23(t.co短縮), CJK等=2, ASCII=1"""
    counted = URL_RE.sub("X" * 23, s)
    return sum(1 if ord(c) < 0x80 else 2 for c in counted)


def fmt_yen_1dec(yen_value: float) -> str:
    sign = "+" if yen_value >= 0 else "-"
    return f"{sign}¥{abs(yen_value):.1f}"


def fmt_pct_1dec(pct_value: float) -> str:
    sign = "+" if pct_value >= 0 else "-"
    return f"{sign}{abs(pct_value):.1f}%"


def short_name(name: str) -> str:
    return NAME_SHORT.get(name, name)


# ============================================================
#  Yahoo Finance 取得 (並列)
# ============================================================
def fetch_yahoo(code: str) -> tuple[float, float] | None:
    """1銘柄の (close, prev) を Yahoo から取得。失敗時 None。"""
    yahoo_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T"
    proxy_url = f"{PROXY_URL}?url={urllib.parse.quote(yahoo_url, safe='')}"
    try:
        r = requests.get(proxy_url, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        j = r.json()
        result = j.get("chart", {}).get("result", [])
        if not result:
            return None
        meta = result[0].get("meta", {})
        close = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose")
        if not isinstance(close, (int, float)) or not isinstance(prev, (int, float)) or prev <= 0:
            return None
        return (float(close), float(prev))
    except Exception:
        return None


def fetch_all_prices() -> dict[str, tuple[float, float]]:
    """全225銘柄を並列取得。{code: (close, prev)} を返す。"""
    results: dict[str, tuple[float, float]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        future_to_code = {ex.submit(fetch_yahoo, code): code for code, _, _ in STOCKS}
        for f in concurrent.futures.as_completed(future_to_code):
            code = future_to_code[f]
            data = f.result()
            if data is not None:
                results[code] = data
    return results


# ============================================================
#  寄与額計算 → ランキング
# ============================================================
def compute_rankings(prices: dict[str, tuple[float, float]]) -> dict:
    """ベスト4/ワースト4 を抽出"""
    items = []
    for code, name, paf in STOCKS:
        if code not in prices:
            continue
        close, prev = prices[code]
        yen = (close - prev) * paf / DIVISOR
        pct = (close - prev) / prev * 100
        items.append({
            "code": code,
            "name": name,
            "yen": yen,
            "pct_value": pct,
            "close": close,
            "prev": prev,
        })
    items.sort(key=lambda x: x["yen"], reverse=True)
    best = items[:4]
    worst = list(reversed(items[-4:]))   # 最下位(最も下げた銘柄)を先頭に
    now = datetime.now(JST)
    return {
        "best": best,
        "worst": worst,
        "update_time": now.strftime("%H:%M"),
    }


# ============================================================
#  ツイート組み立て
# ============================================================
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
    lines.append(PUBLIC_URL)
    return "\n".join(lines)


def build_tweet(data: dict) -> str:
    candidates = [
        (4, True, True),
        (4, True, False),
        (4, False, True),
        (4, False, False),
        (3, True, True),
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


# ============================================================
#  X 投稿
# ============================================================
def post_tweet(text: str):
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )
    client.create_tweet(text=text)


# ============================================================
#  変化検知 (構成 / 順位 / 寄与額)
# ============================================================
def composition_changed(new_data: dict, old_state: dict | None) -> bool:
    if old_state is None:
        return True

    YEN_DELTA_RATIO = 0.20

    def codes_order(items):
        return [r["code"] for r in items[:4]]

    def yen_by_code(items):
        return {r["code"]: r.get("yen", 0.0) for r in items[:4]}

    for side in ("best", "worst"):
        new_order = codes_order(new_data[side])
        old_order = codes_order(old_state.get(side, []))

        if set(new_order) != set(old_order):
            print(f"[info] {side} 構成変化: {set(new_order) ^ set(old_order)}")
            return True
        if new_order != old_order:
            print(f"[info] {side} 順位変化: 旧={old_order} 新={new_order}")
            return True

        new_yen = yen_by_code(new_data[side])
        old_yen = yen_by_code(old_state.get(side, []))
        for code, ny in new_yen.items():
            oy = old_yen.get(code, 0.0)
            if abs(oy) < 1.0:
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


# ============================================================
#  メイン
# ============================================================
def main() -> int:
    print(f"[info] 取得開始: {len(STOCKS)}銘柄 (並列{PARALLEL})")
    t0 = datetime.now()
    prices = fetch_all_prices()
    elapsed = (datetime.now() - t0).total_seconds()
    got = len(prices)
    print(f"[info] 取得完了: {got}/{len(STOCKS)} 銘柄 ({elapsed:.1f}秒)")

    if got < 50:
        print(f"[error] 取得銘柄数が少なすぎる ({got}/{len(STOCKS)}) - スキップ", file=sys.stderr)
        return 1

    data = compute_rankings(prices)
    print(f"[info] ランキング計算完了 (時刻: {data['update_time']})")
    print(f"[info] ベスト1: {data['best'][0]['name']} +¥{data['best'][0]['yen']:.1f}")
    print(f"[info] ワースト1: {data['worst'][0]['name']} ¥{data['worst'][0]['yen']:.1f}")

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
