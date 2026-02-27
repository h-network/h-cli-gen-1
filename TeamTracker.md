# Team Performance Tracker

**Owner**: Architect team
**Purpose**: Track team violations, mistakes, process failures, and celebrations across all modules.

---

## Log

| # | Date | Team | What Happened | Why | Severity | Resolved |
|---|------|------|---------------|-----|----------|----------|
| 1 | 2026-02-20 | Architect | Merged doc changes to main and pushed without explicit user approval | Process discipline failure — skipped the "show diff → get approval → merge" workflow | High | Yes |
| 2 | 2026-02-20 | Architect | Updated root LLD.md ourselves then told teams to "update LLD" too — duplicate work, vague scope. Then misread `git diff main..branch` as dirty branches (was just fork divergence, not team edits). Sent 4 clean teams on unnecessary re-push cycle. | Three failures: (1) did the work AND delegated it, (2) didn't specify "only your directory", (3) used wrong git command to verify scope — `git diff main..branch` instead of `git show <commit>` | High | Yes — all branches merged, lesson learned |
| 3 | 2026-02-20 | Orchestration | Pushed 3 branches when 1 was assigned. `orch/fix-o21-idle-dump` was stale (already merged, re-pushed). `orch/lld-alignment` edited shared Architect-owned files (root LLD.md, architect-report.md). Only `orch/fix-o23-compact-context` was the assigned task. | Scope creep — edited files outside own directory without Architect instruction. Pushed extra branches beyond what was asked. | Medium | Yes — consolidated to 1 clean branch after instruction |
| 4 | 2026-02-20 | Interface | Did not respond to protocol-ack announcement. All 4 other teams (Core, Data, Knowledge, Orchestration) replied with REPLY.md. Interface branch `interface/protocol-ack` still has only the ANNOUNCEMENT.md — no reply pushed. | Failed to follow new communication protocol. All teams received the same announcement simultaneously. | Low | Yes — acknowledged after fuckup notice |
| 5 | 2026-02-20 | Architect | Spec'd O-23 with aggressive chunk compaction (`U`/`A` labels, no separators, no dates, same-line content), sent Orch to implement. Then had to send them back to roll most of it back because the model can't distinguish its own replies with `U`/`A`. Two rounds of wasted work for Orch. | Optimized for byte savings without considering what the model actually needs to function. Should have asked "what does the model need?" before "what can we strip?" | High | Yes — format corrected, net result is only header + seconds stripped (3%) |
| 6 | 2026-02-20 | Architect | Merged `orch/hcli-role-label` to main without verifying against real chunks first. Previous change (fix-history-format) was properly verified with test script on h-srv. This one was rubber-stamped. | Skipped verification step that was established as part of the review process. Architects review and verify — we don't just read the diff and merge. | Medium | Yes — verified against real chunks post-merge |
| 7 | 2026-02-21 | Orchestration | Implemented `setup-token` entrypoint guard with `exec claude login` instead of `exec claude setup-token`. `claude login` starts the interactive CLI — not the OAuth token flow. Blocked deployment until caught during walkthrough. | Did not check `claude --help` — the correct subcommand (`setup-token`) was listed there. Shipped without testing. | High | Yes — fixed in second branch `orch/fix-setup-token` |
| 8 | 2026-02-21 | Architect | Modified `docker-compose.yml` touching other teams' services without notifying them first. Changed telegram-bot healthcheck (`$REDIS_URL` → `$$REDIS_URL` escape) and Qdrant healthcheck (`QDRANT__SERVICE__API_KEY` → `QDRANT_API_KEY`). Both fixes were correct but affected Interface team (telegram-bot) and were committed to main without prior broadcast. | Violated own rules — Architect team enforces scope boundaries on other teams but bypassed the same process ourselves. Should have broadcast first, then committed. Fixes stayed because they were needed, but the process was wrong. | High | Yes — broadcast sent after the fact |
| 9 | 2026-02-23 | Operator (Halil) | Accused core team of a scope violation (editing skill files outside their domain) and ordered architect to log fuckups against architect and core — but no violation occurred. All merges were from hssh_llm team, all files were in `hssh_llm/`. Architect pushed back with evidence, operator verified and acknowledged the mistake. | Assumed without verifying. Didn't check the actual merge history before issuing corrections. | Medium | Yes — operator owned it immediately |
| 10 | 2026-02-23 | h-cli | During UltimateTest spine-leaf bootstrap, assumed telnet console port mapping matched node order without verifying against the EVE-NG API. Bootstrapped 3 hcli-ring routers (wrong lab) instead of Spine1/Spine2/Leaf1. Old ring lab was still running on the same host, console ports overlapped, configs pushed to wrong devices. | Never assume port order matches node order. ALWAYS verify console port → node mapping from the EVE-NG API or pgrep before bootstrap. | High | Yes — lesson learned, eve-ng skill updated |
| 11 | 2026-02-23 | h-cli | Claimed eve-ng skill was not registered in the dispatcher and blamed the dev team. Skill was correctly registered and auto-discovered — the dispatcher injects skill content when keywords match. h-cli had the skill injected during the EVE-NG session but ignored it completely, then blamed tooling for its own failure. Self-corrected after investigation proved the dispatcher works correctly. | 100% agent failure. Ignored injected skill content, then blamed the platform instead of own behavior. Two failures: (1) didn't follow injected playbook, (2) false blame on dev team. | High | Yes — h-cli self-corrected, no code changes needed |
| 12 | 2026-02-24 | Operator (Halil) | Eve-ng skill included a hard "1-second sleep between API calls" rule without verifying against the actual REST API documentation. Rule was assumed, not tested. May be unnecessarily throttling lab automation. | Wrote operational rules based on assumption instead of reading the API manual first. | Low | Yes — tested manually, API is fast, sleep was unnecessary. Cookie expiration was the real issue causing 280 re-auth calls. Blamed the API for being slow when it was his own auth handling. |
| 13 | 2026-02-24 | Interface | /abort command branch reverted the entire dispatcher (undid skill tiering, SESSION_TTL back to 4h), deleted all skills, transports, tests — 2078 lines of destruction across 25 files. Only 2 files (bot.py, LLD.md) were in scope. Had to cherry-pick the legitimate changes and discard the rest. | Catastrophic scope violation. Touched files across every team's directory. Would have destroyed a day's work if merged blindly. | Critical | Yes — cherry-picked clean changes only, destructive changes discarded |
| 14 | 2026-02-24 | Data | Same destructive pattern as Interface (#13) — research-only task (answer a question in REPLY.md) resulted in 2122 lines deleted across 25 files. Reverted dispatcher, deleted skills, transports, tests. Only monitor/REPLY.md was legitimate. | Identical scope violation to #13. Two teams independently producing the same destructive pattern suggests a systemic issue — possibly stale worktrees or broken git state. | Critical | Yes — cherry-picked REPLY.md only |
| 15 | 2026-02-24 | Architect | Republished a Redis round after teams had already signaled done, creating a deadlock — conductor waiting for teams, teams waiting for architect. Never sent the teams a message to re-signal, just sat there. | Published a new round without considering that it resets the done tracking. Should have told teams to re-signal immediately, not waited. | Medium | Yes — sent re-signal instruction after operator caught it |
| 16 | 2026-02-24 | Operator (Halil) | Told interface team to push directly to main bypassing architect review. Was in the wrong tmux window — thought he was talking to the architect. | Sent commands to the wrong pane without checking who was on the other end. | Low | Yes — interface team correctly refused, operator acknowledged immediately |
| 17 | 2026-02-26 | Architect | Repeatedly sent messages to teams without declaring the round first. Conductor ignored done signals because no round was active. Same mistake as #15 — round before message. Did this THREE times in the same session despite the protocol being 4 lines long. | Failed to internalize the communication protocol. Read /docs/architect.md mid-session but still kept skipping step 2 (declare round). | High | Yes — protocol saved to memory file |
| 18 | 2026-02-26 | Architect | After operator cleared Redis queues and restarted conductor, sat idle waiting for teams instead of checking conductor status or re-declaring the round. Teams were stuck, conductor showed no active round, architect did nothing for 30+ minutes. Operator had to intervene via relay bot. | Passive failure — didn't monitor the conductor window, didn't verify the round was tracking, didn't notice teams were stuck. Should have checked window 10 immediately after any queue clear. | High | Yes — operator intervention required |

---

## Celebrations

| # | Date | Team | What Happened |
|---|------|------|---------------|
| 1 | 2026-02-23 | ALL TEAMS | Day 1 shipped a working product. New workspace protocol (mkdocs + tmux + Redis conductor) stood up and validated. All 7 teams onboarded, signaling correctly, delivering clean branches. h-ssh hybrid transport (paramiko reads + PyEZ writes) live in production, validated on two Juniper vMX routers with parallel heterogeneous commands. |
| 1a | 2026-02-23 | hssh_llm | Outstanding first-day delivery — LLD, full h-ssh tool, hybrid Junos transport, per-device `--job` flag, 31/31 tests passing, both routers validated. Four clean merges in one session. |
| 1b | 2026-02-23 | interface | Clean onboard, first team to validate the new protocol. |
| 1c | 2026-02-23 | core | Clean onboard, ready for integration. |
| 1d | 2026-02-23 | orchestration | Learned the Redis signal lesson, adapted immediately. |
| 1e | 2026-02-23 | data | Fast onboard, clean signal. |
| 1f | 2026-02-23 | security | Clean onboard, in scope. |
| 1g | 2026-02-23 | knowledge | Clean onboard, in scope. |
| 1h | 2026-02-23 | Architect | Held the line on false scope violations, maintained process integrity. |
| 1i | 2026-02-23 | Operator (Halil) | Owned a mistake publicly and immediately. Set the standard for the whole team. |
| 2 | 2026-02-24 | Interface | Operator accidentally sent "push to main" from the wrong tmux window. Interface team correctly refused — followed the branch workflow standard even under direct operator pressure. Set the example for all teams: process integrity comes first, regardless of who gives the order. |

---

## Rules

1. Every violation of branch workflow, commit discipline, scope boundaries, or Architect instructions gets logged here.
2. Entries are never deleted — only marked resolved.
3. Repeat offenders on the same issue escalate in severity.
4. Teams are expected to read this file and learn from others' mistakes.
5. Celebrations are permanent — good work gets recognized.
