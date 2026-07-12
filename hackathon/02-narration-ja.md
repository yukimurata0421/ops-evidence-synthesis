# 改訂版3分台本

このファイルを音声収録の正本にします。読み上げ部分は約940文字、3分、平均約313文字/分です。OESは解析するAgent、`stream_v3`は解析対象です。見出しごとに別テイクで録り、文末に0.2〜0.4秒の無音を残してください。画面説明は読み上げず、`ナレーション`の引用部分だけを収録します。

## 0:00–0:18　製品と対象システムを明示

画面：

```text
Ops Evidence Synthesis (OES)

SRE向け
Evidence-grounded DevOps Incident Review Agent

Demo target:
stream_v3 — 24/7 YouTube Live Delivery System
```

使用素材：`assets/screenshots/00-title-card.png`

ナレーション：

> Ops Evidence Synthesis、OESは、SREの障害調査をAIの回答から証拠付きの判断へ変えます。対象のstream_v3は、航空機データの映像と音声を24時間YouTube Liveへ届けます。

## 0:18–0:36　stream_v3の構成と今回の問い

画面：

```text
ADS-B aircraft data
        ↓
Browser visualization + Program audio
        ↓
FFmpeg / RTMPS
        ↓
YouTube Live

systemd / watchdog
        └── monitoring and recovery
```

使用素材：`assets/screenshots/01-stream-v3-system.png`

ナレーション：

> stream_v3は、航空機データを映像化し、音声と合成してFFmpegからYouTubeへ配信します。systemdとwatchdogが監視、復旧します。問いは、正常な自己回復か、視聴者影響を伴う障害かです。

演出上の注意：ここでは「ログを解析します」ではなく、判断課題を具体的に提示します。

## 0:36–0:50　完成したReviewを先に見せる

画面：

- [Runtime Full Review](https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471)
- `Evidence`、`Counter Evidence`、`Missing Evidence`が見える位置
- 使用素材：`assets/screenshots/10-runtime-review-hero.png`

ナレーション：

> 5つのAIが解析しますが、OESは多数決で原因を決めません。支持、反証、不足証拠を、人間が判断できる形にします。

字幕：

```text
5 AI providers
Agreement ≠ Cause
```

字幕素材：`assets/screenshots/20-agreement-not-cause.png`

## 0:50–1:18　ログ解析前にシステムの意味を確定

画面：

