import json
import os
from typing import Any, Dict, Optional

import requests


HOSTMAN_AGENT_URL = "https://agent.hostman.com/api/v1/cloud-ai/agents/{access_id}/call"


class AgentError(RuntimeError):
    pass


def call_hoplite_agent(payload: Dict[str, Any], test_mode: bool = False) -> Optional[Dict[str, Any]]:
    """Call the Hostman/Kimi agent if configured."""
    access_id = os.getenv("HOSTMAN_AGENT_ACCESS_ID")
    bearer_token = os.getenv("HOSTMAN_AGENT_BEARER_TOKEN")
    if not access_id or not bearer_token:
        return None

    url = HOSTMAN_AGENT_URL.format(access_id=access_id)

    if test_mode:
        test_instruction = (
            "This is a connectivity test. Respond as the Hoplite Oracle. "
            "Your message MUST begin exactly with: 'Oracle is at your service.' "
            "After that, add one short sentence confirming that you received the test request."
        )
    else:
        test_instruction = (
            "For normal household notifications, do not use the test greeting. "
            "Keep the message concise, natural, and useful to the consumer."
        )

    prompt = (
        "You are the Hoplite Household Replenishment Agent, also known as the Hoplite Oracle. "
        "Use ONLY the supplied backend data. Never invent consumption, prices, availability, "
        "dates, or product information. The backend has already calculated the consumption rate, "
        "days remaining, estimated run-out date, and notification threshold. Do not recalculate them. "
        "Return valid JSON only with keys: action, priority, message, amazon_option. "
        "action must be one of NONE, LOW_STOCK_NOTIFICATION, URGENT_REPLENISHMENT_NOTIFICATION. "
        "If days_remaining is greater than 7, action must be NONE. If days_remaining is 7 or less, "
        "a low-stock or urgent notification may be generated according to notification_state. "
        "If an Amazon URL is supplied, amazon_option may contain it; otherwise it must be null. "
        "Always allow the consumer to buy the product themselves. "
        f"{test_instruction}\n\n"
        f"BACKEND_DATA:\n{json.dumps(payload, default=str)}"
    )

    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
            },
            json={"message": prompt},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AgentError(f"Hostman agent request failed: {exc}") from exc

    # Hostman returns the agent answer in the `message` field. Keep support for
    # a `response` envelope as a small compatibility fallback.
    candidate = data.get("message", data.get("response", data)) if isinstance(data, dict) else data
    if isinstance(candidate, dict):
        return candidate
    if isinstance(candidate, str):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise AgentError("Hostman agent returned non-JSON content") from exc
    raise AgentError("Unexpected Hostman agent response")
