# h-cli — Executive Summary

**Natural language infrastructure management via Telegram.** Send plain text, get things done.

## What it does

A messaging bot backed by Claude Code that executes network/sysadmin commands in a hardened container. You type "scan 192.168.1.1" — it runs nmap and returns results. Maintains conversation context for 4 hours with automatic chunking at 100KB so it understands "that host" and "same scan again." Supports Telegram, Discord, Slack, and a web UI.

## Architecture

Up to twelve services, two isolated Docker networks:

- **Interface bots** — Telegram, Discord, Slack, Web UI (each an independent container, profile-selectable)
- **Redis** — message queue + session storage
- **claude-code** — dispatcher + Asimov firewall (MCP proxy), bridges both networks
- **core** — toolbox with MCP server (default: Debian 12, optional: ParrotOS), backend-only

Every `run_command()` call passes through the Asimov firewall before reaching core.

## Security posture

Production-hardened. 45 security items implemented:

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

Deployed and running. Security hardening complete (45 items), Asimov firewall active with HMAC-signed results.
