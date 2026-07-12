# Findy DevOps × AI Agent Hackathon video kit

This directory is the self-contained source pack for the three-minute OES demo.
It separates editable vector slides, transparent overlays, captured public UI,
the narration, and the exact recording order.

## Start here

1. 動画編集では、まず[05-画面とナレーション対応表.md](05-画面とナレーション対応表.md)を上から順に使用します。
2. 全体方針は[01-storyboard.md](01-storyboard.md)で確認します。
3. ナレーションだけを読む場合は[02-narration-ja.md](02-narration-ja.md)を使用します。
4. Assemble the numbered files in `assets/screenshots/` using
   [03-shot-list.md](03-shot-list.md).
5. Run through [04-recording-checklist.md](04-recording-checklist.md) before
   uploading the video.

## Directory map

```text
hackathon/
├── 01-storyboard.md             timing, screen, and judging purpose
├── 02-narration-ja.md           final Japanese narration
├── 03-shot-list.md              file-by-file edit order
├── 04-recording-checklist.md    capture, edit, and upload checks
├── 05-画面とナレーション対応表.md  screen-by-screen Japanese edit guide
├── claims-and-sources.md        exact public facts and URLs
├── assets/
│   ├── slides/                  editable 1920x1080 SVG title cards
│   ├── overlays/                editable transparent subtitle overlays
│   └── screenshots/             generated 1920x1080 PNG captures
└── tools/
    └── capture_screens.py       regenerates screenshots without model calls
```

## Regenerate the screen pack

```bash
.venv/bin/python hackathon/tools/capture_screens.py
```

The capture script only opens public read-only pages. It does not press the
live model buttons and does not use credentials.

## Naming rule

The first spoken and visual occurrence must be `Ops Evidence Synthesis (OES)`.
After that, use `OES` and pronounce it as the three letters O-E-S.

The primary demo target must also be named before any evidence screen:

```text
stream_v3 — a 24/7 YouTube Live delivery system for ADS-B visuals and audio
```

OES is the investigating system. `stream_v3` is the system being investigated.

## Google Cloud wording

Use:

```text
Cloud Run上のOESが、Gemini Enterprise Agent Platform API経由で、
Model GardenのGemini 3.1 Flash-Liteを呼び出しています。
```

Do not call the current path merely `Vertex Gemini`, and do not claim that the
public Fast Review is deployed to Agent Runtime. The application is served on
Cloud Run and calls the Agent Platform model API directly.
