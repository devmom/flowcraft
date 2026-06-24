---
name: technical-documentation
description: "Build and review high-quality technical docs as well as agent instruction files in your repository."
category: text
version: "1.0.0"
author: "openclaw-ported"
script_path: ""
script_language: none
tags: ["git", "automation", "documentation", "testing", "code-review"]
timeout_seconds: 60
source: marketplace
enabled: true
---

# Technical Documentation

## Purpose

Produce and review technical documentation that is clear, actionable, and maintainable for both humans and agents, including contributor-governance files and agent instruction files.

## When to use

- Creating or overhauling docs in an existing product/codebase (brownfield).
- Building evergreen docs meant to stay accurate and reusable over time.
- Reviewing doc diffs for structure, clarity, and operational correctness.
- Running full-repo documentation audits that must include both governance files and product docs surfaces (`docs/`, `README*`, `.md/.mdx/.mdc`, Fern/Sphinx/Mintlify-style sources).
- Updating or reviewing AGENTS.md and/or CONTRIBUTING.md to keep agent and contributor workflows aligned with current repo practices.
- Improving repository onboarding/docs that include contribution instructions, issue templates, PR flow, and review gates.
- Designing governance documentation strategy for repos with alias instruction files (for example `CLAUDE.md`, `AGENT.md`, `.cursorrules`, `.cursor/rules/*`, `.agent/`, `.agents/`, `.pi/`) where `AGENTS.md` is treated as canonical when present and aliases should be kept as compatibility surfaces.
- Diagnosing agent-file drift where teams had to prompt iteratively to surface missing files, broken commands, or policy conflicts.
- Applying repository-specific documentation overlays, including OpenClaw page-type, docs IA, preservation, and validation rules when present.

## Workflow

1. Classify task: `build` or `review`; context: `brownfield` or `evergreen`.
2. Inventory full documentation scope early (governance + product docs): AGENTS/CONTRIBUTING/aliases plus docs directories, framework sources, and root/module READMEs.
3. Detect multilingual scope (README/docs in multiple languages) and define required parity level.
4. Read `references/agent-and-contributing.md` for agent instruction and `CONTRIBUTING.md` workflow rules (inventory, canonical/alias mapping, dual-mode balance, deliverable standards, and precedence/conflict handling).
5. Read `references/principles.md` for the governing ruleset (Matt Palmer & OpenAI).
6. For OpenClaw docs work, read `references/openclaw.md` before the build/review playbook.
7. For build tasks, follow `references/build.md`.
8. For review tasks, follow `references/review.md` and proactively detect issues without waiting for repeated prompts.
9. For complex or high-risk tasks (build or review), it is acceptable to run longer, deeper, and more exhaustive investigations when needed for confidence.
10. When available, use sub-agents for bounded parallel discovery/review work, then merge outputs into one coherent final deliverable.
11. Use `references/tooling.md` when platform/tooling choices affect recommendations.
12. Run a proactive issue sweep for both governance and docs-content surfaces, and fix high-confidence defects in the same pass unless explicitly asked for report-only mode.
13. In brownfield mode, prioritize compatibility with current docs IA, tooling, and release state.
14. In evergreen mode, prioritize timeless wording, update strategy, and durable structure.
15. Return deliverables plus validation notes, parity status, and remaining gaps.

## Sub-agent orchestration guidance

Prefer sub-agents when the repo is large or the requested change set is broad; use them by default for repo-wide, multi-framework, or high-conflict work.

- `inventory-agent` -> `agents/inventory-agent.md` (`fast` / Claude `haiku`): file/config discovery, coverage map, and missing-path checks.
- `governance-agent` -> `agents/governance-agent.md` (`thinking` / Claude `sonnet`): AGENTS/CONTRIBUTING/alias precedence, conflicts, and policy drift.
- `docs-framework-agent` -> `agents/docs-framework-agent.md` (`thinking` / Claude `sonnet`): framework config, relative path base, and file-path vs URL-path mapping checks.
- `synthesis-agent` -> `agents/synthesis-agent.md` (`long` / Claude `opus`): merge sub-agent outputs into one prioritized fix plan and unified precedence model.

