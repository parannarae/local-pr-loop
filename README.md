# Local PR Loop

`local-pr-loop` is a vendor-neutral [Agent Skills](https://agentskills.io) package for an asynchronous, repository-local owner/reviewer code-review loop. It keeps canonical state and immutable history in JSON, checks source drift, records deadlines, and generates a Markdown report for the latest event.

## Install

Install or copy this repository as a skill named `local-pr-loop` in the directory your agent uses for Agent Skills. The repository root is the skill directory: it contains `SKILL.md`, `scripts/`, and `references/`.

The package requires Bash, Git, and Python 3.9 or newer. Its Python helpers use only the standard library; it does not require an account, network access, a hosted pull request service, or an agent-vendor SDK.

## Use

Assign the agent either the **owner** or **reviewer** role and ask it to use `local-pr-loop` for the target repository. The skill creates review artifacts beneath the target repository's `.local/reviews/` directory and progresses until `LGTM` or the recorded timeout.

See [SKILL.md](SKILL.md) for the workflow and [references/](references/) for the event and source-state contracts.

## License

MIT. See [LICENSE](LICENSE).
