"""時系列のCSVを3つの統計の方法で調べ、ブラウザで開くだけで見られる dashboard.html を作る。

使い方: python3 dashboard.py data.csv
"""

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

import pandas as pd

# 設定：検出に使う値。ダッシュボードの説明文（期間・しきい値）もこの値から書く
TRAIN_HOURS = 72  # 最初の72時間（3日間）を「いつもの状態」の手本にする
K = 3             # 手本のばらつき（σ）の何倍外れたら異常とするか
MA_HOURS = 24     # 「最近の流れ」とする移動平均の長さ（時間）
FLAT_HOURS = 3    # この時間ずっと同じ値なら「値が止まった」とする
METHODS = {"sigma": f"範囲チェック（平均±{K}σ）", "moving": "流れチェック（移動平均からのずれ）", "flat": "停止チェック（値の張り付き）"}

# 正解：data.csv（練習用の架空データ）に入れた異常の区間。時刻が合わないCSVでは答え合わせを省く
ANSWERS = [
    ("spike", "2026-11-05 20:00:00", "2026-11-05 20:00:00"),
    ("drift", "2026-11-07 18:00:00", "2026-11-08 17:00:00"),
    ("stuck", "2026-11-11 08:00:00", "2026-11-11 13:00:00"),
    ("drop", "2026-11-15 00:00:00", "2026-11-15 11:00:00"),
]

# 出力：グラフの道具 Chart.js の取得元と、中身を確かめるための指紋（SHA-256）
CHART_JS_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js"
CHART_JS_SHA256 = "74401d738dd3e03ee5dfb3b6841210fe2c4ead8a960c4011ca4ba0b78a9fd8f3"
OUTPUT = Path(__file__).resolve().parent / "dashboard.html"


# 1. 読み込み：CSV（timestamp, value の2列）を読み、1時間ごとの表にそろえる
def load(path):
    if not path.exists():
        sys.exit(f"{path} が見つかりません")
    df = pd.read_csv(path)
    missing = {"timestamp", "value"} - set(df.columns)
    if missing:
        sys.exit(f"{path} に {', '.join(sorted(missing))} の列がありません（timestamp, value の2列が必要です）")
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["timestamp", "value"]).sort_values("timestamp")
    hourly = df.set_index("timestamp")["value"].resample("1h").mean().interpolate()
    if not hourly.index.equals(pd.DatetimeIndex(df["timestamp"])):
        print("1時間ごとのデータではないため、1時間ごとの平均にそろえました")
    if len(hourly) < TRAIN_HOURS + MA_HOURS:
        sys.exit(f"データが短すぎます（{TRAIN_HOURS + MA_HOURS}時間以上必要です）")
    return hourly.rename("value").rename_axis("timestamp").reset_index()


# 2. 検出（方法1）：手本の平均±K×σ の外に出た点に印を付ける
def sigma_check(values):
    train = values[:TRAIN_HOURS]
    mean, sd = train.mean(), train.std()
    lower, upper = mean - K * sd, mean + K * sd
    return (values < lower) | (values > upper), {"mean": mean, "sd": sd, "lower": lower, "upper": upper}


# 2. 検出（方法2）：直前 MA_HOURS 時間の平均（今の値は含めない）から、ふだんのずれの K 倍より離れた点に印を付ける
def moving_check(values):
    ma = values.rolling(MA_HOURS).mean().shift(1)
    gap = values - ma
    sd = gap[:TRAIN_HOURS].std()
    return gap.abs() > K * sd, ma, sd


# 2. 検出（方法3）：FLAT_HOURS 時間の最大と最小の差がゼロなら、その時間すべてに印を付ける
def flat_check(values):
    width = values.rolling(FLAT_HOURS).max() - values.rolling(FLAT_HOURS).min()
    flags = [False] * len(values)
    for end, w in enumerate(width):
        if w == 0:
            flags[end - FLAT_HOURS + 1 : end + 1] = [True] * FLAT_HOURS
    return pd.Series(flags, index=values.index)


# 2. 検出（正解の準備）：ANSWERS のうち、開始と終了の時刻がこのCSVにある区間だけを使う
def answers_for(df):
    times = set(df["timestamp"])
    spans = [(kind, pd.Timestamp(s), pd.Timestamp(e)) for kind, s, e in ANSWERS]
    return [(kind, s, e) for kind, s, e in spans if s in times and e in times]


# 2. 検出（答え合わせ）：正解の区間と比べ、種類ごとの検出数・見逃し・誤報を数える
def score(df, flags, truth):
    is_anomaly = pd.Series(False, index=df.index)
    by_type = []
    for kind, start, end in truth:
        inside = df["timestamp"].between(start, end)
        is_anomaly |= inside
        hit_times = df.loc[inside & flags, "timestamp"]
        by_type.append({
            "type": kind,
            "hits": len(hit_times),
            "total": int(inside.sum()),
            "first_after_hours": None if hit_times.empty
            else int((hit_times.iloc[0] - start) / pd.Timedelta(hours=1)),
        })
    return {
        "flagged": int(flags.sum()),
        "missed": sum(r["hits"] == 0 for r in by_type),
        "false_alarms": int((flags & ~is_anomaly).sum()),
        "by_type": by_type,
    }


# 3. HTML作成：Chart.js を取ってきて、指紋が合うか（途中で変わっていないか）確かめる
def fetch_chartjs():
    try:
        with urllib.request.urlopen(CHART_JS_URL, timeout=30) as res:
            body = res.read()
    except OSError as err:
        sys.exit(f"Chart.js を取得できませんでした（インターネット接続を確認してください）: {err}")
    if hashlib.sha256(body).hexdigest() != CHART_JS_SHA256:
        sys.exit("取得した Chart.js の中身が想定と違うため、中止しました")
    return body.decode("utf-8")


