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
applies to every authenticated request. The search covers any
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
setting for every authenticated request.

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
on retries. A full account reset clears the current reward progress and current-cycle earned
cashback. Permanent milestone and reset history is preserved as described below.


### Survey version 3 in Primio

On each entry to the Surveys tab, GET `/pilot/surveys/questions` with the signed-in
user's bearer token. The response has `version: 3`, `question_count: 6`, `language`,
`questions`, `can_submit`, `survey_id`, and `survey_url`.
Render each item separately in `position` order using its `text` and `answer_type`.
Each question has a unique `id`. Keep separate answer state keyed by that ID;
never bind two Yes/No controls to the same boolean. Each Yes/No item supplies
localized `options` labels with JSON boolean values. Both `false` and `true`
are valid answers; an unselected answer must stay distinct from `false`.
The saved account language controls English/French text.

| Position | Answer ID | Question (English) | Type |
| --- | --- | --- | --- |
| 1 | `enjoyed_application` | Did you enjoy the application? | Yes/No |
| 2 | `price_comparison_beneficial` | Did you find the price comparison beneficial? | Yes/No |
| 3 | `would_buy_automatic_device` | Would you like to buy a device that reads directly the amount of the product without you pressing the I used it button? | Yes/No |
| 4 | `would_pay_for_device` | Would you be prepared to pay between 20€ to 30€ for this device? | Yes/No |
| 5 | `share_anonymous_usage_and_demographic_data` | Do you agree to share your usage and demographic data anonymously? | Yes/No |
| 6 | `comments` | Any other comments or feedbacks you want to tell us? | Text, max 2000 characters |

If `can_submit` is true, POST these six fields to the returned `survey_url`:

```json
{
  "enjoyed_application": true,
  "price_comparison_beneficial": false,
  "would_buy_automatic_device": true,
  "would_pay_for_device": false,
  "share_anonymous_usage_and_demographic_data": true,
  "comments": "Please add more products."
}
```

All six fields are required. The first five accept strict JSON booleans. Comments
must contain nonblank text of at most 2000 characters. Questions 3 and 4 are
independent: interest in a device does not imply acceptance of its price.
Question 5 explicitly covers usage AND demographic data; earlier usage-only
answers are never reused as consent for this revised question.

After a recorded refill or the seventh distinct usage following enrollment or account reset, the pack
response and GET `/pilot/pack` expose `survey_required`, `survey_id`, and
`survey_url`. Usage retries reuse the invitation. After submission, later usage
and refills do not activate another survey. Refresh the questions endpoint to
show completion. An identical submission retry returns the saved answers;
changing a completed survey returns 409. Another user's survey returns 404.

GET `/pilot/surveys?limit=100&offset=0` returns the signed-in user's history.
GET `/pilot/surveys/{survey_id}` returns an individual invitation or saved response.
Previously submitted version-1 and version-2 responses retain their original
question text, meanings, and answers. Pending invitations expose version 3.
Use `/pilot/surveys/questions` for the current question list, rather than deriving
it from historical answers. Updating the question definitions does not reopen
completed surveys. The password-protected POST `/pilot/auth/reset` starts a new
cycle; a new refill or seven subsequent usages enable a new survey. All previous answers
remain in history, and posting to a survey from an earlier cycle returns 409.

### Survey database inspection

New version-3 submissions store ALL SIX answers in `pilot_survey_answers`, linked
through `survey_id` to `pilot_refill_surveys` and its user. The original four
answer columns and the version-2 extra-answer table remain for historical data;
new questions are not mapped onto old answers. Application startup automatically
creates the new table, with no alteration or removal of existing columns.
Account activity resets preserve this table with survey history.
The questions themselves are defined by version in `pilot_surveys.py`.

To see the six current answer columns in psql:

```sql
\d pilot_survey_answers
```

To retrieve a participant's version-3 answers (change the participant code):

