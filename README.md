# AI秘書

ICレコーダーに話した一日の予定をWhisperとLLMで時間割に変換し、Androidタブレットなどに表示する個人用AI秘書システムである。
予定時刻が近づくとVOICEVOXで知らせ、音声で予定の確認・追加・変更・完了を行える。
OpenAI APIとllama.cppによるローカルLLMの両方に対応している。

本ソフトウェアは、作者自身が日常的に使用しながら開発しているプロトタイプである。
以下の環境や手順は作者の動作確認例であり、記載したOS・ライブラリのバージョン以外で動作しないことを意味するものではない。
また、すべての環境での動作を保証するものでもない。

## 主な機能

- ICレコーダーの録音をWhisperで文字起こしし、LLMでMarkdownと予定JSONを生成。
- 予定をSQLiteのデータベースに保存し、HTTPサーバーから各クライアントへ配信。
- Android、Windows、UbuntuのFletクライアントで時間割と秘書キャラクターを表示。
- faster-whisperで音声を認識し、OpenAI APIまたはローカルLLMで会話。
- VOICEVOXによる音声応答、口パク表示、予定開始前のリマインダー。
- GUIと音声による予定の追加・時間変更・完了記録。
- 一括起動スクリプトによるサーバー群の起動と、ICレコーダーから予定DBまでの一括更新。

## システム構成

```text
SONY ICD-PX240などのICレコーダー
  └─ 録音ファイル
       ↓
Ubuntuサーバー
  ├─ Whisper large-v2 → 文字起こし
  ├─ LLM → 予定Markdown / JSON
  ├─ SQLite ←→ 時間割HTTPサーバー（8766）
  ├─ 音声会話サーバー（50000）
  │    ├─ faster-whisper small
  │    ├─ OpenAI API または llama-server（8080）
  │    └─ VOICEVOX（50021）
  │
  └─ LAN / Wi-Fi
       ├─ Androidタブレット（メイン）
       ├─ Windows 11
       └─ Ubuntu
          Flet時間割・キャラクター・音声入出力
```

### 作者の動作確認済み環境

以下の環境は作者の動作確認例であり、記載したOS・ライブラリのバージョン以外で動作しないことを意味するものではない。

| 用途 | 作者の環境・使用技術 |
| --- | --- |
| サーバー | Ubuntu 24.04 LTS + NVIDIA GPU |
| Python | プロジェクトの指定は3.12以上。作者の厳密な実行バージョンは公開前に確認 |
| メインクライアント | Androidタブレット |
| その他のクライアント | Windows 11 / Ubuntu |
| クライアントUI | Flet 0.85.3 |
| 音声認識 | Whisper / faster-whisper |
| 音声合成 | VOICEVOX |
| LLM | OpenAI API または llama.cpp |

### リポジトリの主なファイル

| ファイル | 役割 |
| --- | --- |
| `start_ai_secretary.py` | サーバー群の一括起動 |
| `update_schedule_from_ic.py` | 録音から予定DBへの一括更新 |
| `ic_recorder/ic_recorder_schedule.py` | 文字起こし・予定Markdown/JSON生成 |
| `schedule/json_to_schedule_sqlite.py` | JSONをSQLiteへ登録 |
| `schedule/schedule_http_server.py` | 予定配信・編集API |
| `ai_host/voice_roundtrip_server_secretary.py` | 音声会話・音声合成・予定操作 |
| `src/main.py` | Fletクライアント |
| `src/audio_backend.py` | OS別の音声入出力 |
| `src/vad_recorder.py` | 発話区間検出 |
| `config.example.toml` | 環境設定の公開サンプル |
| `generate_client_config.py` | APK用設定の生成 |

## 最初に行う設定

プロジェクトのルートで、公開サンプルをコピーする。

```bash
$ cd ~/flet_ai_secretary
$ cp config.example.toml config.toml
```

`config.toml` を自分の環境に合わせて編集する。
個人用の設定ファイルはGitへコミットしないこと。

