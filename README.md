# Local PR Loop

`local-pr-loop` is a vendor-neutral [Agent Skills](https://agentskills.io) package for an asynchronous, repository-local owner/reviewer code-review loop. It keeps durable review conversations and immutable history in JSON, checks source drift, records deadlines, and regenerates a skim-first Markdown summary of the whole loop on every publication.

## Install

Install or copy this repository as a skill named `local-pr-loop` in the directory your agent uses for Agent Skills. The repository root is the skill directory: it contains `SKILL.md`, `scripts/`, and `references/`.

The package requires Git and Python 3.9 or newer. Its helpers use only the Python standard library; it does not require an account, network access, a hosted pull request service, or an agent-vendor SDK.

## Vendor-specific adapters

The workflow remains vendor-neutral: its portable behavior lives in `SKILL.md`, `scripts/`, and `references/`. Files beneath `agents/` are optional client-specific adapters that other Agent Skills implementations may ignore.

`agents/openai.yaml` provides OpenAI and Codex interface metadata. Keep workflow rules out of this file, and declare a product-specific dependency only when the portable skill genuinely requires it. When updating the skill, review the adapter's display name, description, default prompt, policy, and dependencies so they remain aligned with `SKILL.md`. Add future vendor adapters as separate, clearly named files without changing the portable workflow contract.

## Use

Assign the agent either the **owner** or **reviewer** role and ask it to use `local-pr-loop` for the target repository. Findings remain stable conversation threads across owner replies and reviewer decisions. The skill creates review artifacts beneath the target repository's `.local/reviews/` directory and progresses until every thread is resolved at `LGTM` or the recorded timeout.

See [SKILL.md](SKILL.md) for the workflow and [references/](references/) for the event and source-state contracts.

## License

MIT. See [LICENSE](LICENSE).
