# Security Policy

## Supported Version

Security fixes are provided for the latest released version, currently 0.3.x.

## Reporting

Report vulnerabilities privately through a
[GitHub security advisory](https://github.com/PavluntiyJ/dotameta/security/advisories/new).
Do not open a public issue for an unpatched vulnerability.

Never include live OpenDota or Stratz credentials in a report. Revoke any token
that may have been exposed. Credentials belong only in environment variables or
the allowlisted local `.env` file, which is ignored by Git.

The response caches can contain personal match history or hero aggregates. Use
`dotameta cache --clear` to remove both API caches, or `--no-cache` when local
persistence is inappropriate. Do not attach cache files containing another
person's data to a report.
