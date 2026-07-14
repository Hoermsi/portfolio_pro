---
target: views/ai_portfolio.py (KI-Portfolios)
total_score: 21
p0_count: 0
p1_count: 3
timestamp: 2026-07-12T22-01-21Z
slug: views-ai-portfolio-py
---
Method: dual-agent (A: design review · B: detector/browser evidence), run as two isolated sub-agents.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 2 | `st.status(expanded=True)` gives good in-flight feedback during the strategist run, but on a failed/truncated call the cost already billed is silently dropped from the session tracker — the user sees no confirmation that money was still spent. |
| 2 | Match System / Real World | 3 | German finance vocabulary (Kauf/Verkauf/Umschichtung, Anteil %) is consistent and appropriate for the sole developer-user. |
| 3 | User Control and Freedom | 2 | No cancel/abort once "Neue KI-Anweisungen einholen" fires — a multi-second, paid API call with no way out once clicked. Reset flow itself has good control. |
| 4 | Consistency and Standards | 2 | Both money-consequential buttons render in Streamlit's unstyled default red (`#ff4b4b`) — identical to the system's own loss-red token, breaking the "green/red only for G/V" rule the rest of the app follows. |
| 5 | Error Prevention | 3 | The destructive reset is genuinely well-guarded (button hard-disabled until an explicit checkbox is ticked, confirmed live). The costly AI-run button has zero equivalent friction despite spending real metered money per click. |
| 6 | Recognition Rather Than Recall | 3 | Model, cost estimate, and mandate are all shown inline next to the run button. |
| 7 | Flexibility and Efficiency | 1 | No keyboard shortcuts, no bulk actions (e.g. can't batch-dismiss dust positions). |
| 8 | Aesthetic and Minimalist Design | 1 | Measured, not assumed: primary CTA text fails WCAG AA at 3.3:1, the sidebar "KI-Analyse bereit" badge fails catastrophically at 1.2–1.6:1, and the comparison/allocation charts are silently clipped ~47% at tablet width. Plus 19 unfiltered dust positions (down to 0.0000003%) compete with the 5 that matter. |
| 9 | Error Recovery | 2 | Error messages from the agent layer are specific and actionable ("Antwort abgeschnitten... bitte erneut versuchen"), but never disclose that the failed attempt still cost real money. |
| 10 | Help and Documentation | 2 | Two contextual `help=` tooltips exist; no guidance on the strategist's mandate rules or trade-cost mechanics beyond one caption. |
| **Total** | | **21/40** | **Acceptable — significant improvements needed, concentrated exactly where this product's stakes are highest: money-visibility and error recovery around a metered, irreversible action.** |

## Anti-Patterns Verdict

**Does this look AI-generated?** Not in the obvious "generic SaaS" sense — no card-grid KPI tiles, no gradient text, the layout faithfully follows the committed navy/tonal-gradient system (verified: `stMetric` background computes to exactly `linear-gradient(145deg, rgb(23,32,51), rgb(17,24,39))`, matching DESIGN.md's tokens precisely). The tell here isn't sloppiness, it's **an unfinished system**: two components were clearly never styled and fall back to framework defaults that happen to collide with the design system's own semantics.

**LLM assessment (A)**: Two concrete tells — (1) both `type="primary"` buttons on this page inherit Streamlit's stock brand red, which is the exact hex of the app's own loss-red token; nothing in `apply_theme()` overrides `button[kind="primary"]`. (2) The changelog/recommendation cards use hardcoded emoji (🟢 buy / 🔴 sell / ⚪ hold) — a third, undocumented red/green pairing that DESIGN.md explicitly warns against adding, and one that's semantically confusable with real gain/loss color two sections above.

**Deterministic scan (B)**: `detect.mjs` found nothing to flag in `views/ai_portfolio.py` itself (pure Streamlit calls, no author-written markup) and one advisory in `ui/components.py` (`.88rem` metric-label size isn't a named token in DESIGN.md's four documented typography roles — a real documentation gap now that DESIGN.md exists, not a code defect; the value is used consistently in exactly one place).

**Live browser detector + manual verification** (script injection succeeded — Streamlit's DOM accepts mutation, no CSP blocked it): 10 findings, converging with and extending Assessment A:
- **Primary CTA text measured at 3.3:1** (white on `#ff4b4b`) — fails WCAG AA (4.5:1), independently confirming and quantifying A's semantic-collision finding.
- **Sidebar "KI-Analyse bereit" badge at 1.2:1 and 1.6:1** — Streamlit's own default success-green (`#3dd56d`, not a token anywhere in `ui/components.py`) bleeding through the dark-theme CSS overlay's text-color rule. Appears on every page via the sidebar, not just this one.
- **Comparison chart and allocation pie both silently clipped** at ≤768px viewport width — the Plotly SVG stays at its desktop pixel width inside a narrower, `overflow-x:hidden` container; a resize event does not trigger Plotly to relayout. Roughly the right half of the comparison chart (later dates, legend) is invisible with no scrollbar to reveal it.
- **Keyboard focus is inconsistent**: native Streamlit buttons and Plotly's toolbar show a visible ring on focus; `st.tabs` tab controls and generic landmark containers show none at all (confirmed via real Tab-key traversal, not inference).
- **Heading hierarchy skips levels**: h1 "KI-Portfolios" is followed directly by h4 "🤖 Krypto-KI: Positionen" with no h2/h3.

No user-visible overlay was left in the browser — findings were retrieved via `window.impeccableDetectAsync()` and the injected `live-server.mjs` process was stopped immediately after (confirmed port closed).

## Overall Impression

The bones are right — this page inherits Portfolio Pro's dark terminal system faithfully and gets the hardest part (the destructive reset) genuinely correct. But the two components that matter most on this specific page — the button that spends real money and the chart that answers "is the AI winning" — are the two that got the least design attention. The primary CTA is an unstyled framework default that happens to look like a warning. The comparison chart, this page's entire reason for existing, is unreadable on anything narrower than a laptop. Neither is a matter of taste; both are measurable, fixable defects.

## What's Working

1. **The reset guard is exemplary error prevention.** Hard-disabled via `disabled=not confirm` until an explicit checkbox is ticked — verified live (dimmed, `cursor: not-allowed`, genuinely non-interactive). This is the strongest interaction pattern on the page and the template the AI-run button should borrow from.
2. **The empty state teaches instead of showing nothing.** Before the "Depot anlegen" CTA even appears, an info box explains exactly what will happen (exact copy of current positions, 0.25% trade cost, ongoing comparison) and shows the real current value as a preview metric first — textbook progressive disclosure for a first-time setup.
3. **The skipped-position warning is disciplined, trustworthy design.** When a position has no price at baseline time, the app surfaces this directly with a concrete recovery instruction rather than silently under-counting the baseline — exactly right for a tool whose entire premise is "trust the number."

## Priority Issues

**[P1] Primary CTA color collides with the loss-red token and fails contrast**
Why it matters: Both money-consequential buttons ("Neue KI-Anweisungen einholen", "Depot jetzt anlegen") render in Streamlit's default `#ff4b4b` — identical to DESIGN.md's `data-negative`. This breaks the system's own core rule (red/green reserved strictly for gain/loss) on its two highest-stakes controls, and independently measures at 3.3:1 contrast, below WCAG AA.
Fix: Add an explicit `button[kind="primary"]` rule to `apply_theme()`'s injected CSS using a neutral/structural color (e.g. a lighter slate from the border family), reserving red/green for G/V only.
Suggested command: `/impeccable harden`

**[P1] Silent cost loss on a failed strategist run**
Why it matters: `agents/base.py` computes real billed cost before checking for a truncated/failed response; on error, `strategist.run_strategy` discards that usage entirely, so the sidebar's running cost tracker under-reports actual spend with zero acknowledgment that a failed run still cost money. For a solo developer paying out of pocket, this directly undermines the one number he uses to judge whether the experiment is worth continuing.
Fix: Propagate `usage`/`total_cost_usd` through the error branch and surface it in the error message (e.g. "Fehlgeschlagen, aber $0.04 wurden bereits verbraucht").
Suggested command: `/impeccable harden`

**[P1] Comparison and allocation charts are silently clipped at ≤768px width**
Why it matters: Confirmed reproducible — the Plotly SVG renders at its desktop pixel width inside a narrower container with `overflow-x: hidden`; roughly the right half of the comparison chart (recent dates, legend) is invisible with no scrollbar and no resize-triggered relayout. This is this page's central visual — "is the AI beating me" — becoming unreadable on a tablet.
Fix: Force Plotly to relayout on container resize (`fig.update_layout(autosize=True)` plus a resize listener, or set an explicit `width=None`/`use_container_width` equivalent that Streamlit's newer chart API respects), or cap column width usage so charts get full-width real estate below a breakpoint.
Suggested command: `/impeccable adapt`

**[P2] Sidebar status badge fails contrast catastrophically**
Why it matters: The "KI-Analyse bereit" badge (Streamlit's default success-green `#3dd56d`, not a token in the design system) combined with the app's own dark-theme text-color override measures at 1.2:1 and 1.6:1 — both far below the 4.5:1 floor. This is chrome visible on every page, including this one.
Fix: Either exclude `stAlert`/`stSuccess` text from the global text-color override in `apply_theme()`, or give success/warning/info alerts their own on-brand color pair consistent with the rest of the palette.
Suggested command: `/impeccable audit`

**[P2] Emoji action markers reintroduce an undocumented third red/green pair**
Why it matters: `_AKTION_ICON` hardcodes green=buy/red=sell for changelog and recommendation cards — a third G/V color pairing distinct from the two already-documented ones, and easily confusable with actual gain/loss coloring shown two sections above on the same page.
Fix: Replace emoji action markers with neutral glyphs or text badges (↑/↓/↔ in text-muted, or small uppercase action chips), keeping red/green exclusively for value gain/loss.
Suggested command: `/impeccable polish`

**[P2] No confirmation before a costly, several-second paid AI call**
Why it matters: "Neue KI-Anweisungen einholen" fires immediately on click with no confirm step, despite an explicit dollar estimate right next to it and its proximity to a dense two-column layout that invites misclicks.
Fix: A lightweight popover confirm or the same checkbox-gate philosophy already proven on the reset flow, scaled down.
Suggested command: `/impeccable harden`

## Persona Red Flags

**Alex (Power User)**
- No keyboard shortcuts or bulk actions anywhere — can't dismiss dust positions or batch-review recommendation history without the mouse.
- The strategist call has no cancel/abort once fired; if Alex clicks the wrong scope tab's button, the only way out is waiting out the full paid API call.

**Sam (Accessibility-Dependent User)**
- Confirmed via real keyboard traversal: `st.tabs` tab controls and generic landmark containers get **zero** visible focus indicator, while native buttons and Plotly's toolbar do get a ring — an inconsistent, unreliable anchor for keyboard-only navigation.
- Confirmed measured contrast failures directly in Sam's path: the primary action button (3.3:1) and the sidebar status badge (1.2–1.6:1) both fall well under WCAG AA.
- The comparison chart's only differentiator between "Echt" and "KI" lines is color (slate vs. green) — no dash pattern or marker shape for colorblind/low-vision users.

**René (solo finance-tool owner — project-specific persona)**
Profile: single developer checking real stock/crypto/cash holdings weekly on a self-funded Anthropic Console key, per PRODUCT.md.
Red flags: (1) the silent cost-loss-on-error bug directly corrupts the one number (session cost) he uses to decide whether the experiment is worth its spend; (2) the AI-run button's Streamlit-red styling, one section below a metric using that same red for "the AI is losing," creates hesitation on a page whose entire job is inspiring calm trust in the numbers; (3) no persistent total-experiment-cost-to-date exists anywhere — only a per-run figure and a session-scoped total that resets on app restart.

## Minor Observations

- Heading hierarchy skips levels (h1 → h4, no h2/h3) — a real but small screen-reader structure gap.
- Position table shows all 19 holdings unfiltered, including 5 below 0.002% allocation at full 6-decimal precision — pure noise against the handful that matter.
- The 6-color categorical palette used for the allocation pie necessarily repeats past the 6th of 19 crypto symbols, undermining the legend.
- Streamlit's `use_container_width` parameter is deprecated and fires repeated warnings in server logs — a mechanical cleanup, not a design issue, but worth bundling with the responsive-chart fix above since both touch the same chart calls.
- The stock tab's setup metric label ("Aktueller Wert deiner Aktien-Positionen (Startkapital)") states the same concept twice; could split into label + caption.
- Changelog `begruendung` text appears mid-word truncated in at least one observed entry — worth a truncate-at-word-boundary pass if this is a render artifact rather than source data.

## Questions to Consider

- If this page's entire premise is "did the AI beat me," why does the chart's AI-line color never reflect whether it's currently winning or losing — only its fixed identity?
- The reset flow proves the team already knows how to gate a costly action properly — why does the more-frequently-clicked, real-money "get new AI instructions" button get none of that friction?
- Would a small persistent "total spent on this experiment since start" figure change how confidently the answer to "is this worth it" can be given each time the page opens?
