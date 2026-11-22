"""Database-only reward/reset tracking, exercised through normal account actions."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

# Reuse the survey suite's isolated SQLite application, never the deployed DB.
import test_pilot_surveys as survey_tests
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from database import Base, SessionLocal, engine
from main import app
from models import PilotAccountResetEvent, PilotCashbackMilestone, PilotPack, PilotRefillEvent, PilotUsageEvent, PilotUsageOracle


class AccountHistoryTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.client = TestClient(app)
        self.user_ids = {}

    def account(self, language='en'):
        user_id, headers = survey_tests.SurveyTests.account(self, language)
        self.user_ids[headers['Authorization']] = user_id
        return user_id, headers

    def start(self, headers):
        r = self.client.post('/pilot/pack/start', headers=headers)
        self.assertEqual(r.status_code, 200, r.text)

    def event(self, headers, kind='usage', request_id=None):
        request_id = request_id or str(uuid4())
        r = self.client.post('/pilot/pack/'+kind+'?defer_oracle=true', headers=headers,
                             json={'request_id':request_id})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json(), request_id

    def history(self, headers):
        # Inspect the database directly; account history is never exposed by API.
        user_id = self.user_ids[headers['Authorization']]
        with SessionLocal() as db:
            milestones = list(db.scalars(select(PilotCashbackMilestone).where(
                PilotCashbackMilestone.user_id == user_id,
            ).order_by(PilotCashbackMilestone.reached_at.desc(), PilotCashbackMilestone.id.desc())))
            resets = list(db.scalars(select(PilotAccountResetEvent).where(
                PilotAccountResetEvent.user_id == user_id,
            ).order_by(PilotAccountResetEvent.reset_number.desc())))
            def values(row):
                return {column.name: getattr(row, column.name) for column in row.__table__.columns}
            return {
                'account_reset_count':len(resets),
                'lifetime_cashback_milestone_count':len(milestones),
                'lifetime_cashback_earned_cents':sum(row.cashback_cents for row in milestones),
                'milestones':[values(row) for row in milestones],
                'resets':[values(row) for row in resets],
            }

    def test_usage_and_refill_milestones_and_retries(self):
        _, h = self.account()
        self.start(h)
        for _ in range(19):
            result, _ = self.event(h)
        self.assertEqual(result['user_points'], 95)
        self.assertEqual(self.history(h)['lifetime_cashback_milestone_count'], 0)
        result, key = self.event(h, 'refill')
        self.assertEqual(result['user_points'], 105)
        self.assertEqual(result['cashback_progress_points'], 5)
        self.assertEqual(result['cashback_unlocked_cents'], 500)
        self.assertEqual(self.history(h)['lifetime_cashback_milestone_count'], 1)
        self.event(h, 'refill', key)
        self.assertEqual(self.history(h)['lifetime_cashback_milestone_count'], 1)
        for _ in range(19):
            result, _ = self.event(h)
        self.assertEqual(result['user_points'], 200)
        self.assertEqual(self.history(h)['lifetime_cashback_milestone_count'], 2)
        hist = self.history(h)
        self.assertEqual(hist['lifetime_cashback_earned_cents'], 1000)
        self.assertEqual([m['points_threshold'] for m in hist['milestones']], [200,100])
        self.assertEqual([m['source_event_type'] for m in hist['milestones']], ['usage','refill'])
        self.assertTrue(all(not m['reconstructed'] for m in hist['milestones']))
        self.assertEqual(hist['milestones'][1]['source_request_id'], key)
        # Polling and replaying a recorded event never increment the count.
        self.assertEqual(self.history(h)['lifetime_cashback_milestone_count'], 2)
        self.assertNotIn('lifetime_cashback_milestone_count', self.client.get('/pilot/pack', headers=h).json())

    def test_resets_preserve_history_and_start_another_points_cycle(self):
        user_id, h = self.account()
        self.start(h)
        for _ in range(20):
            self.event(h)
        body = {'password':'survey-test-password','request_id':str(uuid4())}
        r = self.client.post('/pilot/auth/reset', headers=h, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        reset = r.json()
        self.assertEqual(reset['user_points'], 0)
        self.assertEqual(reset['cashback_earned_cents'], 0)
        self.assertEqual(self.history(h)['account_reset_count'], 1)
        self.assertEqual(self.history(h)['lifetime_cashback_milestone_count'], 1)
        self.assertEqual(self.history(h)['lifetime_cashback_earned_cents'], 500)
        self.client.post('/pilot/auth/reset', headers=h, json=body)
        self.assertEqual(self.history(h)['account_reset_count'], 1)
        self.start(h)
        self.event(h)
        # A delayed duplicate reset must not erase newly recorded activity.
        duplicate = self.client.post('/pilot/auth/reset', headers=h, json=body)
        self.assertEqual(duplicate.json()['user_points'], 5)
        for _ in range(19):
            result, _ = self.event(h)
        self.assertEqual(self.history(h)['lifetime_cashback_milestone_count'], 2)
        self.assertEqual(result['cashback_earned_cents'], 500)
        hist = self.history(h)
        self.assertEqual([m['cycle_number'] for m in hist['milestones']], [1,0])
        self.assertEqual(hist['resets'][0]['points_before_reset'], 100)
        # Password-only clients remain compatible; each accepted reset counts.
        r = self.client.post('/pilot/auth/reset', headers=h, json={'password':'survey-test-password'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.history(h)['account_reset_count'], 2)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(PilotCashbackMilestone).where(PilotCashbackMilestone.user_id==user_id)), 2)
            self.assertEqual(db.scalar(select(func.count()).select_from(PilotAccountResetEvent).where(PilotAccountResetEvent.user_id==user_id)), 2)

    def test_backfill_before_reset_preserves_original_timestamps(self):
        user_id, h = self.account()
        start = datetime.now(timezone.utc)-timedelta(days=2)
        with SessionLocal.begin() as db:
            pack = db.scalar(select(PilotPack).where(PilotPack.user_id==user_id))
            pack.started_at = start
            for i in range(20):
                event = PilotUsageEvent(pack_id=pack.id, request_id=str(uuid4()), volume_used_ml=65,
                                        occurred_at=start+timedelta(minutes=i))
                db.add(event)
            db.flush()
            reached_at = event.occurred_at
            source = event.request_id
        # Reset must recover the threshold before deleting its source rows.
        r = self.client.post('/pilot/auth/reset', headers=h, json={'password':'survey-test-password'})
        self.assertEqual(r.status_code, 200, r.text)
        hist = self.history(h)
        self.assertEqual(hist['lifetime_cashback_milestone_count'], 1)
        self.assertTrue(hist['milestones'][0]['reconstructed'])
        self.assertEqual(hist['milestones'][0]['source_request_id'], source)
        self.assertEqual(hist['milestones'][0]['reached_at'].replace(tzinfo=timezone.utc), reached_at)
        self.assertEqual(hist['resets'][0]['points_before_reset'], 100)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(PilotUsageEvent)), 0)

    def test_failed_reset_and_other_users_do_not_change_history(self):
        _, h = self.account('fr')
        self.start(h)
        self.event(h)
        r = self.client.post('/pilot/auth/reset', headers=h, json={'password':'wrong-password'})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.history(h)['account_reset_count'], 0)
        self.assertEqual(self.client.get('/pilot/pack', headers=h).json()['user_points'], 5)
        self.client.post('/pilot/auth/reset', headers=h, json={'password':'survey-test-password'})
        _, other = self.account()
        self.assertEqual(self.history(other)['account_reset_count'], 0)
        self.assertEqual(self.history(other)['resets'], [])
        self.assertEqual(self.history(other)['milestones'], [])
        self.assertEqual(self.client.get('/pilot/account/history').status_code, 404)

    def test_five_euro_reward_still_requires_one_hundred_points(self):
        _, h = self.account()
        self.start(h)
        for i in range(1,20):
            result, _ = self.event(h)
            self.assertEqual(result['cashback_earned_cents'], 0)
            self.assertEqual(result['cashback_unlocked_cents'], 0)
            self.assertEqual(result['cashback_progress_points'], i*5)
            self.assertEqual(result['points_to_next_cashback'], 100-i*5)
            self.assertEqual(result['cashback_points_per_euro'], 20)
            self.assertEqual(result['cashback_points_per_reward'], 100)
            self.assertEqual(result['cashback_reward_cents'], 500)
        result, key = self.event(h)
        self.assertEqual(result['cashback_unlocked_cents'], 500)
        self.assertEqual(result['cashback_earned_cents'], 500)
        self.assertEqual(result['cashback_progress_points'], 0)
        self.assertEqual(result['points_to_next_cashback'], 100)
        retry, _ = self.event(h, request_id=key)
        self.assertEqual(retry['cashback_unlocked_cents'], 0)
        self.assertEqual(len(self.history(h)['milestones']), 1)

    def test_existing_one_euro_records_and_cached_messages_keep_their_value(self):
        _, h = self.account()
        self.start(h)
        for _ in range(20):
            _, key = self.event(h)
        # Simulate the previously deployed €1 rule and its cached event message.
        with SessionLocal.begin() as db:
            milestone = db.scalar(select(PilotCashbackMilestone))
            milestone.cashback_cents = 100
            usage = db.scalar(select(PilotUsageEvent).where(PilotUsageEvent.request_id==key))
            saved = db.get(PilotUsageOracle, usage.id)
            old = dict(saved.payload, cashback_points_per_euro=100,
                       cashback_earned_cents=100, cashback_unlocked_cents=100)
            old.pop('cashback_points_per_reward')
            old.pop('cashback_reward_cents')
            saved.payload = old
            saved.message = 'Old reward message'
        self.assertEqual(self.client.get('/pilot/pack', headers=h).json()['cashback_earned_cents'], 100)
        with patch('pilot.oracle_copy', return_value=('Updated reward message','kimi')) as kimi:
            r = self.client.post('/pilot/pack/usage/'+key+'/oracle', headers=h)
            self.assertEqual(r.status_code, 200, r.text)
            payload = kimi.call_args.args[0]
            self.assertEqual(payload['cashback_reward_cents'], 500)
            self.assertEqual(payload['cashback_unlocked_cents'], 100)
            self.assertEqual(payload['cashback_earned_cents'], 100)
        self.event(h, 'refill')
        for _ in range(18):
            result, _ = self.event(h)
        self.assertEqual(result['user_points'], 200)
        self.assertEqual(result['cashback_unlocked_cents'], 500)
        self.assertEqual(result['cashback_earned_cents'], 600)
        self.assertEqual([m['cashback_cents'] for m in self.history(h)['milestones']], [500,100])

    def test_kimi_receives_five_euro_policy_in_both_languages(self):
        _, h = self.account('fr')
        self.start(h)
        for _ in range(19):
            self.event(h)
        _, key = self.event(h, 'refill')
        for language in ('fr','en'):
            self.client.put('/pilot/preferences/message-language', headers=h, json={'language':language})
            with patch('pilot.oracle_copy', return_value=('Reward message','kimi')) as kimi:
                r = self.client.post('/pilot/pack/refill/'+key+'/oracle', headers=h)
                self.assertEqual(r.status_code, 200, r.text)
                payload = kimi.call_args.args[0]
                self.assertEqual(payload['language'], language)
                self.assertEqual(payload['cashback_points_per_reward'], 100)
                self.assertEqual(payload['cashback_reward_cents'], 500)
                self.assertEqual(payload['cashback_unlocked_cents'], 500)
                self.assertEqual(payload['cashback_progress_points'], 5)
                self.assertEqual(payload['points_to_next_cashback'], 95)

    def test_history_absent_from_all_app_responses_and_openapi(self):
        _, h = self.account()
        hidden = {'account_reset_count', 'lifetime_cashback_milestone_count', 'lifetime_cashback_earned_cents'}
        def assert_private(response):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(hidden.isdisjoint(response.json()))
        assert_private(self.client.get('/pilot/pack', headers=h))
        assert_private(self.client.post('/pilot/pack/start', headers=h))
        for kind in ['usage','refill']:
            assert_private(self.client.post('/pilot/pack/'+kind+'?defer_oracle=true', headers=h,
                           json={'request_id':str(uuid4())}))
        assert_private(self.client.post('/pilot/auth/reset', headers=h, json={'password':'survey-test-password'}))
        self.assertEqual(self.history(h)['account_reset_count'], 1)
        self.assertEqual(self.client.get('/pilot/account/history', headers=h).status_code, 404)
        schema = self.client.get('/openapi.json').json()
        self.assertNotIn('/pilot/account/history', schema['paths'])
        for model in schema['components']['schemas'].values():
            self.assertTrue(hidden.isdisjoint(model.get('properties', {})))

    def test_failed_reset_rolls_back_deletions_and_reset_record(self):
        _, h = self.account()
        self.start(h)
        self.event(h)
        import pilot_auth
        real_status = pilot_auth.pack_status
        calls = 0
        def fail_after_deletion(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('simulated reset failure')
            return real_status(*args, **kwargs)
        client = TestClient(app, raise_server_exceptions=False)
        with patch('pilot_auth.pack_status', side_effect=fail_after_deletion):
            r = client.post('/pilot/auth/reset', headers=h,
                            json={'password':'survey-test-password','request_id':str(uuid4())})
        self.assertEqual(r.status_code, 500)
        status = self.client.get('/pilot/pack', headers=h).json()
        self.assertEqual(status['user_points'], 5)
        self.assertEqual(self.history(h)['account_reset_count'], 0)
        self.assertEqual(self.history(h)['resets'], [])

    def test_failed_usage_rolls_back_milestone_and_source_event(self):
        _, h = self.account()
        self.start(h)
        for _ in range(19):
            self.event(h)
        # The milestone is flushed before Oracle payload creation, then rolled back
        # with the source event if the request cannot commit.
        client = TestClient(app, raise_server_exceptions=False)
        with patch('pilot.stock_message_payload', side_effect=RuntimeError('simulated failure')):
            r = client.post('/pilot/pack/usage?defer_oracle=true', headers=h,
                            json={'request_id':str(uuid4())})
        self.assertEqual(r.status_code, 500)
        status = self.client.get('/pilot/pack', headers=h).json()
        self.assertEqual(status['user_points'], 95)
        self.assertEqual(self.history(h)['lifetime_cashback_milestone_count'], 0)
        self.assertEqual(self.history(h)['milestones'], [])
        self.event(h)
        self.assertEqual(self.history(h)['lifetime_cashback_milestone_count'], 1)


if __name__ == '__main__':
    unittest.main()
