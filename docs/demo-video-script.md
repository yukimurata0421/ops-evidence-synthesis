# 3分30秒デモ動画台本

機能一覧ではなく、「1件の障害判断が、人間による意味付けから証拠付きの
レビュー判断へ変わる」流れを見せる。

## 使用URL

- Runtime Code Profile: https://ops-evidence.yukimurata0421.dev/code-profiles/31dd5326f0e9e052697975e7174d9de6ebf7c2fde58625cb96ce41f29faab621/
- Runtime Review: https://ops-evidence.yukimurata0421.dev/reviews/a7fc02ea095516eaaed07f4599c3e25f94d092163ed163efccfb6f0300ee50e0/
- Runtime Full Review: https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=a7fc02ea095516eaaed07f4599c3e25f94d092163ed163efccfb6f0300ee50e0
- Runtime Review Graph: https://ops-evidence.yukimurata0421.dev/ui/review-graph?evidence_sha256=a7fc02ea095516eaaed07f4599c3e25f94d092163ed163efccfb6f0300ee50e0
- Monitoring Review: https://ops-evidence.yukimurata0421.dev/reviews/8d165418fca88f856d8525bbdae804b6b649455450796b2dc44d2134b21abd9a/
- More Data Rescore: https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore

## 0:00-0:15 完成形を先に見せる

Runtime Full Reviewを開き、最上位カード、Evidence ID、counter evidence、
missing evidence、provider positionsを一瞬見せる。

ナレーション:

> 5つのAIが障害を解析しています。しかし、ここにあるのは多数決の答えでは
> ありません。どの証拠が仮説を支持し、何が反証し、何が不足しているかを
> 人間が判断するためのレビューです。

## 0:15-0:35 4段階を示す

画面または字幕:

```text
Source Profile -> Human Semantics -> Log Analysis -> Evidence-backed Decision
```

ナレーション:

> 複数AIにログを読ませる前に、コードからシステムの意味を推定し、人間が
> その意味を承認します。承認後は、サニタイズ済み証拠と固定JSONだけで解析します。

## 0:35-1:30 Code Profileを主役にする

Runtime Code Profileを開き、次の順に見せる。

1. Gemini Pro Code Profile
2. Gemini System Reading
3. Gemini Questions For Human Approval
4. 人間回答

ナレーション:

> Gemini 3.1 Proがサニタイズ済みコードから、システム目的、コンポーネント、
> ログとメトリクスの意味を推定します。ただしGemini自身には、その推定を
> 確定する権限を与えていません。

入力例として次を見せる。

```text
The critical outcome is a continuously available public YouTube live stream
with fresh ADS-B visual content and audible program audio.

Zero is healthy for failure counters. A controlled deployment restart can be
expected; repeated or failed restarts are suspicious. Monitoring-only warnings
do not prove user impact without public-output or runtime evidence.
```

## 1:30-2:05 Gemini解釈と人間の再レビュー

候補パッチで、次のような構造化結果を拡大する。

```json
{
  "metric_name": "stream_engine_ffmpeg_restart_count",
  "healthy_direction": "zero",
  "zero_behavior": "healthy",
  "increase_behavior": "suspicious"
}
```

ナレーション:

> 人間が自然言語で答えた運用知識を、Geminiが機械判定可能な候補JSONへ
> 変換します。これはまだ確定値ではありません。人間が解釈を再確認し、
> 問題があれば回答へ戻して再解釈します。

`Approve Reviewed Interpretation` 相当の承認結果を見せる。

## 2:05-2:25 ハッシュ固定と境界

次を表示する。

- `status: approved`
- Runtime approved profile SHA256: `77ceaa551a41d4a9e24fa3533de0bfe7df1f17a56702d6ed13e1e6b5342ce709`
- `source_access_after_approval: disabled`
- `context_is_not_evidence: true`

ナレーション:

> 承認結果は、元プロファイル、人間の回答、Gemini出力とハッシュで結び付きます。
> 承認後のログ解析にはこのJSONだけを渡し、ソースの再参照を禁止します。

## 2:25-3:10 最終Reviewの1判断だけ説明する

Runtime Reviewを開く。45,000入力行から27,926件のサニタイズ済みeventを
受け入れ、909 Evidence Itemを5つの実APIが最大19チャンクで解析したことを
短く示す。全Evidence Item coverageは100%、raw row direct promptは0。

説明するカードは1枚だけに絞る。

- Suspected issue
- Operational mechanism
- Evidence IDs
- Counter evidence
- Missing evidence
- Provider positions
- Promotion state

ナレーション:

> この仮説を原因とは断定していません。Evidence IDが技術的な確認対象を
> 支持する一方、反証と不足証拠が残るためValidation Targetです。スコアは
> 真実である確率ではなく、人間が先に確認すべきレビュー優先度です。

## 3:10-3:30 追加証拠で締める

同じReview Graphを数秒見せる。時間に余裕があれば、別の保存済み事例である
More Data Rescoreを開く。

ナレーション:

> 最後に別の保存済み事例で、追加証拠が入るとValidation Targetから判断状態が
> 変わることを示します。AIは原因仮説を作れます。Ops Evidence Synthesisは、
> その仮説を人間が安全に判断できる、再現可能な証拠へ変換します。

## 撮影上の注意

- Code Profile時点では「ログもサニタイズ済み」と言わない。正しくは、ログ解析前にコードをサニタイズし、システムの意味を確定している。
- ターミナルは承認済みprofile pathとReview URLを示す10秒程度に留める。
- API待ち時間はカットする。
- monitoringの結果へ切り替える場合は、runtimeとは別の監視面事例だと明示する。
- provider convergenceを原因確率や多数決として説明しない。
