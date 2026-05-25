"""
dm_sender.py — Instagram DM outreach module

Uses the same instagrapi client as enricher.py.
Sends one DM at a time with randomised human-like delays.
Optionally follows the recipient first (reduces spam flags).

⚠️  Keep daily sends under 50–80 to protect your account.
"""

import re
import time
import random


# ---------------------------------------------------------------------------
# Message personalisation
# ---------------------------------------------------------------------------

def personalise(template: str, username: str, full_name: str = "") -> str:
    """
    Replace {username} and {name} placeholders.
    {username} → @username (without the @)
    {name}     → first name from full_name, falls back to username
    """
    first_name = full_name.strip().split()[0] if full_name.strip() else username
    return (
        template
        .replace("{username}", username)
        .replace("{name}", first_name)
    )


# ---------------------------------------------------------------------------
# Single send
# ---------------------------------------------------------------------------

def follow_and_dm(
    cl,
    username: str,
    message: str,
    do_follow: bool = True,
) -> dict:
    """
    Optionally follow `username`, then send them a DM.

    Returns:
        {
          username,
          followed: bool,
          sent:     bool,
          error:    str | None,
          thread_id: str | None,
        }
    """
    result = {
        "username":  username,
        "followed":  False,
        "sent":      False,
        "error":     None,
        "thread_id": None,
    }

    try:
        # Resolve username → user_id (cached by instagrapi after first lookup)
        user_id = cl.user_id_from_username(username)
    except Exception as e:
        result["error"] = f"User not found: {e}"
        return result

    # ── Optional follow ───────────────────────────────────────────────────────
    if do_follow:
        try:
            cl.user_follow(user_id)
            result["followed"] = True
            # Short pause after follow before DMing
            time.sleep(random.uniform(4, 9))
        except Exception as e:
            # Non-fatal — still attempt the DM
            result["error"] = f"Follow failed ({e}), still sending DM"

    # ── Send DM ───────────────────────────────────────────────────────────────
    try:
        thread = cl.direct_send(text=message, user_ids=[int(user_id)])
        result["sent"]      = True
        result["thread_id"] = str(thread.id) if thread else None
    except Exception as e:
        err = str(e)
        result["error"] = err
        # Surface common Instagram blocks clearly
        if "feedback_required" in err.lower() or "challenge" in err.lower():
            result["error"] = "Instagram flagged this action — slow down or verify your account."
        elif "please wait" in err.lower() or "ratelimit" in err.lower():
            result["error"] = "Rate limited by Instagram. Pausing recommended."

    return result
