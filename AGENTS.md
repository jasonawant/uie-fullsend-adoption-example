# Repository Guide

This repository demonstrates a per-repository Fullsend installation and its
GitHub Actions integration.

## Verification

Run these commands before committing changes:

```bash
yamllint .github/ .fullsend/
markdownlint "**/*.md"
```

The commands use `.yamllint` and `.markdownlint.json` for repository-specific
rules. CI runs the same checks for pull requests and pushes to `main`.

## Workflow Changes

- Keep changes to Fullsend-managed workflow files minimal.
- Do not add secrets, tokens, or credential values to repository files.
- Validate YAML after changing workflows or `.fullsend` configuration.

## Git Conventions

- Name agent branches `agent/<issue>-<slug>`, for example
  `agent/4-verification-scaffolding`.
- Use imperative, scoped commit messages when applicable, for example
  `ci: add repository lint checks`.
- Keep commits focused on one logical change.
