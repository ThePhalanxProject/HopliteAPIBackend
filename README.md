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
| `POST /pilot/pack/start` | Starts the participant's 1.5 L pack. Repeating the request retains the original start time. |
| `GET /pilot/pack` | Returns the current balance, full doses left and forecast. |
| `POST /pilot/pack/usage` | Records one use. Send `{"request_id":"<UUID>"}` and reuse that UUID if retrying the same tap. |

Send `Authorization: Bearer <token>` with each request. Each tap deducts
65 mL, capped at the remaining balance. Pack responses use `current_volume_l`, `capacity_l`, `dose_ml`, and
`consumption_rate_l_day`. The API uses all accepted taps for the
pack and the actual elapsed time since activation, including time between
taps. The forecast is available after two uses and 48 hours of history. It
is recalculated when the client reads the pack. Once empty, the run-out
timestamp is the time of the last tap.

Pilot responses expose the forecast and dose count for Primio to display.
The pilot endpoints do not send notifications.

### Pilot Oracle and price search

An accepted usage tap earns 5 points. `POST /pilot/pack/refill` takes
`{"request_id":"<UUID>"}` and restores an activated, partially used or empty
pack to 1.5 L. A refill earns 10 points and starts a new forecast cycle.
Prior simulated events remain in the account history, with their stored counts
interpreted as millilitres. Existing taps count at 5 points
under the new rules. Retry the same event with its original request ID.
Refill attempts on a full or unstarted pack return 409. A newly accepted refill
returns `refill_recorded: true`. A usage or replay response returns false.
`last_refill_at` is available on pack reads and event responses, and remains
null until the first refill. `full_doses_remaining` is the server-calculated
integer wash count for the current stock.

Every 50 cumulative points earns €1 of cashback. The API tracks earned
cashback without issuing payments or redemptions. Pack responses include
`user_points`, `cashback_earned_cents`, and `points_to_next_cashback`.
Usage and refill responses also include `points_earned`,
`cashback_unlocked_cents`, and `oracle`. A retry earns zero additional
points and cashback. Oracle generates a message with the outcome and reward
progress in the participant's saved language. If Kimi is unavailable, the
structured stock and reward fields are still returned with a null message.

The authenticated `POST /pilot/prices` endpoint takes the brand and shopping
location from Primio, for example
`{"brand":"Ariel","city":"Amiens, France","country":"fr"}`.
Country is a two-letter country code. The participant's saved message language
applies unless the request includes an explicit language. The search covers any
pack size of the selected liquid laundry detergent brand. Eligible offers must
state a wash count in the title. Ranking uses
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

### Pilot app sign-in

Use an existing pilot token once as an invitation in Primio's sign-up screen.
Enrollment adds email/password sign-in to the same pilot participant and pack.
Subsequent logins create a seven-day bearer session accepted by the existing
pilot endpoints. Logout revokes that session when the backend is reachable.
Existing pilot tokens continue to work for API testing. Local-only Primio user
accounts must enroll to get a backend session.

### Refill recommendations in Primio

Pack status and usage/refill responses include `stock_status` and
`refill_needed`. The flag is true for a forecast of seven days or less,
fewer than three full washes, or empty stock. Once triggered, the flag remains
true for the current pack cycle, even when the forecast changes. A successful
refill starts a new cycle and clears the flag. Show `refill_alert_message`
beside Oracle while the flag is true. It is also present in Oracle responses
and stays available when the agent is offline.

When the flag is true, Primio can automatically send the user's selected
brand and shopping location to `POST /pilot/prices` with the same bearer
token. Display the returned `message` next to Oracle and the linked
`offers` beneath it. To avoid repeating searches, remember successful
searches by participant ID and the pack's current `started_at` timestamp.
An empty offer list is a successful search with no eligible offers.

The **Refresh prices** button calls the same authenticated `POST /pilot/prices`
endpoint with `"refresh": true` alongside brand, city, and country. This bypasses
SerpApi's cache for the search. Automatic searches can omit the field.

### Fast usage taps

For immediate stock and points, call `POST /pilot/pack/usage?defer_oracle=true`
with the usual request UUID. The response includes `oracle_url` and commits
the usage before any Kimi call. Update the UI from that response, then make a
separate authenticated POST to `oracle_url` with no body and display its
`message` when ready. The message uses the original tap's context, and a
successfully generated message is reused if requested again. Keep the refill
alert driven by the current pack response. Requests that omit `defer_oracle`
continue to receive the Oracle message inline.
