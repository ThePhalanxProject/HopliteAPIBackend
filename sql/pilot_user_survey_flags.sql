-- Database-only flags derived from submitted survey records.
-- Safe to reapply. No survey answers, user accounts or API fields are changed.
CREATE OR REPLACE VIEW pilot_user_survey_flags AS
SELECT
    u.id AS user_id,
    u.participant_code,
    l.email,
    EXISTS (
        SELECT 1
        FROM pilot_refill_surveys s
        WHERE s.user_id = u.id
          AND s.submitted_at IS NOT NULL
          AND (c.started_at IS NULL OR s.refilled_at >= c.started_at)
    ) AS survey_completed,
    EXISTS (
        SELECT 1
        FROM pilot_refill_surveys s
        WHERE s.user_id = u.id
          AND s.submitted_at IS NOT NULL
    ) AS survey_ever_completed,
    (
        SELECT MAX(s.submitted_at)
        FROM pilot_refill_surveys s
        WHERE s.user_id = u.id
          AND s.submitted_at IS NOT NULL
    ) AS last_survey_submitted_at
FROM pilot_users u
LEFT JOIN pilot_logins l ON l.user_id = u.id
LEFT JOIN pilot_survey_cycles c ON c.user_id = u.id;
