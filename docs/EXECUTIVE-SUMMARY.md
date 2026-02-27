# h-cli — Executive Summary

**Natural language infrastructure management via Telegram.** Send plain text, get things done.

## What it does

A Telegram bot backed by Claude Code that executes network/sysadmin commands in a hardened ParrotOS container. You type "scan 192.168.1.1" — it runs nmap and returns results. Maintains conversation context for 4 hours with automatic chunking at 100KB so it understands "that host" and "same scan again."

## Architecture

Four containers, two isolated Docker networks:

- **telegram-bot** — user interface, auth gatekeeper
- **Redis** — message queue + session storage
- **claude-code** — dispatcher + Asimov firewall (MCP proxy), bridges both networks
- **core** — ParrotOS toolbox with MCP server, only network that can execute commands

Every `run_command()` call passes through the Asimov firewall before reaching core.

## Security posture

Production-hardened. 44 security items implemented:

- Network-isolated frontend/backend
- Fail-closed allowlisting (Telegram chat IDs, sudo commands)
- All containers non-root, dropped capabilities, no-new-privileges, read-only rootfs on telegram-bot
- Dedicated SSH identity (auto-generated, easy to revoke)
- Redis auth, memory-capped, persistent
- **Asimov firewall** — deterministic pattern denylist (always active, zero latency) + independent Haiku gate check (on by default, ~2-3s, resistant to prompt injection)
- Session chunking at 100KB with up to 50KB context injection

## Cost

Zero API costs — runs on Claude Max/Pro subscription.

## The bigger picture

Part of the **h-ecosystem** for self-improving AI. Every conversation, command, and output is stored as structured JSONL. Pair with **log4AI** (shell logger) and **Docling** (PDF parser) to collect training data. After a month of usage: enough data to fine-tune a personalized ops model.

## Status

Deployed and running. Security hardening complete (44 items), 1 deferred finding (F34 — Playwright sandbox, Docker provides outer sandbox), Asimov firewall active with HMAC-signed results.
