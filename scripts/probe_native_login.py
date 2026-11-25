"""Probe GM's native login transport without an account or saved session.

Run explicitly from the repository root. Sends one empty credential form;
never reads a HAR, accepts credentials, or prints transaction/cookie values.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp  # noqa: E402

from custom_components.mychevrolet.onyonstar.auth import GMAuth  # noqa: E402
from custom_components.mychevrolet.onyonstar.auth.native import (  # noqa: E402
    NativeLogin,
    NativeUnavailable,
)
from custom_components.mychevrolet.onyonstar.http import AuthError, CloudError  # noqa: E402


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        login = NativeLogin(GMAuth(session, "anonymous-probe"))
        try:
            await login.start("", "")
            result = "unexpected_success"
        except NativeUnavailable:
            result = "browser_required_or_unsupported_page"
        except AuthError:
            result = "empty_credentials_rejected"
        except CloudError as error:
            result = f"transport_error_{error.status}"
        finally:
            login.clear()
        print(json.dumps({"result": result, "account_used": False}))


if __name__ == "__main__":
    asyncio.run(main())
