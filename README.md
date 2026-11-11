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

## Button-based pilot

Set `DATABASE_URL` to the deployment's PostgreSQL connection URL in the
runtime environment before starting the API. Startup creates the pilot tables.
The pilot data lives in its own tables, keyed to each participant.

Run `python -m pilot P01` with the same database configuration to create a
participant and assign a pack. The command prints the participant's bearer token
once. Deliver it privately and use HTTPS for all requests.

| Endpoint | Behavior |
| --- | --- |
| `POST /pilot/pack/start` | Starts the participant's 1,500 g pack. Repeating the request retains the original start time. |
| `GET /pilot/pack` | Returns the current balance, full doses left and forecast. |
| `POST /pilot/pack/usage` | Records one use. Send `{"request_id":"<UUID>"}` and reuse that UUID if retrying the same tap. |

Send `Authorization: Bearer <token>` with each request. Each tap deducts
65 g, capped at the remaining balance. The API uses all accepted taps for the
pack and the actual elapsed time since activation, including time between
taps. The forecast is available after two uses and 48 hours of history. It
is recalculated when the client reads the pack. Once empty, the run-out
timestamp is the time of the last tap.

Pilot responses expose the forecast and dose count for Primio to display.
The pilot endpoints do not send notifications.

### Pilot Oracle and price search

Usage responses include `user_points`, `points_earned`, and `oracle`. One
point is credited for each accepted tap, including historical taps already
stored in the database. Retrying a request returns zero new points. Stock
messages are available on tap or through `GET /pilot/pack/oracle`, and both
accept `?language=en` or `?language=fr`. The pilot uses the existing Hostman
agent configuration and returns a stock message even if the agent is offline.
Low stock starts at seven days, urgent at three days or when less than one
dose remains. Primio must display these responses. No push delivery is configured.

The authenticated `POST /pilot/prices` endpoint accepts a specific product
(including size), city, two-letter lowercase country code, and optional
`language` (English or French). Set `SERPAPI_API_KEY` to enable it. Results are
Google Shopping listings localized to that city, ranked by listed item price.
Listings can include online offers or different product variants, and require
checking size, delivery fees and availability. The endpoint does not verify
inventory or shelf prices at physical stores.
