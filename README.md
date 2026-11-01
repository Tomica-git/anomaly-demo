# anomaly-demo：センサーの値から「いつもと違う時間」を、統計の3つの方法で見つけて見比べるダッシュボード

機械学習を始める前に、「データを見て、統計で異常を見つける」流れを体験するための練習用デモです。

## ダッシュボード

URL：（あとで入れる）

## 動かし方

```bash
python3 dashboard.py data.csv
```

`dashboard.html` ができます。データとグラフの道具（Chart.js）を中に埋め込んでいるので、このファイル1つをブラウザで開くだけで見られます。

- 必要なもの：Python 3 と pandas（`pip install -r requirements.txt`）
- 実行のたびに Chart.js を jsDelivr から取得するため、インターネット接続が必要です
- 自分のCSV（`timestamp`, `value` の2列）でも試せます。1時間ごとでないデータは、1時間ごとの平均にそろえて調べます。正解の区間がないので、見逃しと誤報は数えず、反応した時間だけを表示します

## 3つの方法

| 画面の名前 | 中身 |
|---|---|
| 範囲チェック | 最初の3日間から決めた「平均±3σ」の外に出たら反応 |
| 流れチェック | 直前24時間の平均（移動平均）から急に離れたら反応 |
| 停止チェック | 3時間、値がまったく変わらなければ反応 |

期間やしきい値は `dashboard.py` の先頭の設定値で変えられます。

## ファイル

| ファイル | 中身 |
|---|---|
| `dashboard.py` | 読み込み → 検出 → HTML作成 を1本で行う |
| `dashboard.html` | できあがったダッシュボード（1ファイルで動く） |
| `data.csv` | 練習用の架空データ（1時間ごと336件、異常4種類入り） |
| `requirements.txt` | 必要なライブラリ |

## データについて

`data.csv` は、[NAB（Numenta Anomaly Benchmark）の machine_temperature_system_failure.csv](https://github.com/numenta/NAB/blob/master/data/realKnownCause/machine_temperature_system_failure.csv) の値の動き方に合わせて作った架空のデータです。実際の値ではありません。
