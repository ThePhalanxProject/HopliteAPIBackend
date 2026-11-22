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

Every 100 cumulative points earns €1 of cashback. The API tracks earned
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

### Progressive price results in Primio

Add `?progressive=true` to the authenticated `POST /pilot/prices` request.
The response returns `search_id` and `status_url` immediately. Send the same
bearer token with a GET to `status_url` at approximately 5 and 15 seconds.
After the second check, poll every 2 seconds until `stage` is `final`. A check
can return `final` early if the search and summary have already finished.

At each check, display any returned `offers`. While `status` is `searching`,
show that prices are being checked. While it is `summarizing`, keep the offers
visible and show that Oracle is preparing the summary. On `complete`, display
`message` beside Oracle when available. Keep the offers if the message is
unavailable. On `failed`, show a retry action. Each GET reads the same search
and makes no external requests. Snapshots are available for one hour.

### Fast usage taps

For immediate stock and points, call `POST /pilot/pack/usage?defer_oracle=true`
with the usual request UUID. The response includes `oracle_url` and commits
the usage before any Kimi call. Update the UI from that response, then make a
separate authenticated POST to `oracle_url` with no body and display its
`message` when ready. The message uses the original tap's context, and a
successfully generated message is reused if requested again. Keep the refill
alert driven by the current pack response. Requests that omit `defer_oracle`
continue to receive the Oracle message inline.

### Fast refill taps

Call `POST /pilot/pack/refill?defer_oracle=true` with the usual refill request
UUID. Update the UI immediately from the returned stock, refill flag and points.
Then make an authenticated POST to the returned `oracle_url` with no body and
show its `message` beside Oracle when ready. If the message request fails,
retry that URL. If the refill request itself must be retried, reuse its UUID.


### Learning offer preferences in Primio

After opening an offer link, send authenticated `POST /pilot/prices/preferences`:

```json
{
  "request_id": "<new UUID for this interaction>",
  "search_id": "<search_id returned by the price request>",
  "offer_url": "<exact url from the selected offer>",
  "action": "click"
}
```

Use `"select"` when the user explicitly chooses an offer. Reuse request_id on
retries. A successful request returns 204. Link navigation should proceed even
if preference recording fails. Only offers from the signed-in user's saved
search are accepted. For Markdown links, match the tapped URL to offers[].url.

Preferences are scoped to the participant, brand and country. A click adds one
signal, a selection two; clicking and selecting the same offer in one search
counts as two total. Each signal gives a 5% ranking advantage, capped at 15%.
Major retailers remain first. Listed prices remain the actual prices. Saved
preferences persist when old search snapshots expire and are cleared by the
password-protected account reset. Both synchronous and progressive searches
apply the scores and include preference_score in each returned offer.


### Cashback progress across refills

Use `cashback_progress_points` and `cashback_points_per_euro` for the reward
progress indicator in all pack, usage and refill responses. These count reward
progress across pack refills. A refill adds 10 points to the account.
When an event returns positive `cashback_unlocked_cents`, show the earned
reward before displaying progress toward the next euro. Reaching each 100
points earns €1 and surplus points carry forward. Reuse the event request UUID
on retries. A full account reset clears both reward progress and earned cashback.


### Usage and refill surveys in Primio

After a successful refill, check `survey_required`, `survey_id`, and
`survey_url` in the pack response. The same fields on `GET /pilot/pack`
recover pending surveys after restart. Each successful refill creates one
survey invitation. Request retries reuse it. Older pending surveys are shown
first. The first recorded usage also activates a survey if the account has no survey history.\nLater usage taps and retries create no extra survey.

GET `survey_url` with the participant's bearer token to obtain the four
localized questions. Submit answers with POST to that same URL:

```json
{
  "enjoyed_application": true,
  "would_use_connected_product": true,
  "found_useful": false,
  "experience_feedback": "The experience would be better if..."
}
```

All four answers are required. The first three are JSON booleans displayed as
Yes/No. The fourth is nonblank text of at most 2000 characters. On success,
refresh pack status for the next pending survey. An identical submission retry
returns the saved answers; changing a completed survey returns 409.
GET `/pilot/surveys?limit=100&offset=0` returns the signed-in user's survey
history. Survey records, timestamps and answers survive the account reset.

Administrators can query the database by participant or registered email:

```sql
SELECT u.participant_code, l.email, s.*
FROM pilot_refill_surveys s
JOIN pilot_users u ON u.id = s.user_id
LEFT JOIN pilot_logins l ON l.user_id = u.id
WHERE u.participant_code = 'SMOKE-B'
ORDER BY s.refilled_at;
```


### Opening the Surveys tab

On each tab entry, call authenticated GET `/pilot/surveys/questions`.
The response includes `questions`, `language`, `version`, `can_submit`,
`survey_id`, and `survey_url`. Render the returned questions directly.
If `can_submit` is true, POST the four answers to `survey_url` using the
existing survey submission format. If false, the questions are available for
preview; the first recorded use activates the initial survey, and a recorded\nrefill creates the next invitation. Refresh this endpoint
after submission to show the next pending survey or a completion state.


The Surveys tab becomes submittable after one recorded use. Existing participants
with recorded usage and no survey history are activated on their next pack-status
or survey-questions request. Completed surveys survive reset, so reset and later
usage do not create another first-use survey. Each recorded refill still creates
its survey. Survey details and history now include `trigger` (`usage` or `refill`),
`trigger_request_id`, and `triggered_at`. The older `refill_request_id` and
`refilled_at` fields are null for usage-triggered surveys.
