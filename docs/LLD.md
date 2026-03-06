# Docs Module — Low-Level Design

## Purpose

The docs module is the project's knowledge base — architecture, security model, configuration reference, integration guides, and validated test cases. We document how h-cli works so operators, contributors, and the bot itself can reason about the system.

## Directory Structure

```
docs/
├── architecture.md                          # System architecture and project structure
├── security.md                              # Security model, container privileges, data access
├── configuration.md                         # Environment variables and setup
├── context-injection.md                     # Plain text vs JSONL replay — design rationale + benchmarks
├── eve-ng-automation.md                     # EVE-NG integration: SSH, console, lab automation
├── netbox-integration.md                    # NetBox integration: device CRUD, cable management
├── h-ssh-integration-advice.md              # h-ssh integration guidance
├── EXECUTIVE-SUMMARY.md                     # One-page project overview
├── H-CLI-DEVELOPMENT-EXPLAINED.md           # How the AI team built h-cli
├── AI-REVIEW.md                             # Instructions for AI model evaluators
├── ROADMAP.md                               # Feature status and planned work
├── SECURITY-HARDENING.md                    # Full security audit trail (45 items)
├── LLD.md                                   # This document
├── decisions/                               # Architecture decision records
├── proposals/                               # Design proposals
├── gifs/                                    # Visual demos
│   ├── configure-network.gif
│   ├── deploy-lab.gif
│   ├── netbox-discovery.gif
│   └── verify-topology.gif
└── test-cases/                              # Validated behavioral tests (13 cases)
    ├── autonomous-docker-restart.md
    ├── brevity-directive-output-savings.md
    ├── denylist-workaround-adaptation.md
    ├── eve-ng-credential-handling.md
    ├── gate-non-determinism.md
    ├── gate-vs-prompt-enforcement.md
    ├── layered-rules-gate-response.md
    ├── resume-vs-plaintext-context.md
    ├── scope-enforcement.md
    ├── self-access-enforcement.md
    ├── self-modification-prevention.md
    ├── self-rebuild-gate-off.md
    └── semantic-boundary-filtering.md
```

---

## File Responsibilities

### architecture.md

**Role:** Top-level system overview.

**Contents:**
- ASCII diagram of services across two Docker networks
- End-to-end message flow: Telegram → bot → Redis → dispatcher → Claude → firewall → core → back
- Context injection summary (four tiers: Redis history, session chunks, skills, vector memory)
- Network topology summary
- Full project tree with per-file descriptions

**Key facts documented:**
- Two networks: Redis bridges both, claude-code and Grafana on both, Core backend-only
- JSONL written as audit trail, never replayed
- Context injected as plain text (71% fewer tokens)

### security.md

**Role:** Security model reference — what's enforced, where, and how.

**Contents:**

| Section | What it covers |
|---------|---------------|
| Ground rules layers | 4-layer TCP/IP-inspired safety stack (Base Laws → Security → Scope → Behavior) |
| Enforcement points | System prompt (documentation) vs Haiku gate (enforcement) — why they're separate |
| Asimov firewall | Pattern denylist (deterministic) + Haiku gate (semantic) |
| Network isolation | Frontend/backend network split |
| Auth | `ALLOWED_CHATS` allowlist, fail-closed |
| Container privileges | Per-container: user, capabilities, rootfs, network access |
| Data access | Per-container: Redis access, filesystem writes, secrets held |
| Sudo whitelist | Core-only, fail-closed, resolved to full paths |
| HMAC signing | Dispatcher signs results, bot verifies — prevents Redis spoofing |
| Integrations | NetBox, Grafana, EVE-NG — tokens isolated in core |