# 3. HTML作成：検出結果と Chart.js を HTML に埋め込み、1ファイルで動く dashboard.html を書き出す
def build_html(source, df, sigma_info, ma_sd, truth, results):
    nums = lambda col: [None if pd.isna(x) else round(float(x), 3) for x in df[col]]
    data = {
        "source": source,
        "settings": {"train_hours": TRAIN_HOURS, "k": K, "ma_hours": MA_HOURS, "flat_hours": FLAT_HOURS},
        "sigma": {key: round(float(v), 2) for key, v in sigma_info.items()},
        "moving_sd": round(float(ma_sd), 2),
        "truth": [{"type": kind, "start_at": f"{s:%Y-%m-%d %H:%M:%S}", "end_at": f"{e:%Y-%m-%d %H:%M:%S}"}
                  for kind, s, e in truth],
        "methods": [{"key": key, **r} for key, r in results.items()],
        "points": {
            "timestamp": df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
            "value": nums("value"), "ma": nums("ma"), "ma_lower": nums("ma_lower"), "ma_upper": nums("ma_upper"),
            **{key: df[key].astype(int).tolist() for key in METHODS},
        },
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    chartjs = fetch_chartjs().replace("</script", "<\\/script")
    html = TEMPLATE.replace("@@DATA@@", payload).replace("/*@@CHARTJS@@*/", chartjs)
    OUTPUT.write_text(html, encoding="utf-8")


# 3. HTML作成：ダッシュボードの見た目と動き。@@DATA@@ と @@CHARTJS@@ の場所に中身が入る
TEMPLATE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>異常チェック ダッシュボード</title>
<meta name="description" content="時系列のセンサーの値を、3つの統計のチェックで調べた結果を、触って確かめるダッシュボード。">
<style>
:root {
  color-scheme: light;
  --page: #f6f5f1;
  --surface: #ffffff;
  --ink: #1b1b19;
  --sub: #52514e;
  --muted: #6f6e69;
  --line: #e1e0d9;
  --grid: #ecebe5;
  --sigma: #2a78d6;
  --moving: #eb6834;
  --flat: #1baf7a;
  --flow-band: rgba(235, 104, 52, 0.15);
  --band: #e6e4dc;
  --frame: #6f6e69;
  --full-bg: #e5f3e5;  --full-ink: #1d651d;
  --part-bg: #fcefd2;  --part-ink: #6f4b00;
  --none-bg: #fae3e1;  --none-ink: #962a28;
  /* 文字の大きさは3段階だけ：見出し・本文・注釈 */
  --fs-h: 20px;
  --fs-b: 15px;
  --fs-n: 12.5px;
  --sans: "Hiragino Sans", "Hiragino Kaku Gothic ProN", "BIZ UDPGothic", "Yu Gothic UI", "Meiryo", system-ui, sans-serif;
  /* 余白：段と段の間は広く、段の中は狭く */
  --gap-section: 76px;
  --gap-inside: 12px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #111110; --surface: #1a1a19; --ink: #f0efec; --sub: #c3c2b7; --muted: #a3a29b;
    --line: #32322f; --grid: #2a2a28;
    --sigma: #3987e5; --moving: #d95926; --flat: #199e70;
    --flow-band: rgba(217, 89, 38, 0.24); --band: #34332f; --frame: #a3a29b;
    --full-bg: #183019; --full-ink: #9bdc9b;
    --part-bg: #33290f; --part-ink: #f2c66f;
    --none-bg: #3a1c1b; --none-ink: #f3a7a5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #111110; --surface: #1a1a19; --ink: #f0efec; --sub: #c3c2b7; --muted: #a3a29b;
  --line: #32322f; --grid: #2a2a28;
  --sigma: #3987e5; --moving: #d95926; --flat: #199e70;
  --flow-band: rgba(217, 89, 38, 0.24); --band: #34332f; --frame: #a3a29b;
  --full-bg: #183019; --full-ink: #9bdc9b;
  --part-bg: #33290f; --part-ink: #f2c66f;
  --none-bg: #3a1c1b; --none-ink: #f3a7a5;
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--page);
  color: var(--ink);
  font-family: var(--sans);
  font-size: var(--fs-b);
  line-height: 1.7;
}
h1, h2, h3, p, ul { margin: 0; }
h1, h2 { font-size: var(--fs-h); line-height: 1.4; font-weight: 700; }
h3 { font-size: var(--fs-b); font-weight: 700; }
.note { font-size: var(--fs-n); color: var(--muted); line-height: 1.6; }
a { color: var(--sigma); text-underline-offset: 3px; }
button { font: inherit; color: inherit; }
[hidden] { display: none !important; }
.wrap { max-width: 1200px; margin: 0 auto; padding: 0 16px; }

/* 見出し */
header.top { padding-top: 32px; padding-bottom: 20px; }
header.top .sub { color: var(--sub); }

/* 印の切り替え（スクロールしても上に残る） */
.controls {
  position: sticky; top: 0; z-index: 20;
  background: var(--surface);
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}
.controls .wrap { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding-top: 8px; padding-bottom: 8px; }
.controls .note { margin-right: 4px; }

/* 切り替えボタン（凡例を兼ねる） */
.toggle {
  display: inline-flex; align-items: center; gap: 7px;
  min-height: 34px; padding: 4px 12px;
  border: 1.5px solid var(--ink); border-radius: 999px;
  background: var(--surface); cursor: pointer;
  font-size: var(--fs-n); line-height: 1.3;
}
.toggle[aria-pressed="false"] { border-color: var(--line); color: var(--muted); }
.toggle[aria-pressed="false"] .sw { opacity: 0.3; }
.toggle:hover { background: var(--page); }
.toggle:focus-visible, .stat:focus-visible, .seg button:focus-visible, .help:focus-visible, .link-btn:focus-visible { outline: 2px solid var(--sigma); outline-offset: 2px; }

/* 色見本 */
.sw { display: inline-block; flex: none; }
.sw-ring { width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--sigma); }
.sw-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--moving); margin: 0 2px; }
.sw-square { width: 11px; height: 11px; border: 2px solid var(--flat); }
.sw-line-ink { width: 20px; height: 0; border-top: 2px solid var(--ink); }
.sw-line-sigma { width: 20px; height: 0; border-top: 2px solid var(--sigma); }
.sw-band-moving { width: 20px; height: 10px; background: var(--flow-band); border-top: 2px solid var(--moving); }
.sw-band-truth { width: 20px; height: 12px; background: var(--band); border-radius: 2px; }

/* ? の説明 */
.help {
  display: inline-grid; place-items: center;
  width: 20px; height: 20px; margin: 0 2px 0 4px; padding: 0;
  border: 1px solid var(--line); border-radius: 50%;
  background: var(--surface); color: var(--muted);
  font-size: var(--fs-n); font-weight: 700; line-height: 1;
  cursor: pointer; vertical-align: 1px;
}
.help[aria-expanded="true"] { background: var(--ink); color: var(--surface); border-color: var(--ink); }
.pop {
  position: absolute; z-index: 40;
  max-width: min(320px, calc(100vw - 16px));
  padding: 10px 12px;
  background: var(--ink); color: var(--surface);
  border-radius: 10px;
  font-size: var(--fs-n); line-height: 1.6;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
}

/* 段 */
main { padding-bottom: 64px; }
.sec { display: flex; flex-direction: column; gap: var(--gap-inside); margin-top: var(--gap-section); }
.sec:first-child { margin-top: 36px; }
.sec h2 .num { color: var(--muted); margin-right: 8px; }
.insight { display: flex; flex-direction: column; gap: 2px; }
.insight .label { font-size: var(--fs-n); color: var(--muted); }
.insight .text { font-weight: 600; }

