# Shared HTTP gateway

The source of truth for the authentication, configuration, and forwarding contract of `server.py`. Public routes, the Cloudflare Service URL, ports,
secret settings, and starting and updating nginx and the LaunchAgent are covered in the
[public route and deployment doc](../docs/CLOUDFLARE_TUNNEL.md).
The gateway is a standalone process that does not depend on concrete agents, the DB, or TrackEverything imports, and it
forwards HTTP and streaming responses.

## Authentication contract

Cloudflare owns access restriction on the public URL. The actual public path prefix is kept in the secret settings,
and nginx strips that prefix when forwarding. The prefix is not a local authentication condition.
No arbitrary URL key or cookie value is verified locally.

For ordinary GET / HEAD / POST / PUT / PATCH / DELETE / OPTIONS requests,
the gateway forwards the request if **any one** of the following is valid.
If all are invalid, it returns 401 and does not connect upstream. An unset signing secret cannot authenticate.

- **G**: verifies the GitHub raw body and `X-Hub-Signature-256` with HMAC-SHA256.
- **S**: verifies the Slack raw body, `X-Slack-Request-Timestamp`, and `X-Slack-Signature`.
  The absolute clock skew must be 300 seconds or less. The API token and the Signing Secret are different values.
- **A**: compares `Authorization: Bearer <token>` in constant time with the value in the owner-only settings.
- **L**: when the owner calls `GET /__gateway__/login-key` authenticated by G, S, or A,
  the gateway returns a one-time login key (default 180 seconds), the public URL path, and `password_configured`.
  `GET /login/<key>` serves an input page for the fixed password (`credentials.login_password`; its source of truth is
  `GATEWAY_LOGIN_PASSWORD` in `.env`, which setup.py's `--upstream-env` syncs). Only a POST with
  **both the key and the password correct** receives the `__Host-gateway-session` cookie
  (default 7 days, HttpOnly, Secure, SameSite=Lax) and is redirected to `../` with 303, and the key expires.
  While no password is set, the entrance is closed (503), and 8 wrong entries invalidate the key.

One failure does not cancel another valid method. Keys and sessions both live only in the process
memory, and restarting the verification service invalidates all of them (this is the revocation procedure).
A login key can be used only once, when the password passes, and is not consumed by a wrong entry (it expires after 8).
Reserved paths (`/login/…`, `/__gateway__/…`) are not forwarded upstream, and cookies are not passed upstream.
Chat link-preview crawlers (User-Agents listed in `UNFURLER_MARKERS`, such as Discordbot) receive
a no-op 200 without a cookie (so they cannot be used as a validity probe either). In addition, agents follow the convention of
pasting URLs in backtick code spans, so previews are not fetched in the first place.

Only regular PR review reports are forwarded anonymously, for GET / HEAD requests that exactly match the target route's
`public_read_patterns`. The random ID in the HTML doubles as the read permission for that one report.
Opening the link repeatedly does not consume it, and it never issues a login key or session for the management UI.
Report links are sent to Discord wrapped in angle brackets to suppress previews.

Signature verification follows the [GitHub](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries) and
[Slack](https://docs.slack.dev/authentication/verifying-requests-from-slack/) contracts.
nginx passes the raw body to the verification service.

## Configuration contract

`create_app(config)` reads `credentials` and `routes`.
`credentials.github_secret` / `slack_signing_secret` / `bearer_token` are strings, and startup verifies that
at least one of them is non-empty.
An empty signing secret does not accept the corresponding method.

`routes` manages destinations with `prefix`, `upstream`, `strip_prefix`, `headers`, and `enabled`.
The longest prefix wins. A disabled route returns 503, and no match returns 404.
In either case there is no fallback to another route. Credential and route changes are loaded on process restart.

`public_read_patterns` is a per-route list of regular expressions, empty by default. Invalid types or syntax are
rejected at startup. Exact matches are evaluated only against the enabled route with the longest prefix, and anonymous access is allowed
only for GET / HEAD when the raw path is identical to its canonical ASCII path. Percent encoding,
duplicate slashes, and relative segments are never allowed anonymously. The forwarded path after `strip_prefix` keeps the same path.
A route's public settings never carry over to disabled routes or other services.

setup configures the following two patterns only for the default app route with `prefix=/` and `upstream=http://127.0.0.1:18080`.
Existing credentials, other routes, and upstream headers are kept.

```json
"public_read_patterns": [
  "/reports/[0-9a-f]{64}\\.html",
  "/reports/assets/[0-9a-f]{64}/mermaid\\.min\\.js"
]
```

In an environment with a custom upstream, set the same configuration explicitly on the target app route.
The `/reports/` listing, `/static/`, the management API, and write methods are never allowed anonymously.

`public_path_prefix` is used to build the public URL path of a login key. Setting `public_origin`
(e.g. `https://track.example.com`) puts the full URL in the issue response.
`login_key_ttl_seconds` (default 180) and `session_ttl_seconds` (default 7 days) are
the lifetimes of login keys and sessions.

## Forwarding and logging

Destinations are limited to HTTP/HTTPS URLs with a configured numeric loopback IP and port.
Forwarding loops back to the shared entrance or the verification service are rejected at configuration time.
Path prefixes are matched on segment boundaries and stripped when needed. The raw body, signature headers, and query are kept.
The gateway consumes its own Bearer header and sets upstream headers from the route's `headers`.
TrackEverything routes get `X-Webhook-Token`, and clients cannot override it.
A stopped upstream yields 502 and a timeout 504. The body limit is 32 MiB by default.

nginx strips only the public prefix from the raw URI and passes the encoded path to the verifier without decoding it.
The access log does not record queries, and it records any path containing `reports/` as `/reports/[redacted]`,
regardless of the public prefix or percent encoding.
The error log stores crit and above in an owner-only file.
The verification service's access log is disabled. Error responses never contain secret values.
Authenticated responses are `private, no-store`, so shared caches cannot bypass authentication.
Only when the upstream `Cache-Control` contains `no-transform` is that directive kept as well; no other cache permissions are carried over.

## Acceptance cases

H17–H21 in `tests/api/test_gateway.py` check valid and invalid Bearer tokens, GitHub and Slack signatures,
and rejection of a body modified after signing. They use TestClient and HTTPX MockTransport and never connect to a real upstream.
H10 and H11 in `tests/api/test_information.py` check reading a fixed report and rejecting paths outside the public directory.
Real nginx, login session persistence, and exhaustive combinations of authentication conditions are outside the automated suite.
