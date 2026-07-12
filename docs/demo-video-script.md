# 2分58秒 デモ動画台本

## 勝ち筋

機能一覧ではなく、次の一つの変化を見せる。

> 5つのAIで解析しても、証拠がなければ原因にしない。
> 不足証拠が追加されたときだけ、判断を変える。

タイトル:

```text
5つのAIで解析しても、証拠がなければ原因にしない
Ops Evidence Synthesis
```

## 使用URL

- Runtime Code Profile: https://ops-evidence.yukimurata0421.dev/code-profiles/31dd5326f0e9e052697975e7174d9de6ebf7c2fde58625cb96ce41f29faab621/
- Runtime Full Review: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471
- Fast GCP Review: https://ops-evidence.yukimurata0421.dev/ui/fast-gcp-review
- Verified Fast Review: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=2641cb5fe5850d006864dec4aad3b3d2539e9efcef3753b43d5624f8b6e5136b
- More Data Rescore: https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore

## 構成

| 時間 | 画面 | 審査員に残す情報 |
| --- | --- | --- |
| 0:00-0:12 | Runtime Full Review | AIの同意は原因の証明ではない |
| 0:12-0:28 | 問題提起 | 機密ログと証拠不足の誤診 |
| 0:28-0:45 | Agent Trace | 調査、証拠要求、人間ゲート |
| 0:45-1:18 | Code Profile | Geminiによる意味理解と人間承認 |
| 1:18-1:53 | Runtime Review | 45,000行を原因に自動昇格しない |
| 1:53-2:24 | Fast GCP Review | Cloud Runから実Geminiを実行 |
| 2:24-2:47 | More Data Rescore | 追加証拠で判断状態が変わる |
| 2:47-2:58 | End Card | つくる、まわす、とどける |

## 0:00-0:12 完成形から始める

Runtime Full ReviewでEvidence、Counter Evidence、Missing Evidenceが見える位置を表示する。

ナレーション:

> 複数のAIが同じ仮説を支持しても、証拠が不足していればOESは原因にしません。
> これはAIの回答ではなく、SREが安全に判断するための証拠付きレビューです。

字幕:

```text
Agreement != Cause
AIの同意は、原因の証明ではない
```

## 0:12-0:28 課題を二つに絞る

画面:

```text
1. Raw logs may contain secrets
2. AI can sound certain with incomplete evidence
```

ナレーション:

> 障害対応AIには二つの危険があります。機密情報を含むraw logを外部へ出せないこと。
> そして、証拠が足りなくても、もっともらしく原因を断定できてしまうことです。

## 0:28-0:45 Agent Traceを見せる

Runtime Review内の `Agent Trace - ADK tool contract` を表示し、次を順に指す。

```text
freeze_evidence_bundle
run_cross_check_providers
chunk_and_merge_full_corpus
validate_citations
compute_review_targets
```

ナレーション:

> OESは証拠を固定し、モデルを照合し、引用を検証してReview Targetを作ります。
> 不足証拠は次の調査へ送り、最終判断だけは人間へ戻します。

字幕: `Guarded Autonomy - 調査はAgent、最終責任は人間`

## 0:45-1:18 GeminiとHuman Gate

Runtime Code Profileを次の順にスクロールする。

1. Gemini System Reading
2. Gemini Questions For Human Approval
3. 回答・正規化・再レビューを示す短い字幕
4. Runtime ReviewのProfile context

ナレーション:

> ログ解析の前にGemini 3.1 Proがサニタイズ済みコードを読み、正常と異常の意味を人間に質問します。
> 自然言語の回答をGeminiが候補JSONへ変換し、人間の再確認後にSHAで固定します。承認後はソースを再参照しません。

Code Profileの承認フォームは空欄のまま撮影し、入力やtokenを録画しない。次の承認済み実行値を字幕で表示してから、Runtime ReviewのProfile contextへ切り替える。

```text
Approved profile SHA256:
77ceaa551a41d4a9e24fa3533de0bfe7df1f17a56702d6ed13e1e6b5342ce709

profile_id: stream_v3_runtime_source_approved_20260711
mode: approved_profile_context
source context supplied after approval: no
```

## 1:18-1:53 45,000行の実レビュー

Runtime Reviewの概要から `youtube_health` のValidation Targetへ移動する。

字幕:

```text
45,000 input rows
45,000 sanitized events
1,035 Evidence Items
5 real AI providers
Raw log upload: 0
```

ナレーション:

> 実際の配信システムでは、45,000行を全件サニタイズし、1,035個のEvidence Itemを5つの実APIで解析しました。
> 復旧仮説は支持されていますが、配信状態とユーザー影響が不足しています。そのため原因にせず、Validation Targetへ回します。
> 0.74は原因確率ではなく、レビュー優先度です。

## 1:53-2:24 Cloud Runから実Geminiを動かす

Fast GCP Reviewで次を操作する。

1. `Load Sanitized Code Summary`
2. `Run Live Fast Review`
3. 進捗表示
4. 完了後のReviewリンク

画面で確認する値:

```text
Logic: source-approved-evidence-v2
Rows: 2,000
Model: gemini-3.1-flash-lite
Raw logs: not_uploaded
```

ナレーション:

> これはstream_v3専用ではありません。同じAgentを別の通知システムへ適用します。
> 今、Cloud RunからVertex Gemini Flash Liteを実行しています。
> 入力は固定された2,000行のサニタイズ済み証拠だけで、raw logは送信しません。
> 実測約14秒で、schema-validなReview URLが生成されます。

実行画面は別撮りし、実際の待ち時間を8-10秒へ編集する。Cross-checkは約232秒かかるため動画内では押さない。

## 2:24-2:47 不足証拠で判断が変わる

More Data RescoreでBeforeを表示し、`Run Fixed Rescore` を押してAfterへ切り替える。

ナレーション:

> ユーザー影響が不足すると、Agentは追加証拠を要求します。
> child Evidence Bundleが届くと、needs more dataからevidence collectedへ進み、Primary Candidateへ再評価します。
> 最終原因と運用操作は、まだ人間の承認対象です。

字幕:

```text
needs_more_data -> evidence_collected -> re-scored
```

## 2:47-2:58 締め

画面:

```text
Gemini / Vertex AI
Cloud Build -> Cloud Run
SHA-fixed Evidence
Human-gated Action
```

ナレーション:

> AIは原因仮説を作れます。OESは、その仮説を人間が安全に判断できる、再現可能な証拠へ変換します。

最終字幕:

```text
AIに原因を当てさせるのではなく、
原因と呼べる証拠を集めさせる。
```

## 撮影ルール

- 一発撮りにせず、各画面を別録りして編集する。
- 1920x1080、ブラウザ倍率125-150%、全編字幕付きにする。
- Fast GCP Reviewだけ実ライブ実行を見せる。
- JSON全体、長いターミナル、API待機ログは見せない。
- `0 Primary` は失敗ではなく「証拠不足で止まれる」強みとして説明する。
- スコアを原因確率と説明しない。
- Source Profileをruntime evidenceと説明しない。
- Agent Engineへデプロイ済みとは説明しない。
- system切替時は「汎用性の証明」と明言する。

## 撮影前検証

```bash
make smoke-demo-video
```

このコマンドがCode Profile、Runtime Review、Fast Review実行履歴、More Data Rescoreを公開環境で検証してから撮影する。