## Inputs

- Doc type (tutorial, how-to, reference, explanation) and audience.
- File scope or diff scope.
- Docs framework/tooling constraints (Fern, Mintlify, Sphinx, etc.).
- Build/review mode and brownfield/evergreen intent.
- Target agent and human compatibility intent.
- Docs framework surfaces in scope (for example Fern, Sphinx, Mintlify, Markdown/MDX/MDC/RST/RSC files).
- Desired investigation depth/time budget (quick pass vs exhaustive review).
- Execution mode (`single-agent` or `sub-agent-assisted` when available).
- Remediation mode (`apply-fixes` by default, or `report-only` when requested).
- Multilingual scope: source-of-truth language, target locales, and parity expectations.
- Repository-specific overlay constraints, if any.

## Outputs

- Updated draft or review findings with clear next actions.
- Validation notes (what was checked, what remains).
- Navigation/maintenance recommendations for long-term quality.
- Governance-doc alignment summary when AGENTS/CONTRIBUTING were touched.
- Agent instruction-surface map (primary file, alias files, Codex/Claude/Cursor handling plan).
- Documentation-surface coverage map (what was reviewed under `/docs`, README hierarchy, and framework-specific source trees).
- Autodetected issue list with applied fixes (or explicit report-only findings).
- Delegation notes when sub-agents were used (scope delegated and how findings were merged).
- Multilingual parity note (in-sync, partial with rationale, or intentionally divergent).
- Repository-specific overlay notes when one was used.


## References


### agent-and-contributing

# AGENT and CONTRIBUTING Principles

This reference consolidates the core rules for agent-policy and contributor-governance docs.

You must:

1. Discover repo-level and nested instruction files with:
   `rg --files -g 'AGENTS.md' -g 'CONTRIBUTING.md' -g 'CLAUDE.md' -g 'AGENT.md' -g '.cursor/rules/*' -g '.cursorrules' -g '.agent/**' -g '.agents/**' -g '.pi/**' -g 'AGENTS.*.md'`
2. Read the root and nearest-scope `AGENTS.md`/`CONTRIBUTING.md` pair before editing.
3. If alias files exist, normalize to one canonical source (`AGENTS.md` preferred when present; otherwise nearest alias), plus compatibility pointers or explicit symlink notes.
4. Document conflicting instructions and precedence decisions.

## GitHub + AGENTS baseline

Source: https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors
Source: https://agents.md/
Source: https://github.blog/ai-and-ml/github-copilot/how-to-write-a-great-agents-md-lessons-from-over-2500-repositories/
Source: https://cobusgreyling.substack.com/p/what-is-agentsmd
Source: https://www.infoq.com/news/2025/08/agents-md/

Use these as default operating principles:

1. Keep `CONTRIBUTING.md` discoverable and actionable (`.github`, root, or `docs`).
2. Keep agent instructions concrete: real commands, real paths, clear boundaries.
3. Use explicit behavior boundaries for agents: `Always`, `Ask first`, `Never`.
4. Keep contributor and agent rules aligned with actual repository workflows.
5. Ensure clear guidance is provided to agents on if, when and how to raise issues and pull requests.

## Canonical and alias policy

Source: https://agents.md/
Source: https://github.blog/ai-and-ml/github-copilot/how-to-write-a-great-agents-md-lessons-from-over-2500-repositories/

1. Treat `AGENTS.md` as canonical when present.
2. If `AGENTS.md` is absent, treat the nearest alias file as canonical.
3. Keep compatibility surfaces explicit: `AGENTS.md`, `AGENT.md`, `.cursorrules`, `.cursor/rules/*`, `.agent/`, `.agents/`, `.pi/`.
4. If aliases are used, document how they map back to canonical policy (or symlink when supported).
5. When repos use `.agents/` as canonical rule storage, keep `.cursor` as a compatibility symlink to `.agents` for Cursor rule auto-loading.
6. Keep policy DRY: store one shared policy core and expose it via aliases/symlinks instead of duplicating rule text.

