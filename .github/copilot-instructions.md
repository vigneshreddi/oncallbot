# Repository rules for code generation

## Credentials — absolute rule
- Never write passwords, API keys, tokens, connection strings with passwords, or private keys into source, manifests, Dockerfiles, Postman files, docs, or tests.
- All credentials come from `.env` (git-ignored) or Kubernetes Secrets created at deploy time by `scripts/up.ps1`.
- `.env.example` holds placeholder keys only. When a new credential is needed: add the key to `.env.example`, read it from the environment in code, document it in `README.md` under "Credentials".
- Before finishing any change, check: `git diff` contains no secret-looking values; `.gitignore` still covers new secret files.

## Network / exposure
- Kubernetes Services are `ClusterIP` only. No `NodePort`, `LoadBalancer`, `hostNetwork`, `hostPort`, Ingress.
- Namespace NetworkPolicy denies all ingress from outside the cluster. Outbound internet from pods is allowed (LLM APIs, GitHub, package registries).
- No `privileged`, host mounts, Docker socket mounts.

## Runtime hardening
- Containers run as non-root, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`, read-only root FS with explicit `emptyDir` for `/tmp`.

## Dependencies
- Pin versions in `pom.xml` and `requirements.txt`. Prefer base images with no critical/high findings in `docker scout cves`.

## Architecture explorer — keep in sync on every change
- `docs/architecture.html` is the living, plain-English map of this project. Any change that adds, removes, renames or changes the behaviour of a service, script, workflow, integration, data store, security control or status MUST update that file in the same change.
- Update: the affected component card(s) in `components` (both `plain` and technical text, `status`, `files`), and the diagram views if boxes or arrows change.
- Write the `plain` text for a non-technical reader: short sentences, explain directly what the part does and why, no jargon or acronyms without an explanation. Do not use stories, metaphors or analogies.
- Every path listed in `files` must exist. Keep the page free of credentials, external scripts and network calls.
