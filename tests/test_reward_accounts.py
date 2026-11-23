from datetime import datetime,timedelta,timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_pilot_surveys as surveys
from database import Base,SessionLocal,engine
from fastapi.testclient import TestClient
from main import app
from models import PilotCashbackMilestone,PilotPack,PilotRefillSurvey,PilotRewardAccount,PilotRewardCredit,PilotUsageEvent,PilotUser
from pilot import provision_participant
from pilot_rewards import initialize_reward_accounts,sync_reward_account


class RewardAccountTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.client=TestClient(app)
        self.user_id,self.h=surveys.SurveyTests.account(self,'fr')
        self.client.post('/pilot/pack/start',headers=self.h)

    def tap(self,key=None):
        key=key or str(uuid4())
        r=self.client.post('/pilot/pack/usage?defer_oracle=true',headers=self.h,json={'request_id':key})
        self.assertEqual(r.status_code,200,r.text)
        return r.json(),key

    def balance(self,user_id=None):
        with SessionLocal() as db:
            return db.get(PilotRewardAccount,user_id or self.user_id).balance_cents

    def survey_url(self):
        q=self.client.get('/pilot/surveys/questions',headers=self.h).json()
        self.assertTrue(q['can_submit'])
        return q['survey_url']

    def test_survey_plus_point_reward_accumulate_and_retries_do_not_duplicate(self):
        self.assertEqual(self.balance(),0)
        for _ in range(7):self.tap()
        url=self.survey_url()
        r=self.client.post(url,headers=self.h,json=surveys.NEW_ANSWERS)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.balance(),500)
        self.assertEqual(self.client.post(url,headers=self.h,json=surveys.NEW_ANSWERS).status_code,200)
        self.assertEqual(self.balance(),500)
        for _ in range(13):result,key=self.tap()
        self.assertEqual(result['user_points'],100)
        self.assertEqual(result['cashback_unlocked_cents'],100)
        self.assertEqual(self.balance(),600)
        self.tap(key)
        self.assertEqual(self.balance(),600)
        self.client.get('/pilot/pack',headers=self.h)
        self.assertEqual(self.balance(),600)
        with SessionLocal() as db:
            rows=db.query(PilotRewardCredit).filter_by(user_id=self.user_id).all()
            self.assertEqual(sorted((r.reward_type,r.amount_cents) for r in rows),[('points',100),('survey',500)])
            self.assertEqual(sum(r.amount_cents for r in rows),self.balance())

    def test_refill_can_cross_one_hundred_without_resetting_progress(self):
        for _ in range(19):self.tap()
        self.assertEqual(self.balance(),0)
        key=str(uuid4())
        for _ in range(2):
            r=self.client.post('/pilot/pack/refill?defer_oracle=true',headers=self.h,json={'request_id':key})
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()['user_points'],105)
            self.assertEqual(self.balance(),100)

    def test_activity_reset_preserves_euros_and_new_completed_survey_adds_five(self):
        for _ in range(20):self.tap()
        self.client.post(self.survey_url(),headers=self.h,json=surveys.NEW_ANSWERS)
        self.assertEqual(self.balance(),600)
        reset=self.client.post('/pilot/auth/reset',headers=self.h,json={'password':'survey-test-password'})
        self.assertEqual(reset.status_code,200,reset.text)
        self.assertEqual(reset.json()['user_points'],0)
        self.assertEqual(self.balance(),600)
        initialize_reward_accounts(engine)
        self.assertEqual(self.balance(),600)
        self.client.post('/pilot/pack/start',headers=self.h)
        for _ in range(7):self.tap()
        self.assertEqual(self.client.post(self.survey_url(),headers=self.h,json=surveys.NEW_ANSWERS).status_code,200)
        self.assertEqual(self.balance(),1100)

    def test_failed_and_other_user_submissions_do_not_credit_account(self):
        for _ in range(7):self.tap()
        url=self.survey_url()
        bad=dict(surveys.NEW_ANSWERS,would_pay_for_device='yes')
        self.assertEqual(self.client.post(url,headers=self.h,json=bad).status_code,422)
        self.assertEqual(self.balance(),0)
        other_id,other=surveys.SurveyTests.account(self,'en')
        self.assertEqual(self.client.post(url,headers=other,json=surveys.NEW_ANSWERS).status_code,404)
        self.assertEqual(self.balance(other_id),0)
        self.assertEqual(self.balance(),0)

    def test_failed_usage_rolls_back_wallet_and_credit_with_the_source(self):
        for _ in range(19):self.tap()
        with patch('pilot.stock_message_payload',side_effect=RuntimeError('simulated failure')):
            r=TestClient(app,raise_server_exceptions=False).post('/pilot/pack/usage?defer_oracle=true',headers=self.h,
                  json={'request_id':str(uuid4())})
        self.assertEqual(r.status_code,500)
        self.assertEqual(self.balance(),0)
        with SessionLocal() as db:self.assertEqual(db.query(PilotRewardCredit).count(),0)
        self.tap()
        self.assertEqual(self.balance(),100)

    def test_failed_survey_rolls_back_wallet_and_can_be_retried(self):
        for _ in range(7):self.tap()
        url=self.survey_url()
        with patch('pilot.survey_view',side_effect=RuntimeError('simulated failure')):
            r=TestClient(app,raise_server_exceptions=False).post(url,headers=self.h,json=surveys.NEW_ANSWERS)
        self.assertEqual(r.status_code,500)
        self.assertEqual(self.balance(),0)
        with SessionLocal() as db:
            self.assertEqual(db.query(PilotRewardCredit).count(),0)
            self.assertIsNone(db.query(PilotRefillSurvey).one().submitted_at)
        self.assertEqual(self.client.post(url,headers=self.h,json=surveys.NEW_ANSWERS).status_code,200)
        self.assertEqual(self.balance(),500)

    def test_backfill_recovers_retained_point_milestones_and_all_survey_versions_once(self):
        old=datetime.now(timezone.utc)-timedelta(days=2)
        with SessionLocal.begin() as db:
            pack=db.query(PilotPack).filter_by(user_id=self.user_id).one()
            for i in range(20):
                db.add(PilotUsageEvent(pack_id=pack.id,request_id=str(uuid4()),volume_used_ml=65,occurred_at=old+timedelta(minutes=i)))
            for version in (1,2,3):
                db.add(PilotRefillSurvey(id=str(uuid4()),user_id=self.user_id,refill_request_id=str(uuid4()),
                    refilled_at=old,submitted_at=old+timedelta(hours=1),language='fr',version=version))
        initialize_reward_accounts(engine)
        self.assertEqual(self.balance(),1600)
        initialize_reward_accounts(engine)
        self.assertEqual(self.balance(),1600)
        with SessionLocal() as db:
            sync_reward_account(db,self.user_id)
            sync_reward_account(db,self.user_id)
            db.commit()
            self.assertEqual(db.query(PilotRewardCredit).count(),4)
        self.assertEqual(self.balance(),1600)

    def test_provisioning_creates_zero_balance_and_historical_award_amount_is_preserved(self):
        with SessionLocal() as db:
            provision_participant(db,'REWARD-NEW')
            user=db.query(PilotUser).filter_by(participant_code='REWARD-NEW').one()
            self.assertEqual(db.get(PilotRewardAccount,user.id).balance_cents,0)
        with SessionLocal.begin() as db:
            db.add(PilotCashbackMilestone(user_id=self.user_id,cycle_number=0,milestone_number=1,points_threshold=100,
                cashback_cents=500,reached_at=datetime.now(timezone.utc),recorded_at=datetime.now(timezone.utc),
                source_request_id=str(uuid4()),source_event_type='usage',reconstructed=False))
        initialize_reward_accounts(engine)
        self.assertEqual(self.balance(),500)

    def test_new_balance_is_database_only(self):
        for schema in app.openapi()['components']['schemas'].values():
            self.assertTrue({'balance_cents','reward_balance_cents','reward_credits'}.isdisjoint(schema.get('properties',{})))
        self.assertFalse(any('reward-account' in path for path in app.openapi()['paths']))
        self.assertNotIn('balance_cents',self.client.get('/pilot/pack',headers=self.h).json())


if __name__=='__main__':unittest.main()
