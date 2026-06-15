from __future__ import annotations

import os


async def resend_send_api_key(to: str, api_key: str) -> None:
    """Send an API key to a new user via Resend. Requires RESEND_API_KEY env var."""
    import httpx

    resend_key = os.getenv("RESEND_API_KEY", "")
    from_addr = os.getenv("Phronosis_FROM_EMAIL", "Phronosis <noreply@phronosis.dev>")

    body = f"""\
Welcome to Phronosis!

Your API key:

    {api_key}

Add it to your Claude Code MCP config:

    "headers": {{"X-API-Key": "{api_key}"}}

This key was shown once in this email — it is not stored in plaintext anywhere.
If you lose it, sign up again at phronosis.dev to issue a new one.

— The Phronosis team
"""

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            json={
                "from": from_addr,
                "to": [to],
                "subject": "Your Phronosis API key",
                "text": body,
            },
        )
        resp.raise_for_status()


def get_email_sender():
    """Return the configured email sender, or None if RESEND_API_KEY is unset."""
    if os.getenv("RESEND_API_KEY"):
        return resend_send_api_key
    return None
