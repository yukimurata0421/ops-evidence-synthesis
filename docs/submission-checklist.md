# 最終提出チェックリスト

更新日: 2026-07-12

## 正本

- 動画台本: [demo-video-script.md](demo-video-script.md)
- ProtoPedia文案: [protopedia-entry-japanese.md](protopedia-entry-japanese.md)
- URL一覧: [submission-links.md](submission-links.md)
- 構成図: [assets/architecture-devops-ai-agent.svg](assets/architecture-devops-ai-agent.svg)
- X投稿文: [x-post-draft.md](x-post-draft.md)

旧版の台本、英語版ProtoPedia文案、作業用評価戦略は提出導線から削除済み。上記だけを使用する。

## 公開状態

- GitHub: https://github.com/yukimurata0421/ops-evidence-synthesis
- Public URL: https://ops-evidence.yukimurata0421.dev/
- Cloud Run revision: `ops-evidence-api-00260-qj4`
- Image digest: `asia-northeast1-docker.pkg.dev/ops-evidence-synthesis/ops-evidence/ops-evidence-api@sha256:c37293fc079fd4f3fc8392923e313431131397cd6c9c7ffa9b369aaf27eeaacd`
- Request timeout: 900 seconds
- Fast Review logic: `source-approved-evidence-v2`
- Public smoke: passed

## 撮影前

- [ ] ブラウザを1920x1080、倍率125-150%にする。
- [ ] 個人情報、ブックマークバー、トークンを非表示にする。
- [ ] Code Profile、Runtime Review、Fast Review、Rescoreを別タブで開く。
- [ ] Fast GCP Reviewのowner sessionは録画開始前に有効化し、URL欄にtokenがないことを確認する。
- [ ] 次の検証を通す。

```bash
make smoke-demo-video
make smoke-public
```

## 動画

- [ ] 2分45秒から3分00秒に収める。
- [ ] 冒頭12秒で「5つのAIで解析しても、証拠がなければ原因にしない」と言う。
- [ ] Agent TraceのEvidence固定、Cross-check、Citation validation、Review Target生成を見せる。
- [ ] Code ProfileでGeminiの読み取りと質問を見せ、承認済みSHAは字幕で示す。
- [ ] 45,000入力、27,926 event、909 Evidence Items、5実APIを表示する。
- [ ] `0 Primary` を「証拠不足で止まれた結果」と説明する。
- [ ] Fast GCP Reviewだけ実ライブ実行する。
- [ ] Cross-checkは約232秒かかるため録画中に実行しない。
- [ ] More Data Rescoreで `needs_more_data -> evidence_collected` を見せる。
- [ ] 全編字幕を付ける。
- [ ] YouTubeまたはVimeoへアップロードする。

## 説明で禁止する表現

- [ ] スコアを原因確率と呼ばない。
- [ ] provider convergenceを多数決の正解と呼ばない。
- [ ] Source Profileをruntime evidenceと呼ばない。
- [ ] raw logをCloudまたはモデルへ送ったと説明しない。
- [ ] Primary Candidateを確定原因と呼ばない。
- [ ] Agent Engineへデプロイ済みと説明しない。
- [ ] Code Profile時点でログ解析済みと説明しない。

## ProtoPedia

- [ ] タイトル、概要、ストーリーへ日本語正本文案を貼る。
- [ ] 構成図をアップロードする。
- [ ] 動画URLを登録する。
- [ ] タグ `findy_hackathon` を付ける。
- [ ] GitHubと公開URLを関連URLへ登録する。
- [ ] シークレットウィンドウで全URLを確認する。

## 最終フォーム

- [ ] GitHub URLを入力する。
- [ ] デプロイURLを入力する。
- [ ] ProtoPedia URLを入力する。
- [ ] 最終送信後の画面を保存する。

## ProtoPedia URL作成後

次の `pending` を実URLへ置き換える。

- `README.md`
- `HACKATHON_SUBMISSION.md`
- `docs/submission-links.md`

確認:

```bash
rg -n "pending until the project page is created|TODO|FIXME" \
  README.md HACKATHON_SUBMISSION.md docs/submission-links.md
```

## 最終Git確認

```bash
git status --short
git log -1 --format='%H %an <%ae> %s'
```

- [ ] 意図しない差分がない。
- [ ] 作成者情報が `yukimurata0421 <sss071137@gmail.com>` である。
