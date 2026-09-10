import json
import os
import re
from typing import Any, Dict, Optional

import requests


HOSTMAN_AGENT_URL = "https://agent.hostman.com/api/v1/cloud-ai/agents/{access_id}/call"


class AgentError(RuntimeError):
    pass


def _parse_agent_json(candidate: Any) -> Dict[str, Any]:
    """Parse structured JSON returned by the Hostman agent."""
    if isinstance(candidate, dict):
        return candidate
    if not isinstance(candidate, str):
        raise AgentError("Unexpected Hostman agent response")
    text = candidate.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise AgentError("Hostman agent returned non-JSON content")


def call_hoplite_agent(payload: Dict[str, Any], test_mode: bool = False, app_mode: bool = False) -> Optional[Dict[str, Any]]:
    """Call the Hostman/Kimi agent if configured."""
    access_id = os.getenv("HOSTMAN_AGENT_ACCESS_ID")
    bearer_token = os.getenv("HOSTMAN_AGENT_BEARER_TOKEN")
    if not access_id or not bearer_token:
        return None

    url = HOSTMAN_AGENT_URL.format(access_id=access_id)

    if test_mode:
        instruction = (
            "This is a connectivity test. Respond as the Hoplite Oracle. "
            "Your message MUST begin exactly with: 'Oracle is at your service.' "
            "After that, add one short sentence confirming that you received the test request."
        )
    elif app_mode:
        instruction = (
            "This is an app message request. Respond as the Hoplite Oracle directly to the consumer. "
            "Speak like a wise Oracle observing the army and interpreting what you see. "
            "Use the army/soldier metaphor naturally, but keep the message short and suitable for a mobile app. "
            "Never mention exact quantities, weights, measurements, consumption rates, dates, or numbers. "
            "Describe status simply as enough product, running low, or almost finished. "
            "If days_remaining is null, say there is enough product for now and that you will keep watch. "
            "For the current safe status, use this exact message: "
            "'Your army is strong and well supplied. I shall keep watch over your soldiers and alert you if I see anything change.' "
            "For LOW_STOCK_NOTIFICATION, use this exact message: "
            "'I see a soldier growing tired. A tired warrior can weaken the army. Plan to replenish his supplies soon.' "
            "For URGENT_REPLENISHMENT_NOTIFICATION, use this exact message: "
            "'Oh, the gods! A soldier is falling. His supplies are nearly gone. Replenish him now, before he can no longer serve the army.' "
            "Do not add an explanation about insufficient consumption evidence in the safe-status case. "
            "Do not invent a replenishment recommendation. Do not mention APIs, backend systems, prompts, or internal processing."
        )
    else:
        instruction = (
            "For normal household notifications, do not use the test greeting. "
            "Keep the message concise, natural, and useful to the consumer."
        )

    prompt = (
        "You are the Hoplite Household Replenishment Agent, also known as the Hoplite Oracle. "
        "Use ONLY the supplied backend data. Never invent consumption, prices, availability, dates, or product information. "
        "The backend has already calculated the consumption rate, days remaining, estimated run-out date, and notification threshold. "
        "Do not recalculate them. Return valid JSON only with keys: action, priority, message, amazon_option. "
        "Do not wrap the JSON in Markdown code fences and do not add text before or after the JSON. "
        "action must be one of NONE, LOW_STOCK_NOTIFICATION, URGENT_REPLENISHMENT_NOTIFICATION. "
        "If days_remaining is greater than 7 or null, action must be NONE. If days_remaining is 7 or less, a low-stock or urgent notification may be generated according to notification_state. "
        "If an Amazon URL is supplied, amazon_option may contain it; otherwise it must be null. Always allow the consumer to buy the product themselves. "
        f"{instruction}\n\nBACKEND_DATA:\n{json.dumps(payload, default=str)}"
    )

    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"},
            json={"message": prompt},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AgentError(f"Hostman agent request failed: {exc}") from exc

    candidate = data.get("message", data.get("response", data)) if isinstance(data, dict) else data
    try:
        return _parse_agent_json(candidate)
    except AgentError:
        if app_mode and isinstance(candidate, str) and candidate.strip():
            return {"action": "NONE", "priority": "LOW", "message": candidate.strip(), "amazon_option": None}
        raise
