# Security Policy

Please do not open a public issue for vulnerabilities that expose bridge tokens,
permit unauthenticated control, or leak local media information. Report them
privately through GitHub's security advisory feature for this repository.

PlayMac binds to the local network for Playdate access. Treat the bearer token as
a credential and do not publish `~/Library/Application Support/PlayMac/data.json`.