```sql
SELECT u.participant_code, l.email, s.id AS survey_id, s.version,
       s.submitted_at, a.enjoyed_application, a.price_comparison_beneficial,
       a.would_buy_automatic_device, a.would_pay_for_device,
       a.share_anonymous_usage_and_demographic_data, a.comments
FROM pilot_refill_surveys s
JOIN pilot_users u ON u.id = s.user_id
LEFT JOIN pilot_logins l ON l.user_id = u.id
JOIN pilot_survey_answers a ON a.survey_id = s.id
WHERE u.participant_code = 'SMOKE-B'
ORDER BY s.refilled_at;
```

For regression checks, install `httpx` alongside the application dependencies
and run `python -m unittest discover -s tests -v`. Tests use a temporary SQLite
database and do not connect to the deployed database.


### Account language in Primio

The supported account languages are `en` and `fr`. On language selection,
send authenticated PUT `/pilot/preferences/message-language` with
`{"language":"fr"}` or `{"language":"en"}`, then refresh the visible screens.
GET the same endpoint on app restart. The saved account preference controls
Oracle messages, alerts, price summaries, survey questions and pilot API
error messages. Legacy request-specific language values do not override it.

Enrollment and login optionally accept `language`; a successful request saves
an explicit choice, and both return the saved `language`. If login omits it,
the account's saved choice is retained. Send `Accept-Language: fr` or `en`
for unauthenticated errors and login validation. Authenticated errors use the
saved preference and include the `Content-Language` response header.

Pack and Oracle responses include `language`. Survey text uses the saved
choice even when an invitation was created in the other language. Saved Oracle
messages are regenerated in the chosen language when fetched. A completed price
snapshot in the other language is translated in the background on the next GET;
continue polling its status URL. This reuses the saved offers.

Primio must apply its own English/French locale to tabs, buttons, dialogs and
locally generated messages, and display the human-readable API text directly.
The saved preference is retained by account activity resets.


### Database-only cashback milestone and account reset history

The backend permanently records every 100-point threshold (100, 200, 300, and
so on within an account activity cycle), and every successfully processed
account reset. Each milestone stores the user, cycle number, threshold,
100 cashback cents, original timestamp, recording timestamp, source request UUID,
source event type, and whether it was reconstructed from retained activity.
Each reset stores the user, reset number, timestamp, optional request UUID,
and points before reset. A refill preserves the points cycle. An account reset
clears current points and starts another cycle; both history tables survive.

This history is for database inspection only. It is not returned in pack,
usage, refill, start, reset or Oracle responses. There is no account-history
API endpoint and no history fields in the public response schemas. Primio
requires no changes and must not send separate tracking events. Existing
current-cycle rewards and reward messages retain their previous behavior.

Tables are `pilot_cashback_milestones` and `pilot_account_resets`; application
startup creates them without altering existing columns. Their counts can be
calculated directly in SQL. Neither table references resettable activity rows.
Writes use the source activity's transaction and pack lock. Usage/refill retries
and polling do not duplicate milestones; failed requests leave no history rows.
The existing reset request also supports an optional UUID `request_id`. Reusing
it for retries of one reset prevents duplicate resets, while each distinct reset
uses a new UUID. Password-only requests remain supported; without an ID the
server cannot distinguish retries from deliberate additional resets.
Incorrect passwords and failed transactions do not count.

Reset counting starts when the tracking feature is deployed. Earlier resets
cannot be reconstructed reliably from the existing schema. A pack read, new
event, or reset recovers milestones from any usage/refill records still stored
in the current cycle, preserving the original event timestamps and setting
`reconstructed` to true. Activity deleted by an earlier reset cannot be recovered.
A reset saves recoverable milestones before erasing the source activity. This
does not restore old points or issue extra cashback notifications.

To inspect the totals per participant in psql:

