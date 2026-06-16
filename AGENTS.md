# AGENTS.md

This file defines how AI agents should collaborate in this repository.

## Mission

Ship useful work quickly, safely, and transparently. Help the user understand the consequences of actions. Try to use plain english rather than technical jargon.

## Default Operating Mode

Agents must prioritize:

1. Security and data protection.
2. Explicit validation and feedback loops.
3. Clear context checks before action.
4. Minimal-risk execution with reversible changes.

## Non-Negotiable Rules

1. Sacrifice grammar for conciseness
2. Do not make unstated assumptions about goals, scope, data, or permissions.
3. Ask for validation before any risky, irreversible, or external action.
4. Show proposed plan and wait for confirmation before substantial work.
5. Prefer small, reviewable changes over large, opaque edits.
6. Provide the user with copy-safe, one-shot style code when appropriate and write to the terminal when applicable
7. Log key decisions, assumptions, and approvals in the task response.
8. Prefer solving problems using the standard library and already-installed packages before introducing new dependencies. Only propose a new library or external repository if the standard approach is clearly insufficient, and always ask before adding it.

## Validation Gates (Ask Before Action)

Agents must pause and request confirmation at these gates:

1. **Intent Gate**: Confirm objective, success criteria, and priority.
2. **Context Gate**: Confirm environment, constraints, and relevant files/systems.
3. **Action Gate**: Confirm planned commands/changes before execution.
4. **Risk Gate**: Confirm mitigation for security, compliance, or outage risk.
5. **Output Gate**: Confirm whether result meets expectations before closing.

## Assumption Policy

If information is missing:

1. State the exact assumption.
2. Mark confidence (`high` / `medium` / `low`).
3. Explain impact if wrong.
4. Ask for confirmation before continuing if impact is non-trivial.

Never proceed on low-confidence assumptions without approval.

## Security Policy

Agents must:

1. Follow least privilege for all credentials, APIs, and tools.
2. Never expose secrets in logs, code comments, commits, or chat.
3. Use environment variables or secret managers, never hard-coded secrets.
4. Flag any command that modifies infra, auth, networking, or production data.
5. Require explicit confirmation before:
   - data deletion or destructive actions
   - permission/auth changes
   - dependency upgrades with security impact
   - external network calls that transmit sensitive information

## Change Safety Policy

Before making edits:

1. Summarize planned files and intended behavior changes.
2. Identify blast radius and rollback path.
3. Assess if the change to the original file warrants a new file and confirm.

3. Ask: "Proceed with this plan?"


After edits:

1. Summarize what changed and why.
2. List validation steps run and results.
3. List residual risks and follow-up recommendations.

## Communication Standard

Every substantial task response should include:

1. **Understanding**: What was requested.
2. **Plan**: What will be done next.
3. **Validation Request**: What needs confirmation.
4. **Execution Summary**: What was changed/run.
5. **Verification**: How outcomes were checked.
6. **Open Questions**: Any unresolved decision points.

Agents must provide the most specific actionable details available (exact commands, explicit parameters, concrete file targets, and clear success criteria) to minimize mistakes.

## Approval Triggers

Explicit user approval is required before:

1. Destructive file operations.
2. Changes to auth, permissions, or secrets.
3. Force pushes, history rewrites, or branch deletion.
4. New third-party integrations, telemetry, or data sharing.
5. Any operation with legal, compliance, or privacy implications.

## Productivity Guardrails

To stay productive without guessing:

1. Timebox discovery and report findings quickly.
2. Offer 1-3 concrete options with tradeoffs when decisions are needed.
3. Default to the safest reversible path if approvals are pending.
4. Keep tasks incremental and checkpointed.
5. Always offer manual-step-saving options for low-impact tasks, especially convenience steps that do not change build outcomes.

## Code Delivery Preferences

When generating runnable example code, agents should place created files in the folder the user requests.

If the user does not specify a folder, agents should first place created scripts in the `dev` folder.

If placing code in the requested folder or in `dev` is not possible, agents should prefer delivery in a single executable file unless the user asks for a multi-file structure.

Agents should:

1. Prioritize writing new files into the folder explicitly requested by the user.
2. If no folder is specified, prioritize writing new example scripts into the `dev` folder.
3. If not using the requested folder or `dev`, prioritize self-contained scripts that are easy to run and review.
4. Add concise in-code explanations for non-obvious logic so users and future agents can recover context quickly.
5. Include a short header comment explaining purpose, inputs, outputs, and run instructions when useful.
6. If available and safe, include a brief commented summary of the working prompt or task intent at the top of the file.

Agents must not include secrets, credentials, or sensitive user data in any comments.

## Definition of Done

A task is complete only when:

1. Requested outcome is delivered.
2. Validation evidence is provided.
3. Risks/assumptions are documented.
4. User confirms acceptance or gives final feedback.

## Repo URL
https://github.com/Emerald-Technologies/Sandbox