- [Runtime Code Profile](https://ops-evidence.yukimurata0421.dev/code-profiles/31dd5326f0e9e052697975e7174d9de6ebf7c2fde58625cb96ce41f29faab621/)
- 0:50–1:01：`Gemini System Reading`
- 1:01–1:10：`Questions For Human Approval`と人間の自然言語回答
- 1:10–1:18：Gemini候補JSON、人間の再確認、承認SHA

使用素材：

- `assets/screenshots/14-code-profile-system-reading.png`
- `assets/screenshots/15-code-profile-human-questions.png`
- `assets/screenshots/07-human-semantics-gate.png`

ナレーション：

> ログの前に、Gemini 3.1 Proがサニタイズ済みコードから、配信経路とログの意味を推定します。ただし、AIに確定権限はありません。人間の回答を候補JSONにし、再確認して承認します。

強調表示：

```text
status: approved
source_access_after_approval: disabled
context_is_not_evidence: true
approved_profile_sha256: 77ceaa551a41…
```

ナレーション後半：

> 承認結果はSHAで固定し、以後のログ解析ではコードを再参照しません。

## 1:18–1:35　OESのAgent Trace

画面：Review画面の`Agent Trace · ADK tool contract`。

```text
freeze_evidence_bundle
run_cross_check_providers
validate_citations
compute_review_targets
request_more_evidence
arbitrate_review_gate
```

使用素材：`assets/screenshots/11-runtime-agent-trace.png`

ナレーション：

> OESは証拠を固定し、モデルを照合し、引用を検証してReview Targetを作ります。不足証拠を要求し、最終判断の前で人間へ戻します。

字幕：

```text
Guarded Autonomy
Investigation by Agent
Final decision by Human
```

字幕素材：`assets/screenshots/22-guarded-autonomy.png`

## 1:35–2:03　45,000行の実解析

画面：Runtime Reviewの集計から`youtube_health`カードへ移動します。

使用素材：

- `assets/screenshots/12-runtime-target.png`
- `assets/screenshots/21-runtime-metrics.png`

字幕：

```text
45,000 input rows
45,000 sanitized events
1,035 Evidence Items
5 real AI providers
Raw rows prompted: 0
```

ナレーション：

> 45,000行を全件サニタイズし、1,035のEvidence Itemを5つの実APIで解析しました。4モデルは低い送信速度から停滞を疑いましたが、同時にhealthyとstream activeもあります。視聴者影響の証拠がないため、Validation Targetに止めます。

スコアを示しながら：

> スコアは原因確率ではなく、SREの確認優先度です。

## 2:03–2:28　別システムで汎用性とライブ実行を証明

画面：

- 2:03–2:08：`stream_v3 → amazon-notify`の切り替え画面
- 2:08–2:28：[Fast GCP Review](https://ops-evidence.yukimurata0421.dev/ui/fast-gcp-review)

ナレーション：

> OESは汎用です。次は通知システムamazon-notifyへ適用します。

操作：

```text
Load Sanitized Code Summary
Run Live Fast Review
進捗表示
完了後のReviewリンクを開く
```

ナレーション：

> Cloud Run上のOESがAgent Platform APIからGemini 3.1 Flash-Liteを呼びます。2,000行の固定済み証拠だけを使い、raw logは送らず、約14秒でReviewを作ります。

画面字幕：

```text
Cloud Run → Gemini Enterprise Agent Platform API
Model Garden · Gemini 3.1 Flash-Lite
2,000 sanitized rows
Raw log policy: not_uploaded
Schema-valid: 1/1
```

注意：`Run Live Cross-check`は約232秒かかるため、動画内では実行しません。

## 2:28–2:50　不足証拠による再評価

画面：[More Data Rescore](https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore)

- Beforeを表示
- `Run Fixed Rescore`を実行
- Afterへ切り替え

使用素材：

- `assets/screenshots/18-rescore-before.png`
- `assets/screenshots/19-rescore-after.png`
- `assets/screenshots/23-rescore-transition.png`

ナレーション：

> ユーザー影響が不足すると、OESは追加証拠を要求します。child Evidence Bundleを加えると判断状態が進み、Validation TargetをPrimary Candidateへ再評価します。

字幕：

```text
needs_more_data
        ↓
evidence_collected
        ↓
validation_target → primary_candidate
```

ナレーション後半：

> 最終判断は人間です。

## 2:50–3:00　締め

画面：

```text
Gemini / Agent Platform API
Cloud Build
Cloud Run
SHA-fixed Evidence
Human-gated Action
```

使用素材：`assets/screenshots/05-end-card.png`

ナレーション：

> AIは原因仮説を作れます。OESは、その仮説を人間が安全に判断できる、再現可能な証拠へ変換します。

最後の画面：

```text
Ops Evidence Synthesis (OES)

AIに原因を当てさせるのではなく、
原因と呼べる証拠を集めさせる。
```

## 表現上の注意

- OESは解析するAgent、`stream_v3`は解析対象です。「OESをOESで解析する」と誤解させないでください。
- `amazon-notify`へ移るときは「汎用性の証明」と明言します。
- 最初の一回だけ正式名称を読み、その後はOESと呼びます。
- 0 Primaryは失敗ではなく、証拠不足で止まれる安全性として説明します。
- スコアを原因確率と呼びません。
- Cross-checkは動画内で実行せず、Fast GCP ReviewのGemini単体だけをライブ実行します。
- `Cloud Run → Vertex Gemini`ではなく、実装どおり`Cloud Run → Gemini Enterprise Agent Platform API → Model Garden`と表現します。

## 読み方

- OES：オー・イー・エス
- ADS-B：エー・ディー・エス・ビー
- SHA：シャー、またはエス・エイチ・エーで統一
- stream_v3：ストリーム・ブイ・スリー
- amazon-notify：アマゾン・ノーティファイ
- Gemini Enterprise Agent Platform：ジェミナイ・エンタープライズ・エージェント・プラットフォーム
