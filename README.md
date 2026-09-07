# HopliteFastAPI

## MVP consumption prediction and replenishment agent

The existing Arduino → `/measurements` → PostgreSQL flow remains unchanged.

The MVP now adds:

- `GET /consumption?device_id=...` — calculates consumption rate, days remaining and estimated run-out date from stored measurements.
- `GET /notifications?device_id=...` — returns generated replenishment notifications.
- A deterministic 7-day / 3-day notification rule.
- Optional Hostman Native API integration for the Hoplite AI agent. Configure the Hostman agent itself to use Kimi K3, then provide:
  - `HOSTMAN_AGENT_ACCESS_ID`
  - `HOSTMAN_AGENT_BEARER_TOKEN`

The backend performs the arithmetic and trigger decision. The AI agent only turns the structured result into a consumer-facing notification. If the Hostman variables are not configured, `/consumption` still works and no AI notification is generated.

### MVP assumptions

- One household / one device / one product.
- Existing `device_id` is preserved.
- The empty-product threshold is 50 g.
- A low-stock notification is eligible at 7 days or less.
- An urgent notification is eligible at 3 days or less.
- Duplicate notifications of the same type are suppressed.

The Docker deployment continues to start the existing FastAPI application with Uvicorn.
