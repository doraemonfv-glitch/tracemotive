# Security Policy

## Reporting a vulnerability

Please do not report suspected security vulnerabilities in a public Issue, Discussion, or pull request. Use GitHub's Private Vulnerability Reporting instead: open the repository's **Security** page and submit a private report.

Please include, when available:

- The affected TraceMotive version or commit
- A concise description of the vulnerability
- The potential impact
- Reproduction steps or a minimal proof of concept
- Relevant environment information
- A possible mitigation or fix

Do not include real API keys, passwords, tokens, personal data, or captured Agent content in a report. Use redacted or synthetic examples whenever possible.

## Security-sensitive areas

Security and privacy reports may involve, for example:

- Secret and credential redaction
- Content capture
- The local transport and loopback-only Collector
- SQLite persistence
- The Query API
- Rendering of untrusted captured content
- Framework adapters
- Failure isolation

## Supported versions

The current public TraceMotive v0.1 release is supported for security reports.

## Disclosure

Please avoid public disclosure while maintainers review a private report. Do not move details to a public Issue or pull request before a fix or mitigation has been coordinated with the maintainers.

TraceMotive's security posture includes these important invariants:

- The Collector is loopback-only.
- TraceMotive is disabled by default.
- Content capture is independently disabled by default.
- Redaction occurs before the transport queue.
- Tracing failures must not fail Agent execution.
- Captured content is treated as untrusted.
