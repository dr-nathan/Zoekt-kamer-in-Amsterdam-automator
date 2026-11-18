# Security

## Reporting a vulnerability

Use this repository's **Security → Report a vulnerability** option when private vulnerability
reporting is available. If it is unavailable, open an issue asking the maintainer for a private
reporting channel, without including exploit details, credentials or personal data.

Include the affected commit or version, the relevant route or file, a minimal reproduction using
synthetic data, the impact and a proposed fix. Redact tokens, cookies, e-mail addresses and listing
content. Avoid attaching database copies, browser profiles, HAR files or full environment dumps.

Publishing this policy does not authorize testing a deployed website. Agree on the host, testing
window and permitted operations with its operator before any live assessment.

## Keeping credentials and data private

Keep provider keys and session-signing secrets in deployment environment files outside git. Use
`.env.example` for names and empty or clearly fake sample values. Browser cookies, saved sessions,
production group configuration, databases, subscriber data, visitor data, logs and security
assessment evidence must remain outside the repository. `.gitignore` reduces accidental additions;
it cannot remove files already committed.

Run the repository scan before publishing changes:

```bash
# Install Gitleaks from https://github.com/gitleaks/gitleaks/releases first.
python3 .github/scripts/scan_secrets.py --staged
python3 .github/scripts/scan_secrets.py
```

The first command checks staged changes. The second checks all locally reachable git history and
the current tracked filenames. Both force full secret redaction and produce no report files. They
read git data, without scanning ignored runtime folders. Fetch every relevant remote branch before
an audit; an incomplete clone cannot prove that the public history is clean. Pattern-based scanning
cannot detect every secret, encoded value or personal datum.

If a real credential reaches git, revoke or rotate it first. Investigate its use and remove it from
current files. Consider history cleanup only when the affected credential or personal data warrants
it and coordinate it with collaborators. Deleting a file does not invalidate a credential or erase
existing clones. Routine author attribution and deployment usernames alone do not justify rewriting
shared history. Use GitHub's private/noreply commit address for future commits if desired.

## Dependencies and checks

Runtime versions and package hashes are recorded in `requirements.lock`. Regenerate and review the
lockfile when changing dependencies, then run the application tests and a dependency audit before
deployment. CI checks the lockfile against published Python advisories on pushes, pull requests and
a weekly schedule. It also scans fetched git history with Gitleaks and blocks common runtime/private
artifact filenames. Scanner findings are not uploaded as artifacts.

Dependabot checks Python dependency declarations and pinned GitHub Actions. When reviewing a Python
dependency update, regenerate `requirements.lock` as well; this custom lockfile is not automatically
maintained by Dependabot.

GitHub secret scanning, push protection and private vulnerability reporting are separate repository
settings. The presence of these files does not mean those settings are enabled. Repository
administrators should review them in **Settings → Code security** and **Security**.