/* ① カード */
.cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--gap-inside); }
.card {
  display: flex; flex-direction: column; gap: 10px;
  background: var(--surface);
  border: 1px solid var(--line); border-radius: 14px;
  padding: 16px;
}
.card-title { display: flex; align-items: center; gap: 8px; }
.card-desc { color: var(--sub); }
.stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.stat {
  display: flex; flex-direction: column; align-items: flex-start; gap: 0;
  padding: 8px 10px; min-height: 64px;
  border: 1px solid var(--line); border-radius: 10px;
  background: var(--page); cursor: pointer; text-align: left;
}
.stat:hover { border-color: var(--muted); }
.stat[aria-pressed="true"] { border: 2px solid var(--ink); padding: 7px 9px; background: var(--surface); }
.stat-label { font-size: var(--fs-n); color: var(--muted); }
.stat-num { font-size: var(--fs-h); font-weight: 700; line-height: 1.3; }
.stat-unit { font-size: var(--fs-n); font-weight: 400; color: var(--muted); margin-left: 2px; }
.chips { list-style: none; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font-size: var(--fs-n); padding: 2px 8px; border-radius: 6px; }
.chip.full { background: var(--full-bg); color: var(--full-ink); }
.chip.part { background: var(--part-bg); color: var(--part-ink); }
.chip.none { background: var(--none-bg); color: var(--none-ink); }
.status { min-height: 1.6em; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; }
.status b { color: var(--ink); }
.link-btn { border: 0; background: none; padding: 0; color: var(--sigma); text-decoration: underline; text-underline-offset: 3px; cursor: pointer; font-size: var(--fs-n); }

/* グラフ */
.chart-box {
  position: relative;
  height: var(--h, 340px);
  background: var(--surface);
  border: 1px solid var(--line); border-radius: 14px;
  padding: 12px 12px 6px 4px;
}
.chart-box canvas { touch-action: pan-y; }
#box2 { --h: 360px; }
#box3 { --h: 330px; }
#box4 { --h: 180px; }
.legend { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.legend .note { margin-right: 4px; }

/* グラフの説明（なぞると出る） */
.tt {
  position: absolute; left: 0; top: 0; z-index: 10;
  min-width: 200px; max-width: 260px;
  padding: 10px 12px;
  background: var(--surface); color: var(--ink);
  border: 1px solid var(--line); border-radius: 10px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.14);
  font-size: var(--fs-n); line-height: 1.65;
  pointer-events: none;
}
.tt-time { font-size: var(--fs-b); font-weight: 700; }
.tt .who { display: inline-flex; align-items: center; gap: 5px; margin-right: 8px; white-space: nowrap; }
.tt-status { margin-top: 6px; padding: 2px 8px; border-radius: 6px; font-weight: 700; }
.tt-status.ok { background: var(--full-bg); color: var(--full-ink); }
.tt-status.ng { background: var(--none-bg); color: var(--none-ink); }
.tt-status.miss { background: var(--part-bg); color: var(--part-ink); }
.tt-status.calm { background: var(--page); color: var(--sub); font-weight: 400; }
.tt-note { color: var(--muted); margin-top: 4px; }

/* ③ 異常の選択 */
.seg { display: flex; flex-wrap: wrap; gap: 8px; }
.seg button {
  min-height: 38px; padding: 6px 16px;
  border: 1.5px solid var(--line); border-radius: 10px;
  background: var(--surface); cursor: pointer;
}
.seg button:hover { border-color: var(--muted); }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--surface); border-color: var(--ink); font-weight: 700; }

.error { margin-top: 32px; padding: 14px 16px; border-radius: 10px; background: var(--none-bg); color: var(--none-ink); }
footer { padding: 24px 0 48px; border-top: 1px solid var(--line); }

@media (max-width: 860px) {
  .cards { grid-template-columns: 1fr; }
}
@media (max-width: 560px) {
  :root { --gap-section: 56px; }
  header.top { padding-top: 22px; }
  .controls .wrap { gap: 6px; padding-top: 6px; padding-bottom: 6px; }
  .controls .long { display: none; }
  .toggle { padding: 4px 10px; }
  #box2 { --h: 270px; }
  #box3 { --h: 260px; }
  #box4 { --h: 160px; }
  .chart-box { padding: 10px 8px 4px 2px; }
}
</style>
</head>
<body>

<header class="wrap top">
  <h1>温度センサーの異常チェック</h1>
  <p class="sub" id="subtitle"></p>
</header>

<div class="controls">
  <div class="wrap" role="group" aria-label="グラフに出す印">
    <span class="note">グラフに出す印</span>
    <span id="method-toggles" style="display:contents"></span>
  </div>
</div>

<main class="wrap" id="main">

  <section class="sec" id="s1" aria-labelledby="h1-title">
    <h2 id="h1-title"><span class="num">①</span>今の状態</h2>
    <p id="lead1"></p>
    <div class="cards" id="cards"></div>
    <p class="note" id="card-legend"></p>
    <p class="note status" id="hl-status" aria-live="polite"></p>
  </section>

  <section class="sec" id="s2" aria-labelledby="h2-title">
    <h2 id="h2-title"><span class="num">②</span>全体グラフ<span id="days"></span></h2>
    <p class="insight"><span class="label">このグラフでわかること</span><span class="text" id="insight2"></span></p>
    <div class="chart-box" id="box2"><canvas id="c2" role="img" aria-label="全期間のセンサーの値と、3つのチェックが反応した点のグラフ"></canvas><div class="tt" hidden></div></div>
    <div class="legend" data-legend></div>
    <p class="note" id="note2"></p>
  </section>

  <section class="sec" id="s3" aria-labelledby="h3-title">
    <h2 id="h3-title"><span class="num">③</span>選んだ異常の拡大</h2>
    <div class="seg" id="type-buttons" role="group" aria-label="拡大する異常"></div>
    <p class="insight"><span class="label">このグラフでわかること</span><span class="text" id="insight3"></span></p>
    <div class="chart-box" id="box3"><canvas id="c3" role="img" aria-label="選んだ異常の前後を拡大したグラフ"></canvas><div class="tt" hidden></div></div>
    <p class="note" id="note3"></p>
    <div class="legend" data-legend></div>
  </section>

  <section class="sec" id="s4" aria-labelledby="h4-title">
    <h2 id="h4-title"><span class="num">④</span>各方法の判定</h2>
    <p class="insight"><span class="label">このグラフでわかること</span><span class="text" id="insight4"></span></p>
    <div class="chart-box" id="box4"><canvas id="c4" role="img" aria-label="3つのチェックが反応した時間を、方法ごとに1行ずつ並べた図"></canvas><div class="tt" hidden></div></div>
    <p class="note" id="note4"></p>
  </section>

</main>

<footer class="wrap">
  <p class="note" id="footer-note"></p>
</footer>

<div class="pop" id="pop" role="tooltip" hidden></div>

