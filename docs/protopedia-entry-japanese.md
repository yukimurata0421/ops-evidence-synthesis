# ProtoPedia Entry Draft JA

## 作品タイトル

Ops Evidence Synthesis - AIが断定する前に運用証拠を固定するDevOps Review Agent

## 概要

Ops Evidence Synthesis は、DevOpsのインシデントレビューでAIが少ない証拠から自信満々に原因を断定してしまう問題に対して、証拠境界・モデル差分・追加調査点を固定するAIエージェントです。

raw log はローカルに残し、サニタイズ済みのEvidence BundleだけをSHA256で固定します。その同一証拠に対してGeminiを起点に複数AI providerを実行し、各モデルの主張・沈黙・不一致をReview Targetとして可視化します。

AIは最終原因や危険な運用操作を勝手に決定しません。代わりに、人間が確認すべき論点、足りない証拠、再スコア対象を提示します。Cloud Run上の読み取り専用UIから、Summary、Detail、Review Graph、API View、More Data Rescore Demoを確認できます。

## ストーリー

### 1. 解決したい課題と背景

AIOpsや障害対応AIで危険なのは、AIがログを要約できることではなく、十分な証拠がない状態で原因を断定してしまうことです。実運用では、必要なのは「それっぽい答え」ではなく、どの証拠を見たのか、どのモデルが同意したのか、何が未確認なのか、次に何を集めるべきかを追跡できることです。

Ops Evidence Synthesis は、AIの出力をそのまま結論にするのではなく、Evidence Bundle、provider positions、Canonical Review Graph、More Data Rescore Demoとして確認できる形に変換します。

### 2. 想定ユーザー

小規模から中規模のDevOps/SRE/運用改善担当者を想定しています。特に、ログやソースコードを外部AIにそのまま渡せない環境、障害対応の判断を人間が最終責任として持つ必要がある環境、複数AIの出力を監査可能にしたい環境を対象にしています。

### 3. プロダクトの特徴

- raw log はアップロードせず、ローカルでサニタイズする
- Evidence BundleをSHA256で固定し、同じ証拠に対するレビューを再現可能にする
- Geminiを起点に複数providerの主張・沈黙・不一致を比較する
- AIの出力を最終結論ではなくReview Targetへ変換する
- technical convergence と incident / user impact baseline を分離する
- 追加証拠を child Evidence Bundle として取り込み、再スコアできる
- Cloud Run上の読み取り専用UIで、審査員がログインなしに確認できる

### 4. つくる

Gemini Enterprise Agent Platform を baseline provider とし、gpt-oss、Mistral、Qwen、GLMを adversarial cross-check として扱います。複数providerが同じ証拠を見ても、同意は「真実」ではなく review signal として扱います。沈黙や不一致は validation target として残します。

メインデモURL:

https://ops-evidence.yukimurata0421.dev/?evidence_sha256=7e95346cbf15de7f104631b72d784e02665d0cc1488e42a4ccf69b76fe47308d

### 5. まわす

More Data Rescore Demo では、追加証拠によって判断が変わる流れを示します。

- before: `validation_target`
- promotion score: `0.69`
- blocked reason: `user_impact_unverified`
- evidence delta: 追加ログ2件、追加証拠参照4件
- transition: `needs_more_data -> evidence_collected`
- after: `primary_candidate`
- promotion score: `0.84`
- review priority score: `0.86`
- blocked reasons: 解消
- provider positions: 5 provider すべてを表示

URL:

https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore

これは一問一答のAIではなく、追加証拠を取り込み、child Evidence Bundleとして扱い、Review Graphを再スコアするDevOps改善ループです。

### 6. とどける

Cloud Runにデプロイした読み取り専用UIとして提供しています。初回GETでlive model workを起動せず、precomputed review cacheを返すため、審査員はログインなしでSummary、Detail、Review Graph、API View、More Data Rescore Demoを確認できます。

Public entry:

https://ops-evidence.yukimurata0421.dev/

## 開発素材

- Google Cloud Run
- Gemini / Gemini Enterprise Agent Platform系
- Python
- FastAPI
- SQLite
- pytest
- Cloud Build
- GitHub Actions
- gitleaks
- SVGアーキテクチャ図
- 複数provider比較用のprecomputed review cache

## 提出URL

- GitHub: https://github.com/yukimurata0421/ops-evidence-synthesis
- Deployed project: https://ops-evidence.yukimurata0421.dev/
- Architecture image: [assets/architecture-devops-ai-agent.svg](assets/architecture-devops-ai-agent.svg)
- Demo video script: [demo-video-script.md](demo-video-script.md)
- Submission links: [submission-links.md](submission-links.md)

