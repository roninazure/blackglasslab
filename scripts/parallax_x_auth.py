#!/usr/bin/env python3
"""One-time X OAuth 2 PKCE bootstrap; never prints token values."""

from __future__ import annotations

import argparse
import os
from urllib.parse import urlencode

from parallax.social import SOCIAL_CONFIG_PATH
from parallax.x_auth import X_SCOPES, XCredentialStore, XOAuthClient, pkce_pair


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap private PARALLAX X OAuth 2 authorization")
    parser.add_argument("--client-id", default=os.environ.get("PARALLAX_X_CLIENT_ID"), required=False)
    parser.add_argument("--client-secret", default=os.environ.get("PARALLAX_X_CLIENT_SECRET"), required=False)
    parser.add_argument("--redirect-uri", required=True, help="Redirect URI configured for the X OAuth app")
    parser.add_argument("--code", help="Authorization code returned by X; otherwise prompt without echo")
    args = parser.parse_args()
    if not args.client_id or not args.client_secret:
        parser.error("--client-id and --client-secret (or their environment variables) are required")
    verifier, challenge = pkce_pair()
    state = os.urandom(16).hex()
    url = "https://x.com/i/oauth2/authorize?" + urlencode({"response_type": "code", "client_id": args.client_id, "redirect_uri": args.redirect_uri, "scope": X_SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    print("Open this X authorization URL in a browser:")
    print(url)
    print("After authorizing @ParallaxSignal, paste only the returned authorization code.")
    code = args.code or input("Authorization code: ").strip()
    if not code:
        print("No authorization code supplied.")
        return 2
    credentials, error = XOAuthClient().exchange_code(args.client_id, args.client_secret, code, verifier, args.redirect_uri)
    if credentials is None:
        print("X authorization failed: " + (error or "unknown error"))
        return 1
    XCredentialStore(SOCIAL_CONFIG_PATH).update({"PARALLAX_X_CLIENT_ID": args.client_id, "PARALLAX_X_CLIENT_SECRET": args.client_secret, "PARALLAX_X_ACCESS_TOKEN": credentials.access_token or "", "PARALLAX_X_REFRESH_TOKEN": credentials.refresh_token or "", "PARALLAX_X_TOKEN_EXPIRES_AT": str(int(credentials.expires_at or 0))})
    print("X authorization stored in the private PARALLAX social config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
