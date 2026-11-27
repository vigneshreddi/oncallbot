# Optional Draft Fix PRs

## AI-written fixes (default)

During a `code` investigation the model may call the `propose_patch` tool with exact
edits (`old_text` → `new_text`) for one existing file per call, in any configured
service. The tool only validates and returns a proposal: `old_text` must appear exactly
once in the indexed source at `prod_commit`, the path must stay inside the service, no
credential patterns, at most 10 edits of 5,000 characters. Errors are returned to the
model so it can correct the edit. The model has no tool that writes to GitHub.

When the investigation finishes, accepted proposals are stored in
`investigator.fix_proposals`. With `FIX_PR_MODE=auto` (default) the investigator reads
each file from `pr_target_branch` through the Contents API, re-applies the edits (409 if
they no longer match), and opens one draft PR per service through the checks below,
titled and described as **AI-proposed** and not yet reviewed. With
`FIX_PR_MODE=approve` it waits for `POST /incidents/{id}/open-pr`.
`GET /incidents/{id}/fix` shows proposals, status and PR links;
`POST /incidents/{id}/reinvestigate` re-runs an investigation after removing the previous
run's evidence, AI exchanges and unopened proposals. Nothing is merged or deployed.

## Reviewer-submitted fixes

Draft PR creation is a human-only API operation, not an agent tool. It is disabled
by default. No LLM patch is automatically accepted or applied. A reviewer must
inspect any suggestion and submit explicit structured file replacements.
There is no merge, deployment, shell execution, or production mutation path.

## Credentials and Local Opt-In

The new keys in `.env.example` are blank. Put credentials only in git-ignored
`.env` or an explicitly managed runtime Secret. Set `FIX_PR_ENABLED=true`, a
separate `GITHUB_WRITE_TOKEN`, and `FIX_EVIDENCE_BASE_URL` in the investigator
process environment only when intentionally enabling this operation. `up.ps1`
loads these keys from `.env` into the `app-secrets` Secret used by the investigator.

The write token must differ from the existing read-only `GITHUB_TOKEN`. Use a
fine-grained token restricted to the configured repositories, with Contents and
Pull requests write permissions. Never send tokens in the request body.

Reviewer confirmation is an explicit acknowledgement, not authentication.
Use the existing private/local API access boundary; do not expose this endpoint
publicly. The evidence base URL must be HTTPS with no userinfo, query, or
fragment. Protect the linked incident report with the appropriate access controls.

## Repository and Revision Contract

Only the incident service entry in `config/services.yaml` can select a repository:
`repo_url` must be an exact `https://github.com/OWNER/REPO` URL (optional `.git`),
`repo_path` must be a safe relative directory, and `prod_commit` must be a full
40-character deployed commit SHA. Missing revisions, synthetic commits, and
`local-demo` are refused. The reviewer must explicitly configure `pr_target_branch`
on that service entry; there is no default-branch fallback.

Analysis continues to use the deployed SHA. The request must attest that SHA.
GitHub must resolve that exact commit and show it is an ancestor of the maintained
branch. The PR starts at the maintained branch head, not at an old deployed SHA.
Each existing replacement requires its expected blob SHA from the maintained
branch; a new file uses `null`. A moved branch or changed file requires review again.

## Request

`POST /incidents/{incident_id}/propose-fix` requires a completed (`analyzed`)
incident and JSON with these fields. Unknown fields are rejected.

- `reviewer_confirmed`: the JSON boolean `true`, after human review.
- `analysis_sha`: the configured real deployed SHA, not a branch name.
- `target_branch`: the exact configured `pr_target_branch`, such as `release/stable`.
- `files`: 1 to 20 objects containing `path`, complete UTF-8 `content`, and
  `expected_blob_sha` (40-character SHA for an existing file, `null` for a new file).

Paths are repository-relative and must be within the configured service directory.
Absolute paths, dot segments, hidden files, backslashes, encoded traversal,
duplicates, symlinks, submodules, and non-regular replacements are rejected.
The total content limit is 200 KB. Common credential patterns are refused, but
this heuristic is not a guarantee that arbitrary submitted content is safe:
reviewers must remove secrets and sensitive DB values before submitting files.

The Git Data sequence is: create a new branch ref at the maintained SHA, create
blobs, create a tree, create a commit with that parent, update only the new branch
ref without force, then open a pull request with `draft: true` and explicit
head/base branches. The generated title, commit message, and body contain only
fixed text, the incident ID, commit SHAs, and a sanitized evidence link. Logs,
DB rows, report text, and model output are not copied into the PR description.

Success returns HTTP 201 with the PR number, canonical GitHub URL, new branch,
draft status, deployed analysis SHA, and maintained parent SHA. Disabled or missing
credentials return 403; invalid paths/config return 422; stale revisions or
unfinished analysis return 409; GitHub failures return sanitized 502 errors.
GitHub operations are not atomic and are not retried automatically. A failed
request may leave a draft branch or orphan Git objects; inspect the repository
before retrying. Human review and normal CI remain mandatory.

## This repository: draft PRs against `main`

`config/services.yaml` already points every service at
`https://github.com/vigneshreddi/oncallbot` with `pr_target_branch: main`
(`main` is treated as production code). It still uses `prod_commit: local-demo`,
which the PR endpoint refuses. To switch to the real pushed commit:

1. Commit and push everything so the working tree is clean and `HEAD` equals `origin/main`.
2. `./scripts/use-pushed-commit.ps1` writes git-ignored `config/services.deployed.yaml`
   with `prod_commit` = that SHA and `commit_kind: git`. It refuses dirty trees or unpushed commits.
3. `./scripts/up.ps1`, `./scripts/index-code.ps1`, `./scripts/start-investigator.ps1`
   (all three prefer the deployed registry when it exists).
4. In `.env`: `FIX_PR_ENABLED=true`, `GITHUB_WRITE_TOKEN` (fine-grained, this repository only,
   Contents + Pull requests read/write, different from `GITHUB_TOKEN`) and an HTTPS
   `FIX_EVIDENCE_BASE_URL`. Run `up.ps1` and `start-investigator.ps1` again to load them.
5. Get the expected blob SHA of each file to replace: `git rev-parse origin/main:services/order-service/src/main/java/dev/incidentlab/orders/PlaceOrderController.java`.
6. `POST http://localhost:18090/incidents/<id>/propose-fix` with `reviewer_confirmed: true`,
   `analysis_sha` = the deployed SHA, `target_branch: "main"` and the full new file contents.
   On Windows, convert CRLF to LF before sending (Git stores LF); otherwise the PR rewrites every line.
   The full PowerShell command is on the "AI & GitHub setup" page of `docs/architecture.html`.

`./scripts/use-pushed-commit.ps1 -Revert` returns to `local-demo`. Committing new code
moves `HEAD`, so run the script again after each push. Protect `main` on GitHub with
required reviews so a draft PR can never reach production without approval.
GitHub does not enforce rulesets on private repositories of personal free accounts.
Without that protection, the code still only writes to new `incident-fix/*` branches,
but the write token itself could push to `main` if leaked: keep it scoped to this one
repository, give it a short expiry, and revoke it when not testing. Making the
repository public or moving it to a GitHub Team organization enables enforcement.

## Verification

Run `python -m unittest discover -s investigator/tests -p test_fixes.py -v` in the
configured Python environment. All GitHub requests use mocked HTTP; no credentials
or actual external GitHub calls are needed for these tests.