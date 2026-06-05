# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅        |

FlowCraft is currently in MVP (alpha) stage. Security updates are provided for the latest release.

## Reporting a Vulnerability

**Do NOT open a public issue for security vulnerabilities.**

Please report security issues privately to the project maintainers:

1. Email: [INSERT SECURITY CONTACT EMAIL]
2. Include a detailed description of the vulnerability
3. Include steps to reproduce, affected versions, and potential impact
4. We will acknowledge receipt within 48 hours
5. We will provide a timeline for the fix within 5 business days

### Scope

Security vulnerabilities may include but are not limited to:

- Unauthorized access to the local API
- SQL injection in task/user data handling
- Code injection via tool execution
- Path traversal in file operations
- Exposure of API keys or secrets
- Insecure default configurations

### Best Practices for Users

1. **Never expose the FlowCraft API to public networks** — it runs on `127.0.0.1` by default for a reason
2. **Use environment variables for API keys** — never hardcode keys in source files
3. **Review tool permissions** — grant only necessary file/command access
4. **Keep dependencies updated** — run `pip list --outdated` periodically
5. **Audit approval requests** — pay attention to tool execution approvals

## Dependency Security

We aim to keep dependencies minimal:
- Core runtime: `fastapi`, `uvicorn`, `pydantic`, `httpx`
- Optional: `playwright` (browser), `pytest` (testing)

Dependencies are pinned with minimum versions. We monitor for CVEs in our dependency tree.