```sql
SELECT u.participant_code, l.email,
       COALESCE(m.milestone_count, 0) AS cashback_milestone_count,
       COALESCE(m.cashback_cents, 0) AS lifetime_cashback_earned_cents,
       COALESCE(r.reset_count, 0) AS account_reset_count
FROM pilot_users u
LEFT JOIN pilot_logins l ON l.user_id = u.id
LEFT JOIN (
    SELECT user_id, COUNT(*) AS milestone_count, SUM(cashback_cents) AS cashback_cents
    FROM pilot_cashback_milestones GROUP BY user_id
) m ON m.user_id = u.id
LEFT JOIN (
    SELECT user_id, COUNT(*) AS reset_count
    FROM pilot_account_resets GROUP BY user_id
) r ON r.user_id = u.id
ORDER BY u.participant_code;
```

For event timestamps and points immediately before each reset:

```sql
SELECT u.participant_code, m.*
FROM pilot_cashback_milestones m
JOIN pilot_users u ON u.id = m.user_id
WHERE u.participant_code = 'SMOKE-B'
ORDER BY m.reached_at;

SELECT u.participant_code, r.*
FROM pilot_account_resets r
JOIN pilot_users u ON u.id = r.user_id
WHERE u.participant_code = 'SMOKE-B'
ORDER BY r.reset_number;
```

### Amazon lookup and Oracle language

Every new price search and explicit price refresh now starts two bounded lookups
in parallel: the existing any-size Shopping search and an Amazon-specific lookup.
Both use the existing SERPAPI_API_KEY. The Amazon lookup uses SerpApi's Amazon
engine for supported local marketplaces, including France, USA, UAE and KSA.
The app-supplied postcode is used where supported; UAE, KSA and Egypt use the
app-supplied city. Markets without a supported direct Amazon engine use a
localized Amazon-targeted Shopping query instead.

