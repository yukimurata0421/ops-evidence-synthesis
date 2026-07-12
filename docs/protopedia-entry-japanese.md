# ProtoPedia 貼り付け用文案

## 作品タイトル

Ops Evidence Synthesis - 5つのAIで解析しても、証拠がなければ原因にしないDevOps Agent

## ひとことで

AIに原因を当てさせるのではなく、原因と呼べる証拠を集めさせる。機密ログをローカルに残したまま、Geminiを中心に証拠・反証・不足証拠を統合するDevOps調査エージェントです。

## 概要

障害対応AIの危険は、回答が遅いことではありません。raw logに含まれる秘密情報を外部へ送ってしまうことと、証拠が不足していてももっともらしい原因を断定できることです。

Ops Evidence Synthesisは、raw logとraw sourceをローカルに残し、サニタイズ済みのEvidence BundleだけをSHA256で固定します。AIは固定された証拠に対して調査し、引用を検証し、モデル間の不一致をReview Targetへ変換し、足りない証拠を要求します。最終原因の確定と危険な運用操作は人間の承認対象です。

公開主導線では、実際の常時配信システムの45,000入力行を全件サニタイズし、1,035個のEvidence Itemへ集約しました。Gemini、GPT OSS、Mistral、Qwen、Gemma 4の実API出力を検証し、原因を自動昇格させず10件のValidation Targetとして提示しています。

## 解決したい課題

DevOps/SREの現場では、次の問題が同時に発生します。

- ログやソースコードを外部AIへそのまま渡せない
- AIの説明が証拠、推測、操作提案を混在させる
- 複数AIが同意すると、証拠不足でも正解に見える
- 不一致や不足証拠が、次の調査タスクへつながらない
- AIに復旧操作まで任せるにはリスクが高い

OESは「原因は何か」ではなく、「その判断をしてよいだけの証拠があるか」を扱います。

## 想定ユーザー

小規模から中規模のDevOps/SRE、運用改善担当者を想定しています。特に、機密ログを扱う環境、最終判断を人間が担う必要がある環境、複数AIの出力を監査可能にしたい環境を対象にしています。

## AIエージェントである必然性

OESのAgent Traceは次の調査ループを実行します。

```text
freeze_evidence_bundle
-> run_cross_check_providers
-> validate_citations
-> compute_review_targets
-> request_more_evidence
-> arbitrate_review_gate
-> attach_child_bundle
-> re-score
```

AIは一問一答の要約を返すのではなく、証拠を検証し、次に必要な証拠を決め、追加証拠を受けて判断状態を更新します。自律性の境界も明確です。調査、比較、証拠要求、再評価はAgentが進め、最終原因と運用操作はHuman Gateで止めます。

## つくる - Geminiと人間で意味を確定する

ログ解析の前に、Gemini 3.1 Proがサニタイズ済みコードからシステム目的、主要コンポーネント、ログとメトリクスの意味を推定し、人間へ質問します。

人間はJSONを一から書かず、自然言語で運用知識を回答します。Geminiが回答を候補JSONへ正規化し、人間が再確認して承認します。承認結果はSHA256で固定され、その後のログ解析ではコードへの再アクセスを禁止します。Source Profileは解釈コンテキストであり、runtime evidenceとしては扱いません。

Code Profile:
https://ops-evidence.yukimurata0421.dev/code-profiles/31dd5326f0e9e052697975e7174d9de6ebf7c2fde58625cb96ce41f29faab621/

## まわす - 証拠不足を次の調査へ変える

5プロバイダーの支持は多数決の正解として扱いません。各主張はEvidence IDを必要とし、反証、不足証拠、ユーザー影響、provider silenceを保持したままCanonical Review Graphへ統合します。

公開Reviewでは0 Primary Candidate、7 Validation Targetです。これは失敗ではなく、証拠が不足した状態で原因を自動確定しなかった結果です。スコアは原因確率ではなく、人間が先に確認するレビュー優先度です。

Primary Review:
https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=ab18d62c4e628e190345fa218834ca74276f556191d2f068a969f7922945a471

More Data Rescoreでは、Agentが要求したユーザー影響証拠をchild Evidence Bundleとして追加します。状態は `needs_more_data -> evidence_collected` と進み、Validation TargetがPrimary Candidateへ再評価されます。それでも最終原因の確定は人間が行います。

Rescore Demo:
https://ops-evidence.yukimurata0421.dev/ui/rescore-demo?id=amazon-notify-more-data-rescore

## とどける - Google Cloud上で実際に動かす

読み取り専用UIをCloud Runへデプロイし、審査員はログインなしで固定済みReviewを確認できます。Fast GCP Reviewでは、Cloud RunからVertex Gemini Flash Liteを実行し、固定された2,000行のサニタイズ済み証拠から新しいReview URLを生成します。実測は約14秒で、raw logや任意URLは受け付けません。

Fast GCP Review:
https://ops-evidence.yukimurata0421.dev/ui/fast-gcp-review

Verified Fast Review:
https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=2641cb5fe5850d006864dec4aad3b3d2539e9efcef3753b43d5624f8b6e5136b

リリース経路はテスト、秘密情報検査、Cloud Build、Artifact Registry、Cloud Run更新、公開スモークまでを一つの手順で実行します。

## Google Cloud / 開発素材

- Vertex AI / Gemini 3.1 Pro / Gemini 3.1 Flash Lite
- Vertex AI Model Garden / MaaS
- Cloud Run
- Cloud Build
- Artifact Registry
- Cloud Storage
- Secret Manager
- Python / FastAPI
- PostgreSQL / SQLite
- pytest / GitHub Actions / gitleaks

## 差別化

- raw logとraw sourceをモデルへ直接渡さない
- コードの意味をGeminiが推定し、人間が承認する
- Evidence Bundleと承認済み解釈をSHA256で固定する
- 複数AIの一致を原因確率や多数決として扱わない
- 反証、不足証拠、provider silenceを消さない
- 追加証拠をchild bundleとして取り込み再評価する
- 最終原因と運用操作をHuman Gateの外へ出さない
- 45,000-50,000行の実運用規模と、約14秒のライブデモを両方提示する

## システム構成

アップロード画像:
`docs/assets/architecture-devops-ai-agent.svg`

```text
raw logs / raw source stay local
-> local inspect and sanitize
-> Gemini source reading
-> human semantics approval
-> SHA-fixed approved profile
-> Evidence Bundle
-> Gemini-led provider review
-> citation validation
-> Canonical Review Graph
-> missing evidence request
-> human gate
-> child Evidence Bundle
-> re-score
-> read-only Cloud Run UI
```

## 提出URL

- GitHub: https://github.com/yukimurata0421/ops-evidence-synthesis
- Deployed project: https://ops-evidence.yukimurata0421.dev/
- Demo video script: `docs/demo-video-script.md`
- Submission links: `docs/submission-links.md`

## 締め

> AIは原因仮説を作れます。
> OESは、その仮説を人間が安全に判断できる、再現可能な証拠へ変換します。
