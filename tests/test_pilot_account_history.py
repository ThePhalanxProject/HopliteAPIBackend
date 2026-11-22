"""Persistent reward/reset tracking through the authenticated HTTP API."""
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
from models import PilotAccountResetEvent, PilotCashbackMilestone, PilotPack, PilotRefillEvent, PilotUsageEvent


class AccountHistoryTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.client = TestClient(app)

    def account(self, language='en'):
        return survey_tests.SurveyTests.account(self, language)

    def start(self, headers):
        r = self.client.post('/pilot/pack/start', headers=headers)
        self.assertEqual(r.status_code, 200, r.text)

    def event(self, headers, kind='usage', request_id=None):
        request_id = request_id or str(uuid4())
        r = self.client.post('/pilot/pack/'+kind+'?defer_oracle=true', headers=headers,
                             json={'request_id':request_id})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json(), request_id

    def history(self, headers, query=''):
        r = self.client.get('/pilot/account/history'+query, headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_usage_and_refill_milestones_and_retries(self):
        _, h = self.account()
        self.start(h)
        for _ in range(19):
            result, _ = self.event(h)
        self.assertEqual(result['user_points'], 95)
        self.assertEqual(result['lifetime_cashback_milestone_count'], 0)
        result, key = self.event(h, 'refill')
        self.assertEqual(result['user_points'], 105)
        self.assertEqual(result['cashback_progress_points'], 5)
        self.assertEqual(result['cashback_unlocked_cents'], 100)
        self.assertEqual(result['lifetime_cashback_milestone_count'], 1)
        self.assertEqual(self.event(h, 'refill', key)[0]['lifetime_cashback_milestone_count'], 1)
        for _ in range(19):
            result, _ = self.event(h)
        self.assertEqual(result['user_points'], 200)
        self.assertEqual(result['lifetime_cashback_milestone_count'], 2)
        hist = self.history(h)
        self.assertEqual(hist['lifetime_cashback_earned_cents'], 200)
        self.assertEqual([m['points_threshold'] for m in hist['milestones']], [200,100])
        self.assertEqual([m['source_event_type'] for m in hist['milestones']], ['usage','refill'])
        self.assertTrue(all(not m['reconstructed'] for m in hist['milestones']))
        self.assertEqual(hist['milestones'][1]['source_request_id'], key)
        self.assertEqual(len(self.history(h, '?limit=1')['milestones']), 1)
        self.assertEqual(self.history(h, '?limit=1&offset=1')['milestones'][0]['points_threshold'], 100)
        # Polling and replaying a recorded event never increment the count.
        self.assertEqual(self.history(h)['lifetime_cashback_milestone_count'], 2)
        self.assertEqual(self.client.get('/pilot/pack', headers=h).json()['lifetime_cashback_milestone_count'], 2)

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
        self.assertEqual(reset['account_reset_count'], 1)
        self.assertEqual(reset['lifetime_cashback_milestone_count'], 1)
        self.assertEqual(reset['lifetime_cashback_earned_cents'], 100)
        self.assertEqual(self.client.post('/pilot/auth/reset', headers=h, json=body).json()['account_reset_count'], 1)
        self.start(h)
        self.event(h)
        # A delayed duplicate reset must not erase newly recorded activity.
        duplicate = self.client.post('/pilot/auth/reset', headers=h, json=body)
        self.assertEqual(duplicate.json()['user_points'], 5)
        for _ in range(19):
            result, _ = self.event(h)
        self.assertEqual(result['lifetime_cashback_milestone_count'], 2)
        self.assertEqual(result['cashback_earned_cents'], 100)
        hist = self.history(h)
        self.assertEqual([m['cycle_number'] for m in hist['milestones']], [1,0])
        self.assertEqual(hist['resets'][0]['points_before_reset'], 100)
        # Password-only clients remain compatible; each accepted reset counts.
        r = self.client.post('/pilot/auth/reset', headers=h, json={'password':'survey-test-password'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['account_reset_count'], 2)
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
        self.assertEqual(datetime.fromisoformat(hist['milestones'][0]['reached_at']).replace(tzinfo=timezone.utc), reached_at)
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
        self.assertEqual(self.client.get('/pilot/account/history').status_code, 401)

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
        self.assertEqual(status['account_reset_count'], 0)
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
        self.assertEqual(status['lifetime_cashback_milestone_count'], 0)
        self.assertEqual(self.history(h)['milestones'], [])
        self.assertEqual(self.event(h)[0]['lifetime_cashback_milestone_count'], 1)


if __name__ == '__main__':
    unittest.main()
