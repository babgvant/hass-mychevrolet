# Native GM login investigation

On November 25, 2026, an account sign-in capture confirmed the following journey. This document records endpoint shapes and field names only. The capture, credentials, codes, cookies and transaction values are not repository fixtures.

1. The PKCE authorization request returns a `CombinedSigninAndSignup` page with JSON `SETTINGS` containing `csrf`, `transId` and a policy tenant path.
2. `POST {tenant}/SelfAsserted?tx=…&p=…` submits `request_type=RESPONSE`, `logonIdentifier` and `password`, with the transaction's CSRF header and cookies. Success is JSON `status: "200"`.
3. `GET {tenant}/api/CombinedSigninAndSignup/confirmed?csrf_token=…&tx=…&p=…` returns an authenticator page with fresh transaction settings. Its `SA_FIELDS.AttributeFields` includes `otpCode`.
4. Another SelfAsserted POST submits `otpCode` and `request_type=RESPONSE` using the new transaction.
5. `GET {tenant}/api/SelfAsserted/confirmed` returns a 302. Its request URL remains HTTPS, but its response Location uses `msauth.com.gm.mychevrolet://auth/?state=…&code=…`. The trailing slash differs from the requested callback and previously failed our validator.

The adapter uses this journey without executing page JavaScript. Each flow has an isolated, memory-only cookie jar with B2C-compatible unquoted cookies. HTTP sessions close after each request. Credentials are submitted once per explicit user action and not retained by the adapter. MFA asks for a current authenticator code rather than a setup secret. The existing PKCE, state, nonce, issuer, audience and signing-key checks apply to the final exchange. Only the fixed GM authentication origin receives requests; redirects are never followed automatically. Unknown pages, other MFA methods and blocked requests fall back to browser/token sign-in. There is no automated retry or browser fingerprint emulation.

## Validation status

The account capture demonstrates the browser journey, not successful native authentication. An anonymous aiohttp authorization GET returned HTTP 200 and its transaction parsed successfully. An empty-credential transport probe stopped with a browser-required/unsupported response; it did not authenticate an account or establish native viability. Upstream also reports browser automation and access-denied failures: [OnStarJS](https://github.com/BigThunderSR/OnStarJS) and [authentication issue](https://github.com/BigThunderSR/homeassistant-addons-onstar2mqtt/issues/1014).

Run `python scripts/probe_native_login.py` explicitly to repeat the anonymous check. It submits one empty credential form and prints a sanitized outcome. It never reads the capture or accepts account credentials. Do not run it repeatedly against a block.

Native sign-in is labeled experimental and the browser path remains first in setup. Before changing that default, complete a native password/authenticator sign-in from the target Home Assistant instance, confirm vehicle discovery and token renewal, and exercise reauthentication with the same account. Email and SMS verification remain unsupported. Mocked tests verify protocol handling but cannot demonstrate that GM permits a native session.
