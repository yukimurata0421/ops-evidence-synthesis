# ProtoPedia Entry Draft JA

## 作品タイトル

Ops Evidence Synthesis - AIが断定する前に運用証拠を固定するDevOps Review Agent

## 概要

Ops Evidence Synthesis は、DevOpsのインシデントレビューでAIが少ない証拠から自信満々に原因を断定してしまう問題に対して、証拠境界・モデル差分・追加調査点を固定するAIエージェントです。

raw log はローカルに残し、サニタイズ済みのEvidence BundleだけをSHA256で固定します。今回の公開主導線では、45,000行の入力から27,926件のサニタイズ済みruntime eventを受け入れ、909個のEvidence Itemにまとめました。Gemini、GPT OSS、Mistral、Qwen、Gemma 4の5つの実APIでチャンク解析し、原因を自動昇格せず7件のhuman-gated validation targetとして可視化します。amazon-notifyのレビューはMore Data Rescore Demoとして残しています。

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
- Gemini、GPT OSS、Mistral、Qwen、Gemma 4でサニタイズ済みEvidence Itemをチャンク解析する
- AIの出力を最終結論ではなくReview Targetへ変換する
- technical convergence と incident / user impact promotion gate を分離する
- 追加証拠を child Evidence Bundle として取り込み、再スコアできる
- Cloud Run上の読み取り専用UIで、審査員がログインなしに確認できる

### 4. つくる

メインデモでは、まずサニタイズ済みコードをGemini 3.1 Proが読み、人間の8回答を候補JSONへ正規化します。人間が解釈を再確認してSHA承認した後はソース参照を無効化し、45,000行の入力から受け入れた27,926件のruntime event、909個のEvidence Item、承認済みJSONだけを5つの実APIへprovider別チャンクで渡します。providerの支持は「真実」ではなくreview workとして扱い、現在の公開結果は0 primary candidate、7 validation targetです。

メインデモURL:

https://ops-evidence.yukimurata0421.dev/?evidence_sha256=a7fc02ea095516eaaed07f4599c3e25f94d092163ed163efccfb6f0300ee50e0

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

Cloud Runにデプロイした読み取り専用UIとして提供しています。初回GETでlive model workを起動せず、precomputed review cacheを返すため、審査員はログインなしでSummary、Detail、Review Graph、Markdown Incident Report、API View、More Data Rescore Demoを確認できます。

Public entry:

https://ops-evidence.yukimurata0421.dev/

## 開発素材

- Google Cloud Run
- Vertex MaaS / Mistral
- Python
- FastAPI
- SQLite / PostgreSQL ledger support
- pytest
- Cloud Build
- GitHub Actions
- gitleaks
- SVGアーキテクチャ図
- precomputed review cache

## 提出URL

- GitHub: https://github.com/yukimurata0421/ops-evidence-synthesis
- Deployed project: https://ops-evidence.yukimurata0421.dev/
- Primary incident report: https://ops-evidence.yukimurata0421.dev/ui/report.md?evidence_sha256=a7fc02ea095516eaaed07f4599c3e25f94d092163ed163efccfb6f0300ee50e0
- Architecture image: [assets/architecture-devops-ai-agent.svg](assets/architecture-devops-ai-agent.svg)
- Demo video script: [demo-video-script.md](demo-video-script.md)
- Submission links: [submission-links.md](submission-links.md)
