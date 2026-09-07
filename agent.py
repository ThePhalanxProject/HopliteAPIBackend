import json
import os
from typing import Any, Dict, Optional

import requests


HOSTMAN_AGENT_URL = "https://agent.hostman.com/api/v1/cloud-ai/agents/{access_id}/call"


class AgentError(RuntimeError):
    pass


def call_hoplite_agent(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Call the Hostman/Kimi agent if configured.

    The API remains usable without the agent configuration; in that case the endpoint
    returns the deterministic prediction and no AI notification is generated.
    """
    access_id = os.getenv("HOSTMAN_AGENT_ACCESS_ID")
    bearer_token = os.getenv("HOSTMAN_AGENT_BEARER_TOKEN")

    if not access_id or not bearer_token:
        return None

    url = HOSTMAN_AGENT_URL.format(access_id=access_id)
    prompt = (
        "You are the Hoplite Household Replenishment Agent. "
        "Use ONLY the supplied backend data. Never invent consumption, prices, availability, "
        "dates, or product information. The backend has already calculated the consumption rate, "
        "days remaining, estimated run-out date, and notification threshold. Do not recalculate them. "
        "Return valid JSON only with keys: action, priority, message, amazon_option. "
        "action must be one of NONE, LOW_STOCK_NOTIFICATION, URGENT_REPLENISHMENT_NOTIFICATION. "
        "If days_remaining is greater than 7, action must be NONE. If days_remaining is 7 or less, "
        "a low-stock or urgent notification may be generated according to notification_state. "
        "If an Amazon URL is supplied, amazon_option may contain it; otherwise it must be null. "
        "Always allow the consumer to buy the product themselves. Keep the message concise and natural.\n\n"
        f"BACKEND_DATA:\n{json.dumps(payload, default=str)}"
    )

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        },
        json={"message": prompt},
        timeout=20,
    )
    response.raise_for_status()

    data = response.json()
    # Hostman may return the agent answer under different envelope keys.
    candidate = data.get("response", data.get("message", data)) if isinstance(data, dict) else data
    if isinstance(candidate, dict):
        return candidate
    if isinstance(candidate, str):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise AgentError("Hostman agent returned non-JSON content") from exc
    raise AgentError("Unexpected Hostman agent response")