<script>/*@@CHARTJS@@*/</script>
<script id="dashboard-data" type="application/json">@@DATA@@</script>
<script>
(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const fail = (msg) => { el("main").innerHTML = `<p class="error">${msg}</p>`; };
  if (!window.Chart) return fail("グラフの道具（Chart.js）を読み込めませんでした。dashboard.py を実行し直してください。");
  const R = JSON.parse(el("dashboard-data").textContent);

  // ---------- 1. データを読む（dashboard.py が埋め込んだ値） ----------
  const D = R.points;
  const P = D.timestamp.map((ts, i) => ({
    i, ts,
    day: `${ts.slice(5, 7)}/${ts.slice(8, 10)}`,
    time: ts.slice(11, 16),
    hour: Number(ts.slice(11, 13)),
    value: D.value[i], sl: R.sigma.lower, su: R.sigma.upper,
    ma: D.ma[i], ml: D.ma_lower[i], mu: D.ma_upper[i],
    flags: { sigma: !!D.sigma[i], moving: !!D.moving[i], flat: !!D.flat[i] },
    truth: null,
  }));
  const N = P.length;
  const at = new Map(P.map((p) => [p.ts, p.i]));
  const TRUTH = R.truth.map((t) => ({ ...t, from: at.get(t.start_at), to: at.get(t.end_at) }))
    .filter((t) => t.from !== undefined && t.to !== undefined);
  TRUTH.forEach((t) => { for (let i = t.from; i <= t.to; i++) P[i].truth = t; });
  const HAS_TRUTH = TRUTH.length > 0;

  // ---------- 2. 画面の言葉（専門用語は ? の中に入れる） ----------
  const M = Object.fromEntries(R.methods.map((m) => [m.key, m]));
  const KEYS = R.methods.map((m) => m.key);
  const UI = {
    sigma: { label: "範囲チェック", short: "範囲", desc: "いつもの範囲から外れた", sw: "ring", help: "sigma" },
    moving: { label: "流れチェック", short: "流れ", desc: "最近の流れから急に外れた", sw: "dot", help: "flow" },
    flat: { label: "停止チェック", short: "停止", desc: "値が動かなくなった", sw: "square", help: "flat" },
  };
  const TYPE = {
    spike: { button: "急上昇", noun: "1時間だけの急上昇", area: "急上昇の区間" },
    drift: { button: "じわじわ上昇", noun: "じわじわ上昇", area: "じわじわ上昇の区間" },
    stuck: { button: "値が止まる", noun: "値が止まる故障", area: "値が止まる区間" },
    drop: { button: "急落", noun: "急落", area: "急落の区間" },
  };
  const S = R.settings;
  const PAD = S.ma_hours; // 拡大するとき前後に足す時間（流れチェックが異常を覚えている長さ）
  const rarity = Math.round(erfc(S.k / Math.SQRT2) * 1000);
  const HELP = {
    sigma: `σ（シグマ）は、ふだんの揺れの大きさで、${S.k}σの外に出るのは${rarity >= 1 ? `1000回に${rarity}回ほど` : "1000回に1回もない"}の珍しさ。`,
    range: `いつもの範囲は、最初の${S.train_hours}時間（${+(S.train_hours / 24).toFixed(1)}日間）の値から計算した「平均±${S.k}σ」の幅。`,
    flow: `移動平均は直前${S.ma_hours}時間の値の平均のことで、ここではこれを「最近の流れ」とよび、今の値がそこから急に離れると反応する。`,
    flat: `${S.flat_hours}時間のあいだ値が少しも変わらなければ、センサーが止まったとみなして反応する。`,
    missed: "見逃しは、本当の異常の区間に1時間も反応できなかったこと。",
    false: "誤報（専門用語では誤検知）は、異常ではない時間に反応してしまうこと。",
    truth: "異常を入れた区間は、練習用データを作るときに異常を埋め込んだ時間帯で、答え合わせの正解として使っている。",
  };
  const LAYERS = [
    { key: "value", label: "センサーの値", sw: "line-ink" },
    { key: "range", label: "いつもの範囲の線", sw: "line-sigma", help: "range" },
    { key: "flow", label: "最近の流れの線と帯", sw: "band-moving", help: "flow" },
    ...(HAS_TRUTH ? [{ key: "truth", label: "異常を入れた区間", sw: "band-truth", help: "truth" }] : []),
  ];

  function erfc(x) { // 正規分布の外側の割合を求める近似式（Abramowitz & Stegun 7.1.26）
    const t = 1 / (1 + 0.3275911 * x);
    const y = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
    return y * Math.exp(-x * x);
  }

  // ---------- 3. 方法ごとの「見つけた・見逃し・誤報」の時間 ----------
  const byType = (k, type) => M[k].by_type.find((b) => b.type === type);
  const grade = (b) => (b.hits === b.total ? "full" : b.hits > 0 ? "part" : "none");
  const MARK = { full: "○", part: "△", none: "×" };
  const sets = {};
  const mismatch = [];
  KEYS.forEach((k) => {
    const missedTypes = M[k].by_type.filter((b) => b.hits === 0).map((b) => b.type);
    sets[k] = {
      all: P.filter((p) => p.flags[k]).map((p) => p.i),
      found: P.filter((p) => p.flags[k] && p.truth).map((p) => p.i),
      missed: P.filter((p) => p.truth && missedTypes.includes(p.truth.type)).map((p) => p.i),
      false: P.filter((p) => p.flags[k] && !p.truth).map((p) => p.i),
    };
    if (HAS_TRUTH && sets[k].false.length !== M[k].false_alarms) mismatch.push(UI[k].label);
  });

  const state = {
    show: Object.fromEntries(KEYS.map((k) => [k, true])),
    layer: Object.fromEntries(LAYERS.map((l) => [l.key, true])),
    selected: HAS_TRUTH ? TRUTH[0].type : null,
    hl: null, // { key, kind, idx: Set }
  };
  if (!HAS_TRUTH) state.layer.truth = false;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const names = (ks) => ks.map((k) => UI[k].label).join("と");
  const helpBtn = (key, about) => `<button type="button" class="help" data-help="${key}" aria-label="${about}の説明" aria-expanded="false">?</button>`;
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const windowOf = (t) => [Math.max(0, t.from - PAD), Math.min(N - 1, t.to + PAD)];
  const selectedTruth = () => TRUTH.find((t) => t.type === state.selected);

  // ---------- 4. 文章（すべて埋め込まれた数字から組み立てる） ----------
  const span = `${P[0].day}〜${P[N - 1].day}・1時間ごと ${N} 件`;
  el("subtitle").textContent = HAS_TRUTH
    ? `練習用の架空データ（NAB の温度センサーを参考に作成）・${span}`
    : `${R.source}・${span}`;
  el("days").textContent = `（${+(N / 24).toFixed(1)}日間）`;
  el("footer-note").innerHTML = (HAS_TRUTH
    ? `データは、<a href="https://github.com/numenta/NAB/blob/master/data/realKnownCause/machine_temperature_system_failure.csv">NAB（Numenta Anomaly Benchmark）の machine_temperature_system_failure.csv</a> の動き方に合わせて作った架空のデータです。`
    : `データ：${esc(R.source)}。`) +
    `画面の数字はすべて、dashboard.py が CSV から計算して、このファイルに埋め込んだものです。`;
  const perfect = KEYS.filter((k) => M[k].missed === 0);
  const worst = [...KEYS].sort((a, b) => M[b].false_alarms - M[a].false_alarms)[0];
  if (HAS_TRUTH) {
    el("lead1").textContent = perfect.length
      ? `${names(perfect)}は、${TRUTH.length}種類の異常をすべて見つけた。`
      : "どのチェックにも、見つけやすい異常と見逃す異常がある。";
    const caught = TRUTH.filter((t) => KEYS.some((k) => byType(k, t.type).hits > 0));
    el("insight2").textContent = caught.length === TRUTH.length
      ? (perfect.length
        ? `${names(perfect)}だけで、${TRUTH.length}種類の異常をすべて見つけられる。`
        : `${TRUTH.length}種類の異常は、1つのチェックでは見逃しが出るが、${KEYS.length}つを合わせるとすべて見つかる。`)
      : `${TRUTH.filter((t) => !caught.includes(t)).map((t) => TYPE[t.type].noun).join("と")}は、どのチェックでも見つからない。`;
    const worstFalse = sets[worst].false;
    const afterAnomaly = worstFalse.filter((i) => TRUTH.some((t) => i > t.to && i <= t.to + PAD));
    el("insight4").textContent = M[worst].false_alarms === 0
      ? "どのチェックにも誤報は出ていない。"
      : `誤報がいちばん多いのは${UI[worst].label}で、${worstFalse.length}件の${afterAnomaly.length === worstFalse.length ? "すべて" : `うち${afterAnomaly.length}件`}が、異常が終わってから${PAD}時間以内に出ている。`;
    el("note2").textContent = "グラフの上をなぞると、その時間の値と判定が出ます。灰色の区間をクリックすると、③で拡大します。";
    el("note4").textContent = "灰色の区間の中の印は正解、外の印は誤報です。枠は③で拡大している範囲です。";
  } else {
    el("lead1").textContent = "正解の区間がないため、見逃しと誤報は数えず、反応した時間だけを示す。";
    el("insight2").textContent = `反応した時間は、${KEYS.map((k) => `${UI[k].label} ${M[k].flagged}時間`).join("・")}。`;
    el("insight4").textContent = "正解の区間がないため、印が正しいかどうかは、②のグラフの形を見て判断する。";
    el("note2").textContent = "グラフの上をなぞると、その時間の値と、反応したチェックが出ます。";
    el("note4").textContent = "方法ごとに、反応した1時間を色の四角で並べています。";
    ["type-buttons", "box3", "note3"].forEach((id) => { el(id).hidden = true; });
    el("s3").querySelector("[data-legend]").hidden = true;
    el("insight3").textContent = "正解の区間がないため、拡大する異常はありません。";
  }

  function insight3(type) {
    const phrase = (b) => {
      const late = b.first_after_hours > 0 ? "途中から" : "";
      if (b.hits === b.total) return "すべての時間で反応する";
      return b.hits / b.total >= 0.5 ? `${late}大半の時間で反応する` : `${late}一部の時間だけ反応する`;
    };
    const groups = new Map();
    const none = [];
    KEYS.forEach((k) => {
      const b = byType(k, type);
      if (b.hits === 0) none.push(UI[k].label);
      else groups.set(phrase(b), [...(groups.get(phrase(b)) || []), UI[k].label]);
    });
    const pos = [...groups].map(([ph, ns]) => `${ns.join("と")}が${ph}`);
    const noun = TYPE[type].noun;
    if (!pos.length) return `${noun}は、どのチェックでも見つからない。`;
    const posText = pos.map((s, n) => (n < pos.length - 1 ? s.replace(/する$/, "し") : s)).join("、");
    return `${noun}は、${posText}${none.length ? `が、${none.join("と")}では見つからない` : ""}。`;
  }
  function note3(type) {
    const t = TRUTH.find((x) => x.type === type);
    const counts = KEYS.map((k) => {
      const b = byType(k, type);
      const when = b.hits ? `（${b.first_after_hours === 0 ? "最初の1時間" : `開始の${b.first_after_hours}時間後`}から）` : "";
      return `${UI[k].label} ${b.hits}/${b.total}時間${when}`;
    }).join("・");
    const after = KEYS.map((k) => [k, P.filter((p) => p.i > t.to && p.i <= t.to + PAD && p.flags[k] && !p.truth).length])
      .filter(([, n]) => n > 0).map(([k, n]) => `${UI[k].label} ${n}件`);
    return `反応した時間：${counts}。区間のあと${PAD}時間の誤報：${after.length ? after.join("・") : "なし"}。`;
  }

  // ---------- 5. ボタンとカードを作る ----------
  el("method-toggles").innerHTML = KEYS.map((k) =>
    `<button type="button" class="toggle" data-method="${k}" aria-pressed="true" aria-label="${UI[k].label}"><span class="sw sw-${UI[k].sw}" aria-hidden="true"></span><span>${UI[k].short}<span class="long">チェック</span></span></button>`).join("");
  document.querySelectorAll("[data-legend]").forEach((box) => {
    box.innerHTML = `<span class="note">表示の切り替え</span>` + LAYERS.map((l) =>
      `<span><button type="button" class="toggle" data-layer="${l.key}" aria-pressed="true"><span class="sw sw-${l.sw}" aria-hidden="true"></span>${l.label}</button>${l.help ? helpBtn(l.help, l.label) : ""}</span>`).join("");
  });
  el("type-buttons").innerHTML = TRUTH.map((t) =>
    `<button type="button" data-type="${t.type}" aria-pressed="${t.type === state.selected}">${TYPE[t.type].button}</button>`).join("");

  const stat = (kind, label, n, unit) =>
    `<button type="button" class="stat" data-kind="${kind}" aria-pressed="false"><span class="stat-label">${label}</span><span class="stat-num">${n}<span class="stat-unit">${unit}</span></span></button>`;
  const cardNote = {
    sigma: `いつもの範囲：${R.sigma.lower.toFixed(2)}〜${R.sigma.upper.toFixed(2)}${helpBtn("range", "いつもの範囲")}`,
    moving: `反応の目安：最近の流れから約${(S.k * R.moving_sd).toFixed(2)}以上ずれたとき`,
    flat: `反応の目安：${S.flat_hours}時間、値がまったく変わらないとき`,
  };
  el("cards").innerHTML = KEYS.map((k) => {
    const m = M[k];
    const chips = TRUTH.map((t) => {
      const g = grade(byType(k, t.type));
      return `<li class="chip ${g}">${MARK[g]} ${TYPE[t.type].button}</li>`;
    }).join("");
    const stats = HAS_TRUTH
      ? stat("found", "見つけた", TRUTH.length - m.missed, `/${TRUTH.length}種類`) + stat("missed", "見逃し", m.missed, "種類") + stat("false", "誤報", m.false_alarms, "件")
      : stat("all", "反応した", m.flagged, "時間");
    return `<article class="card" data-method="${k}">
      <h3 class="card-title"><span class="sw sw-${UI[k].sw}" aria-hidden="true"></span>${UI[k].label}</h3>
      <p class="card-desc">${UI[k].desc}${helpBtn(UI[k].help, UI[k].label)}</p>
      <div class="stats">${stats}</div>
      ${HAS_TRUTH ? `<ul class="chips" aria-label="異常の種類ごとの反応">${chips}</ul>` : ""}
      <p class="note">${cardNote[k] || ""}</p>
    </article>`;
  }).join("");
  el("card-legend").innerHTML = HAS_TRUTH
    ? `○ すべての時間で反応　△ 一部の時間だけ　× 反応なし　｜　見逃し${helpBtn("missed", "見逃し")}　誤報${helpBtn("false", "誤報")}　｜　数字を押すと、その時間が②③のグラフで光ります。`
    : "数字を押すと、その時間が②のグラフで光ります。";
  if (mismatch.length) el("hl-status").textContent = `注意：${mismatch.join("・")}の誤報の数が、成績と判定データで食い違っています。dashboard.py を実行し直してください。`;

  // ---------- 6. グラフ ----------
  const C = {};
  let narrow = false;
  let gutter = 60;
  const stripLabel = (k) => (narrow ? UI[k].short : UI[k].label);
  function readTheme() {
    const s = getComputedStyle(document.documentElement);
    ["ink", "sub", "muted", "line", "grid", "surface", "sigma", "moving", "flat", "flow-band", "band", "frame", "fs-n", "sans"]
      .forEach((n) => { C[n] = s.getPropertyValue(`--${n}`).trim(); });
    Chart.defaults.font.family = C.sans;
    Chart.defaults.font.size = parseFloat(C["fs-n"]);
    Chart.defaults.color = C.muted;
    narrow = el("box2").clientWidth < 560;
    const ctx = document.createElement("canvas").getContext("2d");
    ctx.font = `${C["fs-n"]} ${C.sans}`;
    gutter = Math.ceil(Math.max(...KEYS.map((k) => ctx.measureText(stripLabel(k)).width))) + 14;
  }

  // 目盛り：日付の変わり目（または6時間ごと）。幅が足りなければ間引く
  function ticksFor(a, b, step, chart) {
    const idx = [];
    for (let i = a; i <= b; i++) if (P[i].hour % step === 0) idx.push(i);
    const room = Math.max(1, chart.width - gutter - 20);
    const every = Math.max(1, Math.ceil((idx.length * (step === 24 ? 46 : 50)) / room));
    return idx.filter((_, n) => n % every === 0).map((value) => ({ value }));
  }
  const xScale = (a, b, zoom) => ({
    type: "linear", min: a - 0.5, max: b + 0.5,
    grid: { display: false }, border: { color: C.line },
    ticks: { color: C.muted, maxRotation: 0, autoSkip: false, callback: (v) => (P[v] ? (zoom ? [P[v].day, P[v].time] : P[v].day) : "") },
    afterBuildTicks: (ax) => { ax.ticks = ticksFor(a, b, zoom ? 6 : 24, ax.chart); },
  });
  const fixGutter = (ax) => { ax.width = gutter; };

  // 灰色の区間・選んだ範囲の枠・光る点・なぞった位置の線を描く
  const overlay = {
    id: "overlay",
    beforeDatasetsDraw(chart, args, o) {
      const { ctx, chartArea: A, scales: { x } } = chart;
      ctx.save();
      ctx.beginPath(); ctx.rect(A.left, A.top, A.width, A.height); ctx.clip();
      if (state.layer.truth) {
        ctx.fillStyle = C.band;
        TRUTH.forEach((t) => {
          const l = x.getPixelForValue(t.from - 0.5);
          const r = x.getPixelForValue(t.to + 0.5);
          ctx.fillRect(l, A.top, Math.max(2, r - l), A.height);
        });
      }
      if (o.frame && HAS_TRUTH) {
        const [a, b] = windowOf(selectedTruth());
        const l = x.getPixelForValue(a - 0.5);
        const r = x.getPixelForValue(b + 0.5);
        ctx.strokeStyle = C.frame; ctx.lineWidth = 1.5;
        ctx.strokeRect(l + 0.75, A.top + 0.75, Math.max(2, r - l - 1.5), A.height - 1.5);
      }
      ctx.restore();
      if (state.layer.truth && o.labels && !(o.labels === "all" && narrow)) {
        ctx.save();
        ctx.fillStyle = C.sub; ctx.font = `${C["fs-n"]} ${C.sans}`; ctx.textAlign = "center"; ctx.textBaseline = "bottom";
        TRUTH.filter((t) => o.labels === "all" || t.type === state.selected).forEach((t) => {
          const cx = x.getPixelForValue((t.from + t.to) / 2);
          if (cx >= A.left && cx <= A.right) ctx.fillText(TYPE[t.type].button, cx, A.top - 3);
        });
        ctx.restore();
      }
    },
    afterDatasetsDraw(chart, args, o) {
      const { ctx, chartArea: A, scales: { x, y } } = chart;
      if (o.kind === "value" && state.hl) {
        const col = C[state.hl.key];
        const wave = reduceMotion ? 0.5 : 0.5 + 0.5 * Math.sin(performance.now() / 260);
        const r = o.haloR + 3 * wave;
        ctx.save();
        ctx.beginPath(); ctx.rect(A.left, A.top, A.width, A.height); ctx.clip();
        state.hl.idx.forEach((i) => {
          if (i < o.lo || i > o.hi) return;
          const px = x.getPixelForValue(i);
          const py = y.getPixelForValue(P[i].value);
          ctx.globalAlpha = 0.16 + 0.2 * wave; ctx.fillStyle = col;
          ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2); ctx.fill();
          ctx.globalAlpha = 0.95; ctx.strokeStyle = col; ctx.lineWidth = 1.6;
          ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2); ctx.stroke();
        });
        ctx.restore();
      }
      if (chart.$hover != null) {
        const px = x.getPixelForValue(chart.$hover);
        ctx.save();
        ctx.strokeStyle = C.muted; ctx.globalAlpha = 0.7; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(px, A.top); ctx.lineTo(px, A.bottom); ctx.stroke();
        if (o.kind === "value") {
          ctx.globalAlpha = 1; ctx.fillStyle = C.ink; ctx.strokeStyle = C.surface; ctx.lineWidth = 1.5;
          ctx.beginPath(); ctx.arc(px, y.getPixelForValue(P[chart.$hover].value), 4, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        }
        ctx.restore();
      }
    },
  };

  function valueDatasets(a, b, zoom) {
    const seg = P.slice(a, b + 1);
    const line = (key) => seg.map((p) => ({ x: p.i, y: p[key] }));
    const marks = (k) => seg.filter((p) => p.flags[k]).map((p) => ({ x: p.i, y: p.value }));
    const s = zoom ? 1 : 0.6;
    return [
      { key: "value", layer: "value", type: "line", data: line("value"), borderColor: C.ink, borderWidth: zoom ? 1.8 : 1.3, order: 2 },
      { key: "su", layer: "range", type: "line", data: line("su"), borderColor: C.sigma, borderWidth: 1.3, order: 3 },
      { key: "sl", layer: "range", type: "line", data: line("sl"), borderColor: C.sigma, borderWidth: 1.3, order: 3 },
      { key: "ml", layer: "flow", type: "line", data: line("ml"), borderWidth: 0, borderColor: "transparent", order: 5 },
      { key: "mu", layer: "flow", type: "line", data: line("mu"), borderWidth: 0, borderColor: "transparent", backgroundColor: C["flow-band"], fill: "-1", order: 5 },
      { key: "ma", layer: "flow", type: "line", data: line("ma"), borderColor: C.moving, borderWidth: 1.3, order: 4 },
      { key: "sigma", mark: true, type: "scatter", data: marks("sigma"), pointStyle: "circle", pointRadius: 6.5 * s, pointBorderWidth: 1.6, pointBackgroundColor: "transparent", pointBorderColor: C.sigma, order: 1 },
      { key: "moving", mark: true, type: "scatter", data: marks("moving"), pointStyle: "circle", pointRadius: 3.2 * s + 0.4, pointBorderWidth: 0.8, pointBackgroundColor: C.moving, pointBorderColor: C.surface, order: 0 },
      { key: "flat", mark: true, type: "scatter", data: marks("flat"), pointStyle: "rect", pointRadius: 7.5 * s, pointBorderWidth: 1.6, pointBackgroundColor: "transparent", pointBorderColor: C.flat, order: 1 },
    ].map((d) => ({ pointRadius: 0, spanGaps: false, ...d, hidden: d.mark ? !state.show[d.key] : !state.layer[d.layer] }));
  }
  function valueOptions(a, b, zoom) {
    const ys = P.slice(a, b + 1).flatMap((p) => [p.value, p.sl, p.su]);
    const lo = Math.min(...ys);
    const hi = Math.max(...ys);
    const pad = Math.max(1, (hi - lo) * 0.08);
    return {
      animation: false, responsive: true, maintainAspectRatio: false, events: [],
      layout: { padding: { top: 20, right: 10 } },
      scales: {
        x: xScale(a, b, zoom),
        y: { min: Math.floor((lo - pad) / 5) * 5, max: Math.ceil((hi + pad) / 5) * 5, grid: { color: C.grid }, border: { display: false }, ticks: { color: C.muted, maxTicksLimit: 6 }, afterFit: fixGutter },
      },
      plugins: {
        legend: { display: false }, tooltip: { enabled: false },
        overlay: { kind: "value", lo: a, hi: b, labels: zoom ? "selected" : "all", frame: !zoom, haloR: zoom ? 11 : 7 },
      },
    };
  }
  function stripConfig() {
    const labels = KEYS.map(stripLabel);
    return {
      type: "bar",
      data: {
        labels,
        datasets: KEYS.map((k, n) => ({
          key: k,
          data: P.filter((p) => p.flags[k]).map((p) => ({ x: [p.i - 0.45, p.i + 0.45], y: labels[n] })),
          backgroundColor: C[k], borderWidth: 0, grouped: false, barPercentage: 1, categoryPercentage: 0.6,
          hidden: !state.show[k],
        })),
      },
      options: {
        indexAxis: "y", animation: false, responsive: true, maintainAspectRatio: false, events: [],
        layout: { padding: { top: 6, right: 10 } },
        scales: {
          x: xScale(0, N - 1, false),
          y: { type: "category", labels, grid: { display: false }, border: { display: false }, ticks: { color: C.sub }, afterFit: fixGutter },
        },
        plugins: { legend: { display: false }, tooltip: { enabled: false }, overlay: { kind: "strip", lo: 0, hi: N - 1, labels: false, frame: true, haloR: 0 } },
      },
      plugins: [overlay],
    };
  }

  let c2, c3, c4;
  const charts = () => [c2, c3, c4].filter(Boolean);
  const valueCharts = () => [c2, c3].filter(Boolean);
  function build() {
    readTheme();
    charts().forEach((c) => c.destroy());
    c2 = new Chart(el("c2"), { type: "line", data: { datasets: valueDatasets(0, N - 1, false) }, options: valueOptions(0, N - 1, false), plugins: [overlay] });
    if (HAS_TRUTH) {
      const [a, b] = windowOf(selectedTruth());
      c3 = new Chart(el("c3"), { type: "line", data: { datasets: valueDatasets(a, b, true) }, options: valueOptions(a, b, true), plugins: [overlay] });
    }
    c4 = new Chart(el("c4"), stripConfig());
  }
  function refreshVisibility() {
    valueCharts().forEach((c) => c.data.datasets.forEach((d) => { d.hidden = d.mark ? !state.show[d.key] : !state.layer[d.layer]; }));
    c4.data.datasets.forEach((d) => { d.hidden = !state.show[d.key]; });
    charts().forEach((c) => c.update());
  }

  // ---------- 7. なぞったときの説明 ----------
  function tipHTML(i) {
    const p = P[i];
    const shown = KEYS.filter((k) => state.show[k]);
    const hit = shown.filter((k) => p.flags[k]);
    let status, cls;
    if (!HAS_TRUTH) { status = hit.length ? "反応あり（正解がないため、正しいかは不明）" : "反応なし"; cls = hit.length ? "miss" : "calm"; }
    else if (p.truth && hit.length) { status = `正解（${TYPE[p.truth.type].area}で反応）`; cls = "ok"; }
    else if (p.truth) { status = `${TYPE[p.truth.type].area}なのに反応なし`; cls = "miss"; }
    else if (hit.length) { status = "誤報（異常ではない時間に反応）"; cls = "ng"; }
    else { status = "異常ではない時間・反応なし"; cls = "calm"; }
    const who = hit.length
      ? hit.map((k) => `<span class="who"><span class="sw sw-${UI[k].sw}"></span>${UI[k].label}</span>`).join("")
      : "なし";
    return `<div class="tt-time">${p.day} ${p.time}</div>` +
      `<div>値 <b>${p.value.toFixed(2)}</b></div>` +
      `<div>反応した方法：${who}</div>` +
      `<div class="tt-status ${cls}">${status}</div>` +
      (shown.length < KEYS.length ? `<div class="tt-note">表示中のチェックだけを数えています</div>` : "");
  }
  function bindPointer(id, getChart, canPick) {
    const canvas = el(id);
    const box = canvas.parentElement;
    const tt = box.querySelector(".tt");
    let lastType = "mouse";
    const hitAt = (ev) => {
      const ch = getChart();
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      const A = ch.chartArea;
      if (x < A.left || x > A.right || y < A.top || y > A.bottom) return null;
      const o = ch.options.plugins.overlay;
      const i = Math.round(ch.scales.x.getValueForPixel(x));
      return i < o.lo || i > o.hi ? null : { i, x: x + canvas.offsetLeft, y: y + canvas.offsetTop };
    };
    const hide = () => {
      const ch = getChart();
      if (ch.$hover != null) { ch.$hover = null; ch.draw(); }
      tt.hidden = true;
    };
    const show = (ev) => {
      lastType = ev.pointerType || "mouse";
      const hit = hitAt(ev);
      if (!hit) return hide();
      const ch = getChart();
      if (ch.$hover !== hit.i) { ch.$hover = hit.i; ch.draw(); }
      tt.innerHTML = tipHTML(hit.i);
      tt.hidden = false;
      const w = tt.offsetWidth;
      const h = tt.offsetHeight;
      let left = hit.x + 16;
      if (left + w > box.clientWidth) left = hit.x - 16 - w;
      let top = hit.y - h - 14;
      if (top < 0) top = Math.min(hit.y + 16, box.clientHeight - h);
      left = Math.min(Math.max(0, left), box.clientWidth - w);
      tt.style.transform = `translate(${left}px, ${Math.max(0, top)}px)`;
    };
    canvas.addEventListener("pointermove", show);
    canvas.addEventListener("pointerdown", show);
    canvas.addEventListener("pointerleave", hide);
    canvas.addEventListener("click", (ev) => {
      if (!canPick || !HAS_TRUTH || lastType !== "mouse") return;
      const hit = hitAt(ev);
      if (hit && P[hit.i].truth) { select(P[hit.i].truth.type); scrollToSection("s3"); }
    });
  }

  // ---------- 8. ボタンの動き ----------
  function setPressed(selector, pressed) { document.querySelectorAll(selector).forEach((b) => b.setAttribute("aria-pressed", String(pressed))); }
  function scrollToSection(id) {
    const top = el(id).getBoundingClientRect().top;
    if (top < 0 || top > window.innerHeight * 0.55) {
      el(id).scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    }
  }
  function toggleMethod(k) {
    state.show[k] = !state.show[k];
    setPressed(`[data-method="${k}"].toggle`, state.show[k]);
    if (!state.show[k] && state.hl && state.hl.key === k) highlight(null);
    refreshVisibility();
  }
  function toggleLayer(key) {
    state.layer[key] = !state.layer[key];
    setPressed(`[data-layer="${key}"]`, state.layer[key]);
    refreshVisibility();
  }
  function select(type) {
    if (!HAS_TRUTH) return;
    state.selected = type;
    document.querySelectorAll("#type-buttons button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.type === type)));
    const [a, b] = windowOf(selectedTruth());
    c3.data.datasets = valueDatasets(a, b, true);
    c3.options = valueOptions(a, b, true);
    c3.$hover = null;
    c3.update();
    c2.draw();
    c4.draw();
    el("insight3").textContent = insight3(type);
    el("note3").textContent = note3(type);
  }
  let raf = 0;
  let lastFrame = 0;
  function pulse(t) {
    if (!state.hl) { raf = 0; return; }
    if (t - lastFrame > 50) { valueCharts().forEach((c) => c.draw()); lastFrame = t; }
    raf = requestAnimationFrame(pulse);
  }
  function highlight(key, kind) {
    document.querySelectorAll(".stat").forEach((b) => b.setAttribute("aria-pressed", "false"));
    const status = el("hl-status");
    if (!key || (state.hl && state.hl.key === key && state.hl.kind === kind)) {
      state.hl = null;
      status.textContent = "";
      valueCharts().forEach((c) => c.draw());
      return;
    }
    const idx = sets[key][kind];
    const label = UI[key].label;
    if (!idx.length) {
      state.hl = null;
      status.textContent = kind === "missed" ? `${label}の見逃しはありません。`
        : kind === "false" ? `${label}の誤報はありません。`
        : kind === "all" ? `${label}が反応した時間はありません。`
        : `${label}が正しく反応した時間はありません。`;
      valueCharts().forEach((c) => c.draw());
      return;
    }
    if (!state.show[key]) toggleMethod(key);
    state.hl = { key, kind, idx: new Set(idx) };
    document.querySelector(`.card[data-method="${key}"] .stat[data-kind="${kind}"]`).setAttribute("aria-pressed", "true");
    const missedNames = M[key].by_type.filter((b) => b.hits === 0).map((b) => TYPE[b.type].button).join("・");
    const what = kind === "found" ? `${label}が正しく反応した <b>${idx.length}時間</b>`
      : kind === "missed" ? `${label}が見逃した区間（${missedNames}）の <b>${idx.length}時間</b>`
      : kind === "all" ? `${label}が反応した <b>${idx.length}時間</b>`
      : `${label}の誤報 <b>${idx.length}件</b>`;
    status.innerHTML = `<span>${what}を、${HAS_TRUTH ? "②③" : "②"}のグラフで光らせています。</span><button type="button" class="link-btn" id="hl-clear">光を消す</button>`;
    if (reduceMotion) valueCharts().forEach((c) => c.draw());
    else if (!raf) raf = requestAnimationFrame(pulse);
    scrollToSection("s2");
  }

  // ? の説明
  const pop = el("pop");
  let popFor = null;
  function closePop() {
    if (!popFor) return;
    popFor.setAttribute("aria-expanded", "false");
    popFor.removeAttribute("aria-describedby");
    popFor = null;
    pop.hidden = true;
  }
  function openPop(btn) {
    if (popFor === btn) return closePop();
    closePop();
    pop.textContent = HELP[btn.dataset.help];
    pop.hidden = false;
    popFor = btn;
    btn.setAttribute("aria-expanded", "true");
    btn.setAttribute("aria-describedby", "pop");
    const r = btn.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    const left = Math.max(8, Math.min(r.left + r.width / 2 - pop.offsetWidth / 2, vw - pop.offsetWidth - 8));
    pop.style.left = `${left + window.scrollX}px`;
    pop.style.top = `${r.bottom + 8 + window.scrollY}px`;
  }

  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button");
    if (!btn) return closePop();
    if (btn.classList.contains("help")) return openPop(btn);
    closePop();
    if (btn.id === "hl-clear") highlight(null);
    else if (btn.dataset.method) toggleMethod(btn.dataset.method);
    else if (btn.dataset.layer) toggleLayer(btn.dataset.layer);
    else if (btn.dataset.type) select(btn.dataset.type);
    else if (btn.dataset.kind) highlight(btn.closest(".card").dataset.method, btn.dataset.kind);
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closePop(); });
  window.addEventListener("scroll", closePop, { passive: true });

  // ---------- 9. はじめの表示 ----------
  build();
  if (HAS_TRUTH) select(state.selected);
  bindPointer("c2", () => c2, true);
  if (HAS_TRUTH) bindPointer("c3", () => c3, false);
  bindPointer("c4", () => c4, true);

  // 幅が大きく変わったとき・明暗が変わったときは描き直す
  let resizeTimer = 0;
  const rebuild = () => { build(); if (HAS_TRUTH) select(state.selected); };
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if ((el("box2").clientWidth < 560) !== narrow) rebuild(); }, 150);
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rebuild);

  // 動作確認用（画面には出ない）
  window.__dashboard = { state, sets, charts: () => ({ c2, c3, c4 }), P, TRUTH, R };
})();
</script>
</body>
</html>
"""


# 実行：読み込み → 検出 → HTML作成 の順に動かし、結果を画面にも出す
def main():
    if len(sys.argv) != 2:
        sys.exit("使い方: python3 dashboard.py data.csv")
    path = Path(sys.argv[1])
    df = load(path)
    values = df["value"]
    df["sigma"], sigma_info = sigma_check(values)
    df["moving"], df["ma"], ma_sd = moving_check(values)
    df["flat"] = flat_check(values)
    df["ma_lower"], df["ma_upper"] = df["ma"] - K * ma_sd, df["ma"] + K * ma_sd
    truth = answers_for(df)
    results = {key: score(df, df[key], truth) for key in METHODS}
    build_html(path.name, df, sigma_info, ma_sd, truth, results)
    print(f"{path.name}: {len(df)}時間ぶんを調べました（正解の区間 {len(truth)}つ）")
    for key, r in results.items():
        detail = f"・見逃し {r['missed']}種類・誤報 {r['false_alarms']}件" if truth else ""
        print(f"  {METHODS[key]}: 反応 {r['flagged']}時間{detail}")
    print(f"保存: {OUTPUT.name}")


if __name__ == "__main__":
    main()
