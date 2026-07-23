# Ariadne

Ariadne is an owner-controlled, recoverable development workflow for Discord:

```text
Discord Thread -> strong model PLAN -> owner approves TASK -> Hermes implementation
               -> deterministic scope/tests -> strong-model review -> Draft PR -> CI/preview -> owner accept
```

It is being extracted from LuxrayKit as a reusable Python project.  Runtime
state, worktrees, transcripts, credentials, and target-project checkouts stay
outside this repository.  The source repository is never a model write target.

The current migration intentionally has three phases:

1. Build and test the standalone compatible engine here.
2. Run a harmless end-to-end dogfood in an isolated fixture repository.
3. Switch the VPS to Ariadne while retaining the r9 release and the existing
   SQLite state as rollback material, then run a LuxrayKit docs-only Draft PR.

Do not use Ariadne as a merge channel until the relevant dogfood phase has
passed.  `!accept` remains an explicit owner-only command even after CI and
preview are green.

Useful entry points:

- [Architecture and lifecycle](docs/architecture.md)
- [Project profile contract](docs/reference/project-profile.md)
- [LuxrayKit migration/cutover](docs/migration/luxraykit-cutover.md)
- [Fixture dogfood procedure](docs/dogfood.md)
- [Source-document audit](docs/migration/luxraykit-source-audit.md)