```toml
[paths]
llama_server = "~/llm/llama.cpp/build/bin/llama-server"
llama_model = "~/llm/model.gguf"
voicevox_run = "~/voicevox/run"

[ic_recorder]
directory = "/media/YOUR_USER/IC RECORDER/VOICE/A/"
file_pattern = "*.mp3"

[client]
server_host = "192.168.1.100"
voice_port = 50000
schedule_port = 8766

[services]
voicevox_url = "http://127.0.0.1:50021"
local_llm_base_url = "http://127.0.0.1:8080/v1"
schedule_url = "http://127.0.0.1:8766/today"
```

`~` はホームディレクトリを表す。
`YOUR_USER` やIPアドレスは自分の環境に置き換えること。
`[client]` のIPは、Androidなどから到達できるサーバーのLANアドレスである。
`127.0.0.1` はそのプログラム自身が動くマシンを指す。

`start_ai_secretary.py` と `ic_recorder_schedule.py` はこの設定を読み込む。
環境変数 `AI_SECRETARY_CONFIG` で別のTOMLファイルを指定することもできる。
ICレコーダーの場所は `IC_RECORDER_DIR`、検索パターンは `IC_RECORDER_PATTERN` で上書きできる。
既存の環境変数を優先する箇所もあるため、設定が反映されない場合は環境変数を確認すること。

## Android用クライアントの設定

Androidが現在のメイン・クライアントである。

なお、Android 以外に Ubuntu と  Windows 11 のクライアントも用意しているが、これらの設定については、後述する。

Python/Fletで作成したプログラムをAPKへビルドしてAndroidタブレット・スマホへインストールする。

### APKをビルドするUbuntu環境

Flet公式ドキュメント: https://docs.flet.dev/

Ubuntu 24.04でビルドする場合は、まず基本ツールを入れておく。

```bash
$ sudo apt update
$ pip install "flet==0.85.3" --break-system-packages
$ pip install "flet-cli==0.85.3" --break-system-packages
```
このプロジェクトではAndroidからAndroid APIを直接利用するため、

```toml
[tool.flet.android]
dependencies = [
    "pyjnius",
]
```

を `pyproject.toml` に設定している。

### APK作成

プロジェクトのルートディレクトリで、

```bash
$ cd ~/flet_ai_secretary

$ python3 generate_client_config.py
$ flet build apk
```

を実行する。

なお、初回ビルドは必要なツールをダウンロードするため、インターネット接続が必要である。

正常に終了すると、

```text
build/apk/
```

以下にAPKが生成される。

現在の設定では、

```text
build/apk/flet-schedule-secretary-voice.apk
```

が生成される。

### Android タブレットやスマホに APK をインストール

Andorid タブレット・スマホに入れる。

- Android タブレット・スマホに入れる。
- Android タブレット・スマホの設定・情報・ビルド番号を7回叩き
- Android側
   - 設定・接続済みのデバイス・USBでファイル転送に設定
- Ubuntu側
   - スマホアイコンを開く内部共有ストレージ・
   - Download
   - ここに、flet-schedule-secretary-voice.apk をコピー
   - スマホをアンマウント
- Android側
  - Files・ダウンロード
  - flet-schedule-secretary-voice.apk をインストール
   - スキャンを求められたら、スキャンして、インスト

## サーバー側 ― Ubuntu 24.04

サーバー側では、主に次の処理を行う。

* ICレコーダー音声のWhisper文字起こし
* LLMによる一日の予定作成
* SQLiteによる予定管理
* HTTPによる予定配信
* クライアントから送られた音声のfaster-whisperによる認識
* OpenAIまたはローカルLLMとの会話
* VOICEVOXによる音声合成

### Python環境

Ubuntu 24.04ではPython 3.12を使用できる。
プロジェクトの `pyproject.toml` はPython 3.12以上を要求する。

```bash
$ sudo apt update
$ sudo apt install python3 python3-pip ffmpeg
$ cd ~/flet_ai_secretary
```

### Pythonライブラリ

現在のサーバープログラムで直接使用している主な外部ライブラリは次の通り。

```bash
$ pip install \
      openai \
      openai-whisper \
      faster-whisper \
      numpy \
      requests \
      resampy \
      soundfile \
      nvidia-cublas-cu12 \
      'nvidia-cudnn-cu12==9.*' \
      --break-system-packages
```

### VOICEVOX

公式サイト: https://voicevox.hiroshiba.jp/
音声合成にはVOICEVOXを使用する。

