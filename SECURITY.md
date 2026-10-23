# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's **Security**
tab and choose **Report a vulnerability**. Do not open a public issue. We aim to acknowledge
reports within three working days and to agree on a disclosure date with you.

## Supported versions

Only the latest minor release receives security fixes while the project is pre-1.0.

## Scope and trust boundaries

In scope, for example:
- reaching internal networks through `/v1/fetch` or the MCP `fetch` tool (the SSRF guard),
  or any other way a caller can choose an upstream host;
- bypassing API-key authentication, archive ownership, quotas or upstream budgets;
- secrets (client keys or provider keys) appearing in logs, responses or stored records;
- script injection through rendered HTML or Markdown output;
- denial of service that one caller can trigger cheaply for all others.

Operator configuration is trusted: upstream URLs such as `FAIRSERP_SEARXNG_URL`,
`FAIRSERP_NOMINATIM_URL` and proxy settings are chosen by whoever runs the server and are
not treated as attacker input. `auth_disabled` is meant for loopback-only development.

Out of scope: the content of third-party search results, and provider outages.

## Hardening notes

See the "Guarantees" section of the README for what the server enforces. Adversarial review
findings and their regression tests live in `tests/test_hardening.py`.
