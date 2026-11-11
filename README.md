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
agent configuration to generate the entire stock message, including the earned
point after a new tap. The API returns a null message if the agent is offline
or the request is a retry. The balance and points remain available.
Low stock starts at seven days, urgent at three days or when less than one
dose remains. Primio must display these responses. No push delivery is configured.

The authenticated `POST /pilot/prices` endpoint takes the brand and shopping
location from Primio, for example
`{"brand":"Ariel","city":"Amiens, France","country":"fr"}`.
Country is a two-letter country code. The participant's saved message language
applies unless the request includes an explicit language. A 1.5 L liquid laundry
pack is the reference. Only offers matching the selected brand can rank, and
different pack sizes qualify when the title states a wash count. Ranking uses
listed item price per wash, excluding delivery. Mixed currencies are not compared.

Set `SERPAPI_API_KEY` for shopping. Primio asks the participant to enter a city
and country, and the API uses those values to localize shopping results.
Consumer offers can come from any seller and can link to Google Shopping.
Listings marked wholesale, business-only, or ex-tax, and known wholesalers,
are filtered before ranking. Unmarked offers may still need checkout checks
for consumer access, VAT, and availability.

Each pilot participant can save their Oracle message language using
`PUT /pilot/preferences/message-language` with `{"language":"fr"}` or
`{"language":"en"}`. The corresponding GET returns the saved setting,
defaulting to English. Stock, tap, and shopping messages use the saved
setting when the request omits an explicit language override.