## Context-awareness by agent platform

Source: https://github.com/vercel-labs/agent-skills/blob/main/AGENTS.md
Source: https://github.com/openai/codex/blob/main/AGENTS.md

1. For Cursor and Claude-style glob consumers, keep rule files narrow and bounded.
2. Avoid over-referencing large path sets that inflate context for glob-based agents.
3. For Codex-style workflows, prefer explicit file references and deterministic commands.
4. Keep long runbooks outside top-level policy files; link to scoped docs.
5. Ensure all agents have a happy path regardless so ensuring everything works across

[... truncated]

### build

# Build Docs Playbook

Read `principles.md` first, then follow this execution flow.

## 1. Detect and align agent instruction and governance instructions

- Use `references/agent-and-contributing.md` as the source of truth for inventory, canonical/alias mapping, and precedence/conflict handling.
- Apply the symlink compatibility policy when in scope (`.agents` canonical directory with `.cursor` compatibility symlink when required by tooling).
- Long-running and extensive build investigations are acceptable when needed to resolve ambiguous or conflicting documentation sources.
- When available, use sub-agents for bounded parallel inventory/cross-check tasks and merge results into one canonical decision set.
- Capture required constraints before writing:
  - nested-agent rules, command/test requirements, PR workflow, and style checks.
- Use the same command and validation expectations in proposed snippets and examples.

## 2. Inventory product documentation surfaces (not governance only)

- For repo-wide builds, include docs content surfaces in addition to AGENTS/CONTRIBUTING.
- Inventory docs files and frameworks in scope (examples): `README*.md`, `docs/**`, `**/*.md`, `**/*.mdx`, `**/*.mdc`, `**/*.rst`, `**/*.rsc`, Fern/Mintlify config, Sphinx `conf.py`.
- Build a coverage map before drafting so governance and product docs are both represented.
- If scope is ambiguous, default to broader docs discovery first, then narrow intentionally.

## 3. Framework config and path mapping rules

- Detect framework/config first (for example Fern config, Sphinx `conf.py`, Mintlify config, or equivalent).
- Resolve every referenced path relative to the file/config that declares it, not assumed repo root.
- Treat filesystem paths and published URL routes as separate mappings; do not infer one from the other without config evidence.
- Validate both layers:
  - config -> file exists on disk
  - config/nav/routing -> URL path is consistent and reachable
- Record path-mapping assumptions and mismatches in handoff (`missing file`, `stale route`, `wrong base path`).

## 4. Define intent and success

- Audience, prerequisites, and job-to-be-done.
- Expected reader outcome immediately after completion.
- Doc type: tutorial, how-to, reference, explanation.
- Success criteria: what must be true after publish.

## 5. Build structure before prose

- Follow the funnel: what/why, quickstart, next steps.
- Keep headings informative and scannable.
- Open each section with the takeaway sentence.
- Add decision points with concrete branch guidance.
- For OpenClaw docs work, choose a page type from `references/openclaw.md` before drafting.
- Keep task-critical OpenClaw configuration inline; link exhaustive defaults, enums, schemas, generated references, and rare debugging workflows.

## 6. Build AGENTS.md and CONTRIBUTING.md intentionally

