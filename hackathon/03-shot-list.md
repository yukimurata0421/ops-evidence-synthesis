# Shot list

## Edit order

| # | File or live action | Duration | Edit instruction |
| ---: | --- | ---: | --- |
| 1 | `assets/screenshots/00-title-card.png` | 18s | Fade in for 6 frames. Keep title and target visible for the full introduction. |
| 2 | `assets/screenshots/01-stream-v3-system.png` | 18s | Slow 102% push-in toward the YouTube Live outcome box. |
| 3 | `assets/screenshots/10-runtime-review-hero.png` | 14s | Show the Review first, then add `20-agreement-not-cause.png` over the lower third. |
| 4 | `assets/screenshots/14-code-profile-system-reading.png` | 11s | Already captured at 160% equivalent zoom. Hold without cursor or additional crop. |
| 5 | `assets/screenshots/15-code-profile-human-questions.png` | 9s | Show staged human answers. Do not type or run APIs during the video. |
| 6 | `assets/screenshots/07-human-semantics-gate.png` | 8s | Move left to right: human answer, Gemini candidate JSON, human approval and SHA. |
| 7 | `assets/screenshots/11-runtime-agent-trace.png` | 17s | Show all six readable steps for 12s, then add `22-guarded-autonomy.png` for the final 5s. |
| 8 | `assets/screenshots/12-runtime-target.png` | 28s | Put `21-runtime-metrics.png` at 90% scale in the top center (1920×1080: X=96, Y=16) for the first 8s. Fade it out, then hold on youtube_health and provider positions. Do not place it in the center or bottom. |
| 9 | `assets/screenshots/04-platform-live.png` | 5s | Use as a clean transition before the live screen recording. |
| 10 | Live recording of Fast GCP Review | 20s | Show Load Summary, Run Live Fast Review, progress, and generated Review link. Compress only idle waiting. |
| 11 | `assets/screenshots/18-rescore-before.png` | 10s | Hold on user_impact_unverified. |
| 12 | `assets/screenshots/19-rescore-after.png` | 12s | Show the promoted state for 8s, then add `23-rescore-transition.png` for the final 4s. |
| 13 | `assets/screenshots/05-end-card.png` | 10s | No extra animation beyond a subtle fade. |

## Live Fast GCP recording

Use:

```text
https://ops-evidence.yukimurata0421.dev/ui/fast-gcp-review
```

Record this segment separately at 1920x1080. Use 125% browser zoom if the
metrics and buttons are not readable in the recording preview:

1. Start with the page already loaded and the URL bar free of tokens.
2. Click `Load Sanitized Code Summary`.
3. Point briefly at `Logic: source-approved-evidence-v2`, `Rows: 2,000`, and
   `Primary model: gemini-3.1-flash-lite`.
4. Click `Run Live Fast Review`.
5. Keep the progress and provider count visible.
6. Open the generated Detail link after completion.
7. Cut idle waiting down to 8-10 seconds, but do not imply the call was instant.

Do not expose an owner token or record the browser address while activating an
owner session. Do not click `Run Live Cross-check` in the main video.

## Safe fallback

If the live call is slow during recording, use the already verified result:

```text
https://ops-evidence.yukimurata0421.dev/ui/full-review-page?evidence_sha256=2641cb5fe5850d006864dec4aad3b3d2539e9efcef3753b43d5624f8b6e5136b
```

The narration must then say `この実行で生成された固定Reviewです`, rather
than pretending that the call just completed on camera.