Amazon joins the major-retailer priority group. When a qualifying Amazon offer
exists in the comparison currency, the best such offer is included among the
five recommendations, alongside up to four other ranked offers. Offers still
require the same brand, an explicit wash count, a positive listed consumer price,
and compatible geographic evidence. B2B, Amazon Business, ex-VAT, multipack,
used and explicitly conflicting-location offers are excluded. The same Amazon
ASIN found in both sources is deduplicated. Known local Amazon marketplaces
use their official homepage (for example https://www.amazon.fr/) in Oracle
bullets. No offer is added if the lookup finds no eligible result. Either lookup
can fail without discarding successful results from the other. Each new search
may consume an additional SerpApi search credit.

For all consumer-facing Kimi responses, the saved user language is authoritative:
fr means French, en means English. Prompt instructions explicitly override the
country, retailer text, search snippets and style. Brand names, outlet names and
URLs remain unchanged. Existing stock/usage/refill messages and price summaries
created before this language policy are regenerated on their next fetch; their
old text is withheld while refreshing. Saved price offers are reused for this
language refresh rather than repeating the Amazon or Shopping lookup.
Primio continues to use the existing language preference endpoint and price
endpoints; no new request fields are needed.

Implementation references:
https://serpapi.com/amazon-search-api
https://serpapi.com/amazon-domains
https://serpapi.com/amazon-organic-results


### Refill or seven-use survey gate and database-only completion flags

A survey becomes submittable after any accepted refill OR seven distinct recorded
usage events in the current account-reset cycle, whichever happens first.
Before either event, GET `/pilot/surveys/questions` returns `can_submit: false`,
`survey_id: null` and `survey_url: null`; the six questions remain available for
preview. Pack responses keep `survey_required: false` until eligibility is met.
A refill response immediately includes the pending survey flag, ID and URL,
even with fewer than seven uses. Its survey details expose `trigger: "refill"`
and the actual refill request ID and timestamp. Seventh-use invitations expose
`trigger: "usage"`. Retries reuse the invitation; rejected refills do not unlock it.
Pending invitations from older releases use the same eligibility check, including
direct POST attempts, which return localized HTTP 409 when not yet eligible.

Only one survey can be submitted per account-reset cycle. Later refills do not
reopen a completed survey. An account reset allows a new survey after another
refill or seven new uses; all earlier answers remain in history. Identical
submission retries return the saved answers without adding rewards again.

Primio should use `can_submit` and `survey_required` from the backend. Any local
explanation should say "after a refill or seven uses". There are no new
survey-completion fields in API responses and no survey-status API endpoint.

The database view `pilot_user_survey_flags` gives administrators:

- `survey_completed`: true when a survey has been submitted in the current
  reset cycle. It becomes false after reset until the new survey is submitted.
- `survey_ever_completed`: true if any historical survey has been submitted;
  account activity resets do not clear this history.
- `last_survey_submitted_at`: latest submission timestamp across history.
- User ID, participant code and registered email for lookup.

These flags are derived by the database from existing submitted timestamps.
There are no duplicate mutable flags to refresh or backfill. Pending surveys do
not count as completed, and versions 1, 2 and 3 all count once submitted.
The view is created at API startup after the tables, using
`sql/pilot_user_survey_flags.sql`. It can also be applied directly with psql
without deploying API code. The refill-or-seven-use API gate requires the code release.
No survey answers, points, token values or account-reset history are changed.

```sql
SELECT participant_code, email, survey_completed, survey_ever_completed,
       last_survey_submitted_at
FROM pilot_user_survey_flags
ORDER BY participant_code;
```

To inspect only one participant, add `WHERE participant_code = 'SMOKE-E'`
before the ORDER BY clause.

### Database-only per-user euro rewards account

Each participant has one `pilot_reward_accounts` row, keyed by `user_id`.
`balance_cents` is the persistent earned euro balance in integer cents.
`pilot_reward_credits` records each addition, its source and its timestamps:

- Each new 100-point milestone adds 100 cents (€1). Refills continue point
  progress and may cross a milestone; they do not reset it.
- Each valid, completed survey adds 500 cents (€5). The refill-or-seven-use submission
  gate and one-submission-per-reset rule still apply.
- Survey/usage/refill retries do not duplicate credits. Unique source keys,
  user/account row locks, and a single source/credit/balance transaction protect
  against double awards and partial writes.

A user completing one survey and one 100-point milestone has a 600-cent (€6)
account balance. Activity resets clear current usage/points as before, while
preserving earned euros, credit history, milestone history and prior survey
answers. A newly eligible survey after reset is a new valid submission and can
earn its own €5 bonus. These records track rewards earned, not payments or
withdrawals; no payment or redemption integration is added.

The balance and credit history are database-only. No new account-balance API
fields, endpoint, Primio screen or Oracle payload fields are added. Existing
current-cycle cashback response fields retain their previous points-only meaning.

Startup automatically creates the two new tables and initializes existing users.
It recovers any retained 100-point milestones and credits all recorded submitted
surveys (including historical versions), once each. Previously recorded milestone
amounts retain their original value. Repeated startup/backfill never awards the
same source twice. Activity erased before permanent milestone tracking existed
cannot be recovered. New provisioning, enrollment and first locked pack access
ensure a zero-balance account also exists for new or older-tool participants.
No existing columns are altered. Deployment/restart is needed to apply startup
initialization to the server database.

To see per-user balances in psql:

```sql
SELECT u.participant_code, l.email, a.balance_cents,
       a.balance_cents / 100.0 AS balance_eur, a.updated_at
FROM pilot_reward_accounts a
JOIN pilot_users u ON u.id = a.user_id
LEFT JOIN pilot_logins l ON l.user_id = u.id
ORDER BY u.participant_code;
```

To inspect the additions for one account:

```sql
SELECT u.participant_code, c.reward_type, c.amount_cents / 100.0 AS credited_eur,
       c.earned_at, c.credited_at, c.milestone_id, c.survey_id
FROM pilot_reward_credits c
JOIN pilot_users u ON u.id = c.user_id
WHERE u.participant_code = 'SMOKE-E'
ORDER BY c.id;
```