```bash
$ mkdir -p ~/voicevox
$ cd ~/voicevox
$ wget https://github.com/VOICEVOX/voicevox_engine/releases/download/0.23.0/voicevox_engine-linux-cpu-x64-0.23.0.vvpp
$ sudo apt install 7zip
$ 7z x voicevox_engine-linux-cpu-x64-0.23.0.vvpp
```
個別に、サーバーを起動する場合には、下記のように VOICEVOX を起動する。
後述するように start_ai_secretary.py を使って、一括起動する場合は、VOICEVOX の個別起動は必要ない。

```bash
$ cd ~/voicevox
$ ./run 
```

## AI (LLM) 

AI秘書では、クラウド AI である OpenAI APIとローカルLLMの両方を使用できる。

### クラウド AI OpenAI APIを使う場合

事前に、OpenAI で API を設定して、API 用のキーを取得しておく必要がある。

```bash
$ export OPENAI_API_KEY="sk-proj-..."
```

を設定する。

個別起動の場合、

```bash
$ python3 ai_host/voice_roundtrip_server_secretary.py --llm openai
```

のように起動する。

後述するように start_ai_secretary.py を使って、一括起動する場合は、個別起動は必要ない。

### ローカルLLMを使う場合

ローカルLLMには、[llama.cpp](https://github.com/ggml-org/llama.cpp) を使う。

llama.cpp は、次のように構築する。

```bash
$ mkdir -p ~/llm
$ cd ~/llm
$ git clone  https://github.com/ggml-org/llama.cpp.git
$ cd ~/llm/llama.cpp/
$ sudo apt install nvidia-cuda-toolkit
$ sudo apt install build-essential cmake
$ cmake -B build -DGGML_CUDA=ON
$ cmake --build build --config Release -j 8
```

ローカルLLMのためには、GGUF形式のLLMモデル をダウンロードする必要がある。

ここでは、gpt-oss-20b-Q5_K_M.gguf を例にする。

使用する LLMモデルは、config.toml に、下記のように設定する。

```toml
[paths]
llama_server = "~/llm/llama.cpp/build/bin/llama-server"
llama_model = "~/llm/gpt-oss-20b-Q5_K_M.gguf"
voicevox_run = "~/voicevox/run"
```

ローカルLLMの個別起動例：

```bash
$ ~/llm/llama.cpp/build/bin/llama-server -m ~/llm/gpt-oss-20b-Q5_K_M.gguf \
   --jinja   --reasoning-format auto   --ctx-size 24576   --temp 1.0   --top-p 1.0 \
   -ngl 10   --host 0.0.0.0   --port 8080   --timeout 3600
```
使用する `llama-server` とGGUFモデルの場所は、環境に合わせて設定すること。

また、別ウィンドウで

```bash
$ python3 ai_host/voice_roundtrip_server_secretary.py
```


後述するように start_ai_secretary.py を使って、一括起動する場合は、ローカルLLMの個別起動は必要ない。

## 起動と日常の使い方

### ICレコーダーから時間割DBまで一括更新

ICレコーダーを接続・マウントし、設定したディレクトリから録音を読める状態にする。
`update_schedule_from_ic.py` は新規録音の処理、予定JSONの特定、SQLiteへの登録、時間割HTTPサーバーの起動をまとめて行う。

```bash
$ cd ~/flet_ai_secretary
$ python3 update_schedule_from_ic.py --llm local
```

OpenAI APIを使用する場合：

```bash
$ export OPENAI_API_KEY="sk-proj-XXXX"
$ python3 update_schedule_from_ic.py --llm openai
```

生成JSON名を手入力する必要はない。
今回の処理で出力された `[PLAN] written:` 行からファイルを特定し、更新時刻によるフォールバックも行う。
既に8766番ポートで時間割サーバーが起動している場合は二重起動しない。

```bash
# SQLiteへの登録までで止める
$ python3 update_schedule_from_ic.py --no-server
```

`--all-text` で全テキストを対象にする方法や、`--input-text` で既存の文字起こしを指定する方法もある。
詳細は `--help` を参照すること。

### サーバー群の一括起動

`start_ai_secretary.py` はVOICEVOX、時間割HTTPサーバー、音声会話サーバーをまとめて起動する。
ローカルLLMを選択した場合はllama-serverも起動する。
既に起動しているサービスは再利用する。

```bash
$ cd ~/flet_ai_secretary
$ python3 start_ai_secretary.py
# 明示する場合
$ python3 start_ai_secretary.py --llm local
```

OpenAI APIを使う場合は、APIキーを環境変数へ設定する。
実際のキーをREADMEやソースコードへ記載しないこと。

```bash
$ export OPENAI_API_KEY="sk-proj-XXXX"
$ python3 start_ai_secretary.py --llm openai
```

`Ctrl+C` でランチャーが起動した子プロセスをまとめて終了する。
既にVOICEVOXを別途起動している場合は `--no-voicevox` を使用できる。
`--llama-server`、`--llama-model`、`--voicevox-run` で起動ファイルやモデルを個別指定することもできる。

### 手動で各段階を実行する場合

通常は一括スクリプトを推奨するが、各処理を確認したい場合は個別実行もできる。
以下はOpenAI APIを使う例だ。

```bash
$ cd ~/flet_ai_secretary
$ export OPENAI_API_KEY="sk-proj-XXXX"
$ python3 ic_recorder/ic_recorder_schedule.py --llm openai
```

生成されたJSONのパスを確認し、実際のファイル名を指定して登録する。

```bash
$ cd ~/flet_ai_secretary/schedule
$ python3 json_to_schedule_sqlite.py   ~/whisper_ic_recorder/daily_plan/record_260906_001_plan.json
$ python3 schedule_http_server.py
```

別のターミナルで音声会話サーバーを起動する。

```bash
$ cd ~/flet_ai_secretary/ai_host
$ python3 voice_roundtrip_server_secretary.py
```

この例のJSON名は作者環境の参考例だ。
実際には今回生成されたファイルを指定すること。
VOICEVOXは別途起動しておく。
個別起動では必要な環境変数も各ターミナルへ設定すること。

### クライアントの起動

AndroidではビルドしたAPKをインストールし、サーバーと同じLANへ接続して起動する。
UbuntuとWindowsでは、各OSの環境構築手順に従い `src/main.py` を実行する。
サーバーが起動していない場合は音声会話や予定取得ができない。

## 予定管理と音声会話

### 予定の確認

音声会話サーバーは会話ごとに時間割HTTPサーバーの `/today` を読み、現在時刻と最新の予定をLLMへ渡す。
「今日の予定は？」「13時から何をする？」などの質問に、SQLiteに保存された予定を使って答える。

### 予定の追加

クライアントの「予定追加」からタイトル・開始時刻・終了時刻・メモを入力できる。
追加項目は `POST /item/add` へ保存され、開始時刻に応じて表示位置へ挿入される。

音声の例：

- 「10時30分から30分、 画像の整理作業を追加して」
- 「 画像の整理作業を10時30分から11時まで追加して」
- 「10時半から 画像の整理作業を30分追加して」

開始時刻を指定せず「 画像の整理作業を30分追加して」と話した場合は、意図しない時刻へ追加しないよう、開始時刻の指定を促す。

### 時間・内容の変更

GUIの編集画面ではタイトル、開始時刻、終了時刻、メモを変更できる。
開始・終了は独立して変更できるため、作業時間の短縮や延長も可能である。
変更は `POST /item/update` に保存される。

音声の例：

- 「 画像の整理作業を9時半までにして」
- 「 画像の整理作業の終了時刻を9時半に変更して」
- 「 画像の整理作業を9時30分から10時30分までに変更して」
- 「 画像の整理作業を1時間に短くして」
- 「 画像の整理作業を9時に変更して」

開始時刻だけを変更する場合は元の所要時間を維持する動作がある。
長い予定を分割したい場合は、元の予定を短縮してから新しい予定を追加する方法を想定している。

### 作業の完了

チェックボックスまたは「現在の作業完了」「今の作業終了」「画像の整理作業終了」などの音声で完了を記録できる。
`POST /item/complete` により完了状態と完了時刻をSQLiteへ保存する。
チェックを外すと未完了へ戻し、完了時刻をクリアする。

### リマインダー

クライアントは時間割サーバーを20秒ごとに確認し、音声で予約されたリマインダーが指定時刻になると一度だけVOICEVOXで知らせる。
確認周期は src/main.py の SCHEDULE_NOTIFY_POLL_SECONDS で変更できる。

会話の「停止」はマイクによる会話待受けを止めるもので、予定通知は独立して動作する。
したがって、会話停止中でもリマインダーは通知される。

## ICレコーダーと生成ファイル

作者が使用しているICレコーダーは **SONY ICD-PX240** だ。
USB接続で録音ファイルへアクセスし、音声ログの取り込みに利用している。
特定の機種専用ではない。

作者環境における録音ファイルの例：

```text
IC RECORDER/
└── VOICE/
    └── A/
        └── 260913_001.mp3
```

元ファイル名は日付と連番を含む `YYMMDD_NNN.mp3` 形式の例である。
`260913_001.mp3` は2026年9月13日の録音ファイルの例だ。

作者のマウント先は `/media/HOGE/IC RECORDER/VOICE/A/` だが、利用者のユーザー名・OS・機種・マウント方法によって異なる。
`config.toml` の `directory` と `file_pattern` を変更すること。
現在の実装では `*.mp3` を既定の検索パターンとしている。
WAVなど別形式への対応は、Whisperが読み込めることに加えて、取り込み処理側の対応も確認する必要がある。

`record_260913_001.mp3` のような名前は、AI秘書側で取り込み・処理したファイルの命名規則として、ICレコーダーの元ファイル名と区別する。
生成される予定JSONの例は `record_260913_001_plan.json` である。
日付・連番は実際の処理対象によって変わる。

### 文字起こしと予定生成

ICレコーダーの文字起こしにはOpenAI Whisperの `large-v2` を使用している。
文字起こし後、LLMが予定Markdownを生成し、JSONへ変換する。
Markdownを正本として保存する構成だ。
予定JSONはSQLiteへ取り込まれる。
`ic_recorder_schedule.py` は `--llm local`、`--llm openai`、`--llm none` を選択できる。
`--format md` でMarkdownのみ、`--format both` でMarkdownとJSONを出力する。
既存の文字起こしを使う `--input-text`、既存MarkdownをJSONへ変換する `--parse-markdown`、用語辞書のテンプレートを作る `--write-glossary-template` などもある。

## Android 以外のクライアント

### Ubuntu版クライアント

Ubuntu 24.04でもAndroid版とほぼ同じクライアントを実行できる。

Ubuntu版では音声入力・音声再生にALSAのパッケージをインストールする必要がある。

```bash
$ sudo apt update
$ sudo apt install alsa-utils
$ pip install "flet==0.85.3"
```

起動する。

```bash
$ python src/main.py
```

### Windows 11版クライアント

Windows 11でも同じクライアントを実行できる。

64bit版Python 3.12以降をインストールする。

PowerShellまたはコマンドプロンプトで、

```powershell
> cd flet_ai_secretary
> pip install "flet==0.85.3"
```

として環境を作る。

起動は、

```powershell
> python src\main.py
```

である。

## 設定・運用上の注意

### ネットワーク

標準設定ではサーバー側で、ポート番号の

```text
50000 : 音声会話サーバー
8766  : 時間割HTTPサーバー
50021 : VOICEVOX
8080  : llama-server（ローカルLLM使用時）
```

を使用する。

Android、Windows、Ubuntuの各クライアントから、AI秘書サーバーの50000番および8766番ポートへアクセスできる必要がある。

通常は、サーバーとクライアントを同じLANに接続して使用する。

現状では、HTTP通信を使用するため、信頼できるLAN内での利用を想定している。
インターネットへ直接ポートを公開しないこと。

### 環境変数

| 環境変数 | 用途 |
| --- | --- |
| `AI_SECRETARY_CONFIG` | 設定TOMLの場所 |
| `OPENAI_API_KEY` | OpenAI APIの認証キー |
| `IC_RECORDER_DIR` | 録音ファイルのディレクトリ |
| `IC_RECORDER_PATTERN` | 録音ファイルの検索パターン |
| `WHISPER_TEXT_DIR` | 文字起こし出力先 |
| `DAILY_PLAN_DIR` | 予定生成ファイルの出力先 |
| `SCHEDULE_URL` | 時間割サーバーの `/today` URL |
| `VOICEVOX_HOST` | VOICEVOX EngineのURL |
| `VOICEVOX_SPEAKER` | VOICEVOXの話者ID（既定30） |
| `LLM_BASE_URL` / `LOCAL_LLM_BASE_URL` | ローカルLLMの接続先 |
| `LLAMA_SERVER_BIN` / `LLM_MODEL_FILE` | ローカルLLMの実行ファイル・モデル |

設定ファイルと環境変数の優先順位はプログラムごとに異なる箇所がある。
起動スクリプトは設定値を子プロセス用の環境変数へ引き継ぐ。
ポート番号は一部の起動コマンドで固定されているため、TOMLの値だけを変更してもすべてのサービスの待受けポートが変わるわけではない。
標準ポート以外を使う場合は関連する起動引数・接続先も確認すること。

### Androidタブレットを常時表示する場合

AC電源につないで常設する場合は、Androidの開発者向けオプションにある「スリープモードにしない」を有効にすると便利だ。

1. Androidの開発者モードを有効にする。
2. 「設定」→「システム」→「開発者向けオプション」を開く。
3. 「スリープモードにしない」を有効にする。

AC電源接続中に画面を常時表示できる。
設定項目の名称や場所はAndroidのバージョン・端末メーカーによって異なる。
長時間の常時表示では発熱や画面の焼き付きなどにも注意すること。


## ライセンスと第三者ソフトウェア

本リポジトリでは、プログラムコードとイラストに異なるライセンスを適用している。

### プログラムコード

本リポジトリに含まれるプログラムコードは、**Apache License 2.0** のもとで公開している。

Copyright 2026 Atsushi Noda

ライセンスの詳細については、本リポジトリの `LICENSE` ファイルを参照してください。

### イラスト

本リポジトリに含まれるイラストは、特に明記されているものを除き、**Creative Commons Attribution-NonCommercial 4.0 International（CC BY-NC 4.0）** のもとで公開している。

Copyright 2026 Atsushi Noda

イラストの複製、配布、改変等は、CC BY-NC 4.0 の条件に従う限り可能です。利用する際は、作者名（Atsushi Noda）を表示してください。

商用目的での利用は、CC BY-NC 4.0 の許諾範囲には含まれません。商用利用を希望する場合は、作者から別途許可を得てください。

CC BY-NC 4.0 の詳細：
https://creativecommons.org/licenses/by-nc/4.0/

### 外部ソフトウェア・モデル

Flet、Whisper、faster-whisper、VOICEVOX、llama.cpp、GGUFモデルなどは、それぞれの権利者が定めるライセンスに従う。
本リポジトリのライセンスがこれらの第三者ソフトウェアやモデルのライセンスを変更するものではない。
VOICEVOXの音声・キャラクターの利用規約も別途確認すること。

## 開発状況・既知の制約

- 作者の日常使用を通じて改良中のプロトタイプだ。未確認のOS・GPU・ライブラリの組み合わせについて動作保証はない。
- 現在の音声会話サーバーはfaster-whisperをCUDA/float16で使用するため、現状のコードではNVIDIA GPU環境が必要。CPUフォールバックは今後の検討事項だ。
- Androidのサーバー接続先はビルド時に設定を埋め込む方式だ。アプリ内での永続的な設定変更は今後の課題である。
- 現在のサーバーは信頼できるLAN内での利用を想定している。認証や暗号化を備えたインターネット公開用サービスではない。
- ICレコーダーの保存先・命名規則・音声形式は機種依存である。すべての機種での動作は確認していない。
- 音声認識やLLMによる予定解釈には誤りがあり得る。重要な予定は画面と保存内容を確認すること。
- OpenAI APIを使用する場合はAPI利用料金が発生する。ローカルLLMでも計算資源や電力を使用する。

## 免責

本ソフトウェアは研究・個人利用を目的としたプロトタイプであり、予定の正確性、通知の確実性、特定の用途への適合性を保証するものではない。
重要な予定や安全に関わる判断を本ソフトウェアのみに依存しないこと。
正式なライセンスの無保証条項も参照すること。
