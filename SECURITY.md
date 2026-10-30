# Security Policy

## Supported versions

Stele is developed on the `main` branch. Security fixes are applied to `main`
only; there are no maintained release branches yet.

## Reporting a vulnerability

Please do **not** open a public issue for security vulnerabilities.

Report privately through GitHub Security Advisories:
https://github.com/Sero3105/stele/security/advisories/new

Include, where possible:

- a description of the issue and its impact;
- steps to reproduce, with a minimal sample file if relevant;
- the affected commit or version and your macOS / Apple Silicon environment.

You can expect an initial response within 7 days. Please allow reasonable time
for a fix before any public disclosure.

## Scope notes

Stele is a local-first application: the server binds to `127.0.0.1` only and
processes user-supplied documents offline. Reports are especially welcome for:

- path traversal or arbitrary file read/write via uploaded file names or archives;
- unsafe handling of crafted PDF, Office, or image files;
- any way to make the server reachable from the network or to exfiltrate documents.

Model weights are downloaded from Hugging Face by `make models` and are outside
the scope of this repository; report weight or upstream library issues to their
respective projects.