- Keep AGENTS.md structure consistent with `agents.md` ecosystem patterns:
  - include YAML frontmatter when present in repo style (`name`, `des

[... truncated]

### openclaw

# OpenClaw Documentation Overlay

Use this reference only for OpenClaw docs work. It layers OpenClaw-specific page
types, navigation, preservation, and validation rules on top of the general
technical-documentation skill.

## Reader Model

- Lead with the task the reader is trying to complete.
- Give one recommended path before alternatives.
- Keep main docs focused on the common path; move dense contracts and rare
  debugging detail to linked reference or troubleshooting pages.
- Explain production risks exactly where the reader can make the mistake.
- Link concepts, guides, references, CLI pages, SDK docs, testing, and
  troubleshooting so readers can continue without rereading.

## Page Types

Choose the page type before writing or reviewing:

- Overview: route readers to the right product area, integration path, or guide.
- Quickstart: get a new user to a working result with the fewest safe steps.
- Topic page: explain a major OpenClaw entity or surface end to end.
- Guide: walk through one workflow from prerequisites to production readiness.
- API/SDK/CLI reference: define every object, method, command, option, response,
  error, enum, default, and version rule in scope.
- Testing guide: show sandbox setup, fixtures, simulated failures, and live-mode
  differences.
- Troubleshooting guide: map observable symptoms to checks, causes, and fixes.
- Governance file: keep agent/contributor policy concrete, scoped, and aligned
  with current OpenClaw repo behavior.

## Topic Pages

Use this shape for major-entity pages:

1. Title naming the entity or surface.
2. Unheaded opening that says what it is, what it owns, and what it does not own.
3. Requirements, only when setup needs accounts, versions, permissions, plugins,
   operating systems, or credentials.
4. Quickstart with the recommended path and smallest reliable verification.
5. Configuration with task-critical options inline and exhaustive details linked
   to reference docs.
6. Major subtopics organized by reader intent, not under a generic "Subtopics"
   heading.
7. Troubleshooting with observable failures and concrete checks.
8. Related links to guides, references, commands, concepts, and adjacent topics.

## Guides

Use this shape for workflow pages:

1. Title naming the outcome, not the implementation detail.
2. Opening that states what the reader can accomplish.
3. Before you begin: accounts, keys, permissions, versions, tools, and
   assumptions.
4. Choose a path, only when the reader must decide.
5. Steps with verb-led headings, commands, expected output, and checks.
6. Test with the smallest reliable proof that the workflow works.
7. Production readiness: security, retries, limits, observability, migrations,
   and cleanup.
8. Troubleshooting near the workflow that causes the failures.
9. See also links to concepts, references, SDK docs, and adjacent guides.

## Docs IA And Navigation

- Read `docs/docs.json` before navigation changes.
- Keep topic pages and common workflows on the m

[... truncated]

### principles

# Documentation Principles

This reference consolidates the core rules used by this skill.

## Matt Palmer: 8 rules for better docs

Source: https://mattpalmer.io/posts/2025/10/8-rules-for-better-docs/

Use these as default operating principles:

1. Write for humans, optimize for agents.
2. Start with a funnel: what/why, quickstart, next steps.
3. Use Diataxis to scaffold content.
4. Write with AI, but structure for agents.
5. Offload routine docs operations to background agents.
6. Automate quality with CI.
7. Automate scaffolding and repetitive workflow tasks.
8. Make contribution easy and visible.

## OpenAI cookbook: what makes documentation good

Source: https://cookbook.openai.com/articles/what_makes_documentation_good

Key quality constraints:

- Prefer specific and accurate terminology over niche jargon.
- Keep examples self-contained and minimize dependencies.
- Prioritize high-value topics over edge-case depth.
- Do not teach unsafe patterns (for example, exposed secrets).
- Open with context that helps readers orient quickly.
- Apply empathy and override rigid rules when it clearly improves outcomes.

## Practical merge policy

When these rules conflict:

1. Preserve reader task success first.
2. Preserve structural clarity second.
3. Preserve long-term maintainability third.
4. Add agent optimization only if it does not reduce human clarity.

For agent-instructions and contributor-governance specifics (AGENTS/aliases/CONTRIBUTING), use `references/agent-and-contributing.md` as the detailed additional source of truth.

When the target repo or request is OpenClaw-specific, layer `references/openclaw.md` on top of these general rules. Otherwise ignore that repo-specific overlay.

## Execution policy for this skill

- Long-running and extensive investigations are allowed for both build and review work when needed to resolve ambiguity or cross-file drift.
- Use sub-agents when available for bounded parallel discovery, verification, or cross-source comparison.
- Keep one merged outcome: sub-agent outputs must be normalized into a single consistent recommendation/fix set.

## Multilingual parity rule

When docs exist in multiple languages, target cross-locale parity for task-critical content (steps, warnings, prerequisites, and limits). If full parity is not possible, publish explicit parity status and sync intent.


### review

# Review Docs Playbook

Read `principles.md` first, then apply this checklist.

## 1. Scope and classification

- Identify doc type and target audience.
- Confirm brownfield vs evergreen intent.
- Confirm expected outcome for the reader.
- For full-repo reviews, explicitly include both governance surfaces and product-doc surfaces (`docs/`, README trees, `.md/.mdx/.mdc`, `.rst/.rsc`, framework docs configs).
- For OpenClaw docs reviews, apply `references/openclaw.md` for page type, docs IA, preservation, examples, and validation checks.

## 2. Investigation behavior

- Proactively find issues and risks without waiting for repeated prompts.
- If there are signals of deeper problems, continue investigation beyond the first pass.
- Long-running and extensive investigations are acceptable when needed for confidence and correctness.
- When available, use sub-agents for bounded parallel discovery (for example file-inventory, command validation, or cross-doc consistency checks), then merge to one final issue set.
- When no issues are found, state that explicitly and call out residual risks or validation gaps.
- Default to `apply-fixes` for high-confidence documentation defects unless the user explicitly requests `report-only`.
- Do not stop at AGENTS/CONTRIBUTING checks when the task is documentation-wide; continue into docs-content and docs-framework surfaces.

## 3. Governance surface review

- Use `references/agent-and-contributing.md` as the source of truth for inventory, canonical/alias mapping, and precedence/conflict handling.
  For AGENTS.md:

- confirm persona intent, scope, and command/tool boundaries are explicit.
- check frontmatter style matches repo conventions when present.
- ensure `Always`, `Ask first`, and `Never` boundaries are present when expected.
- require concrete command examples and repo-specific paths to avoid ambiguity.

For CONTRIBUTING.md:

- verify issue/PR workflow is complete and actionable.
- ensure local setup, lint/test commands, and review criteria are accurate.
- ensure governance does not conflict with nested AGENTS instructions.
- flag oversized files that should be split into linked section docs (for example tool-specific setup and release docs).

For agent-platform awareness:

- confirm references are minimal and scoped for Cursor/Claude glob behavior.
- confirm Codex-facing guidance uses explicit file references.
- confirm both surfaces represent the same shared policy core (commands, boundaries, and precedence), not divergent guidance.
- audit `.agents`/`.cursor` compatibility behavior:
  - verify canonical rule directory and symlink state match repo policy
  - verify symlink target integrity and platform/tooling expectations
  - verify AGENTS policy references remain canonical for Codex even when `.cursor` compatibility exists
- check for context bloat from duplicated policy statements across agent and contributor files.
- check for conflicting rules, skills and agent instructions
- check for conflicting infor

[... truncated]

### tooling

# Documentation Tooling Guide

Source: https://www.mintlify.com/blog/top-7-api-documentation-tools-of-2025

Use this file when deciding build/review expectations for doc platforms.

## Tool-selection checkpoints

- Existing stack lock-in: do not force migration for minor gains.
- API workflow depth: generated references, OpenAPI support, testability.
- Collaboration model: docs-as-code, review workflow, versioning.
- Runtime quality: search, navigation, and copy-ready code snippets.
- AI readiness: structured content, stable URLs, machine-friendly layout yet human readable.
- Human readiness: reading complexity, reading UX, navigation depth, minimize jargon.

## Apply in brownfield mode

- Prioritize compatibility with the current platform.
- Use available components and style conventions before introducing new patterns.
- Propose migration only when current constraints block critical outcomes.

## Apply in evergreen mode

- Favor platforms and templates that make routine updates low-friction.
- Standardize section templates to reduce drift.
- Capture ownership, update cadence, and stale-content detection rules.

## Review implications

- Check whether content uses platform primitives correctly (tabs, callouts, endpoint blocks).
- Flag docs that are technically correct but hard to scan in the chosen platform.
- Recommend platform-specific improvements only when they reduce cognitive load.