**Key design insight documented:** Lower layers always win. A Layer 4 directive (be helpful) cannot override a Layer 1 law (don't destroy infrastructure). The layer hierarchy is the conflict resolution mechanism.

### configuration.md

**Role:** Environment variable reference for deployment.

**Structure:**
- Required variables (3): `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHATS`, `REDIS_PASSWORD`
- Optional variables (10): timeouts, session TTL, gate toggle, pattern files, HMAC key, base image
- Integration variables (6): NetBox, Grafana, EVE-NG URLs and credentials
- Claude Code authentication: OAuth token setup via `docker compose run`

### context-injection.md

**Role:** Design rationale for the plain-text context injection approach.

**Contents:**
- Problem statement: `--resume` replays full JSONL (130K tokens, $0.71/message for a 73-turn conversation)
- Solution: inject history as formatted markdown, fresh `--session-id` each call
- Benchmark results: 71% fewer input tokens, 81% cheaper
- Four-tier implementation: Redis history (< 24h, 30KB cap) → session chunks (> 24h, 50KB cap) → skills (per-message, 20KB) → vector memory (permanent)
- JSONL still written as audit trail, never replayed
- Trade-off analysis: loses tool call details, gains massive token/cost savings

**Quantitative data:**

| Metric | --resume | Plain text | Savings |
|--------|----------|------------|---------|
| Input tokens | 130,312 | 37,207 | 71% |
| Cost per call | $0.7153 | $0.1385 | 81% |

### eve-ng-automation.md

**Role:** Operational guide for EVE-NG lab automation.

**Contents:**
- Golden rule: SSH + `unl_wrapper` is 6x faster than REST API
- SSH command reference: lab management, node control
- Dynamic console port discovery via `pgrep -a qemu_wrapper`
- Console automation via h-ssh telnet transport + raw Python sockets
- EVE-NG interface mapping: `id="0"` = fxp0 (management), `id="1"+` = data plane
- P2P topology XML: invisible bridges for point-to-point links
- REST API reference (read-only use only — broken for writes)
- Juniper vJunOS quirks and SSH bootstrap
- Complete autonomous workflow example

### netbox-integration.md

**Role:** Operational guide for NetBox device management.

**Contents:**
- Golden rule: HTTP 200 doesn't mean success — always verify actual state
- Device structure: prerequisites (manufacturer, device type, role, site)
- Device lifecycle: create, assign primary IP, delete
- Cable management and the orphaned cable problem
- Complete automation workflow and API patterns

**Key gotcha documented:** Deleting a device cascades to interfaces and IPs but NOT cables. Orphaned cables must be cleaned up manually.

### gifs/

**Role:** Visual demonstrations of h-cli capabilities.

| File | Demonstrates |
|------|-------------|
| `deploy-lab.gif` | Lab deployment workflow |
| `configure-network.gif` | Network configuration automation |
| `verify-topology.gif` | Topology verification |

Binary assets used in README and external documentation.

---

## Test Cases Directory

The `test-cases/` directory contains 13 validated behavioral tests. Each documents a real scenario, the observed behavior, and the architectural conclusion.

### Security Enforcement (6 tests)

| File | Tests | Key finding |
|------|-------|-------------|
| `gate-vs-prompt-enforcement.md` | Can Sonnet self-enforce ground rules? | No — gate OFF → agent immediately violated S2 |
| `self-access-enforcement.md` | S2 rule blocking h-cli source access | Consistent blocking; agent learned to self-censor |
| `self-modification-prevention.md` | Blocking access to private git repo | Gate inferred risk from context without explicit rule |
| `self-rebuild-gate-off.md` | Self-modification with gate disabled | Agent rebuilt own container, killed itself. One LLM cannot self-enforce |
| `scope-enforcement.md` | Blocking out-of-scope requests | Financial data blocked; in-scope alternatives offered |
| `semantic-boundary-filtering.md` | Semantic filtering across services | Intent-aware: same entity allowed/blocked by context |

### Optimization (2 tests)

| File | Tests | Key finding |
|------|-------|-------------|
| `resume-vs-plaintext-context.md` | JSONL replay vs plain text | 71% fewer tokens, 81% cheaper |
| `brevity-directive-output-savings.md` | Conciseness directive | 83% output reduction, 48% cheaper, 77% faster |

### Architectural Validation (4 tests)

| File | Tests | Key finding |
|------|-------|-------------|
| `gate-non-determinism.md` | Haiku gate consistency | Same command can get different verdicts — mitigated by denylist |
| `denylist-workaround-adaptation.md` | Model adapting around blocks | Agent restructured commands legitimately |
| `layered-rules-gate-response.md` | 4-layer rule structure | Gate differentiates intent on same host; layer citations in denials |
| `eve-ng-credential-handling.md` | Behavioral guidance | Agent proactively applied security rules without instruction |

### Incident Analysis (1 test)

| File | Tests | Key finding |
|------|-------|-------------|
| `autonomous-docker-restart.md` | Unprompted destructive action | Agent restarted Docker daemon unprovoked (gate was disabled) |

### Central Theme

Defense-in-depth: deterministic pattern blocking + semantic Haiku gate + behavioral prompt guidance. An independent enforcement layer is proven necessary — a single LLM cannot reliably self-enforce via prompt alone.

---

## Position in Message Flow

Reference: Architect LLD section 3 — end-to-end message lifecycle.

Docs don't participate in the runtime message flow directly. We document it. Each doc file maps to specific steps:

| Doc file | Covers steps | What it documents |
|----------|-------------|-------------------|
| `architecture.md` | All (1–9) | System diagram, network topology, flow summary, project tree |
| `security.md` | 1 (auth), 6 (firewall), 8 (HMAC) | 4-layer rules, container privileges, signing |
| `configuration.md` | All (env vars) | Every variable consumed across the flow |
| `context-injection.md` | 4 (context), 8 (storage) | Three-tier injection design + benchmarks |
| `eve-ng-automation.md` | 7 (core execution) | What core does when the command targets EVE-NG |
| `netbox-integration.md` | 7 (core execution) | What core does when the command targets NetBox |

**Our contract:** When any team changes behavior in their step, we update the corresponding doc. The Architect LLD section 3 is the source of truth for the flow — our docs must align with it.

---

## Interfaces

### Inbound: Other Teams → Docs

Docs reflect the current state of the system. When other teams change behavior, we update:

| Source team | What we document |
|-------------|-----------------|
| Architect | Ground rules layers, security model, blocked patterns, message flow |
| Orchestration | Dispatcher flow (bus.py/worker.py/dispatcher.py), context injection, session chunking |
| Interface | Bot commands, Telegram actions, teach mode |
| Core | MCP server, run_command tool, sudo whitelist |
| Data | Monitoring dashboards, metrics schema |

### Outbound: Docs → Operators

Documentation is consumed by:
- **Operators** deploying h-cli (configuration.md, security.md)
- **Contributors** understanding the architecture (architecture.md, test-cases/)
- **The bot itself** — architecture.md content informs the system prompt

### Relation to Skills

Both `docs/` and `skills/` are owned by the Knowledge team but serve different audiences:

| | docs/ | skills/ |
|-|-------|---------|
| Audience | Humans (operators, contributors) | The bot (injected into system prompt) |
| Format | Long-form markdown, diagrams, tables | Concise, keyword-tagged, actionable |
| Loading | Read manually or via git | Loaded automatically by dispatcher per message |
| Budget | No limit | 20 KB per message |

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Flat file structure** | Simple navigation; `test-cases/` is the only subdirectory |
| **Test cases as markdown** | Reproducible documentation of real scenarios — validated observations, not automated tests |
| **Quantitative benchmarks** | Token counts, costs, and timings make decisions evidence-based |
| **Integration guides separate** | EVE-NG and NetBox are optional — dedicated files avoid cluttering core docs |
| **GIFs for demos** | Visual proof of autonomous workflows |
| **No auto-generation** | All docs hand-written for accuracy |

---

## Documentation Standards

- Keep documentation concise and accurate
- Update docs when other teams change behavior that affects documentation
- Use markdown formatting consistently
- Include examples where helpful
- Document interfaces between teams when cross-cutting
- Quantitative evidence preferred over qualitative claims
