from datetime import datetime,timezone
import unittest
from uuid import uuid4

import test_pilot_surveys as surveys
from database import Base,SessionLocal,engine
from fastapi.testclient import TestClient
from main import app
from models import PilotRefillSurvey
from pilot_surveys import SURVEY_REQUIRED_USES


class SevenUseSurveyTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.client=TestClient(app)
        self.user_id,self.h=surveys.SurveyTests.account(self,'fr')
        self.client.post('/pilot/pack/start',headers=self.h)

    def tap(self,key=None):
        r=self.client.post('/pilot/pack/usage?defer_oracle=true',headers=self.h,json={'request_id':key or str(uuid4())})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    def questions(self):
        return self.client.get('/pilot/surveys/questions',headers=self.h).json()

    def test_no_flag_for_first_six_uses_and_activation_at_seven(self):
        self.assertEqual(SURVEY_REQUIRED_USES,7)
        for _ in range(6):
            self.assertFalse(self.tap()['survey_required'])
            q=self.questions()
            self.assertFalse(q['can_submit'])
            self.assertIsNone(q['survey_id'])
            self.assertIsNone(q['survey_url'])
            self.assertEqual(len(q['questions']),6)
        seventh=self.tap()
        self.assertTrue(seventh['survey_required'])
        self.assertTrue(self.questions()['can_submit'])
        detail=self.client.get(seventh['survey_url'],headers=self.h).json()
        self.assertEqual(detail['trigger'],'usage')
        with SessionLocal() as db:
            self.assertEqual(db.query(PilotRefillSurvey).count(),1)

    def test_legacy_pending_invitation_cannot_bypass_the_gate(self):
        key=str(uuid4())
        with SessionLocal.begin() as db:
            db.add(PilotRefillSurvey(id=key,user_id=self.user_id,refill_request_id=str(uuid4()),
                refilled_at=datetime.now(timezone.utc),language='fr',version=3))
        for _ in range(6):self.tap()
        self.assertFalse(self.questions()['can_submit'])
        r=self.client.post('/pilot/surveys/'+key,headers=self.h,json=surveys.NEW_ANSWERS)
        self.assertEqual(r.status_code,409,r.text)
        self.assertIn('7 utilisations',r.json()['detail'])
        self.tap()
        self.assertEqual(self.questions()['survey_id'],key)
        r=self.client.post('/pilot/surveys/'+key,headers=self.h,json=surveys.NEW_ANSWERS)
        self.assertEqual(r.status_code,200,r.text)
        self.assertFalse(self.questions()['can_submit'])
        self.assertFalse(self.client.get('/pilot/pack',headers=self.h).json()['survey_required'])
        with SessionLocal() as db:
            flags=db.execute(__import__('sqlalchemy').text('SELECT survey_completed,survey_ever_completed FROM pilot_user_survey_flags WHERE user_id=:id'),{'id':self.user_id}).one()
            self.assertEqual(tuple(flags),(1,1))

    def refill(self, key=None):
        r=self.client.post('/pilot/pack/refill?defer_oracle=true',headers=self.h,json={'request_id':key or str(uuid4())})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    def test_usage_retries_do_not_count_as_extra_uses(self):
        first=str(uuid4())
        self.tap(first)
        self.tap(first)
        for _ in range(5):self.tap()
        self.assertFalse(self.questions()['can_submit'])
        self.assertTrue(self.tap()['survey_required'])

    def test_refill_unlocks_before_seven_uses_and_retry_reuses_invitation(self):
        self.tap()
        key=str(uuid4())
        result=self.refill(key)
        self.assertTrue(result['survey_required'])
        self.assertEqual(self.refill(key)['survey_id'],result['survey_id'])
        self.assertTrue(self.questions()['can_submit'])
        detail=self.client.get(result['survey_url'],headers=self.h).json()
        self.assertEqual(detail['trigger'],'refill')
        self.assertEqual(detail['trigger_request_id'],key)
        self.assertEqual(detail['refill_request_id'],key)
        self.assertEqual(self.client.post(result['survey_url'],headers=self.h,json=surveys.NEW_ANSWERS).status_code,200)
        self.tap()
        self.assertFalse(self.refill()['survey_required'])
        self.assertFalse(self.questions()['can_submit'])
        with SessionLocal() as db:
            self.assertEqual(db.query(PilotRefillSurvey).count(),1)

    def test_refill_after_reset_unlocks_a_new_survey(self):
        self.tap()
        first=self.refill()
        self.assertEqual(self.client.post(first['survey_url'],headers=self.h,json=surveys.NEW_ANSWERS).status_code,200)
        r=self.client.post('/pilot/auth/reset',headers=self.h,json={'password':'survey-test-password'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertFalse(self.questions()['can_submit'])
        self.client.post('/pilot/pack/start',headers=self.h)
        self.tap()
        result=self.refill()
        self.assertTrue(result['survey_required'])
        self.assertNotEqual(result['survey_id'],first['survey_id'])

    def test_rejected_refill_does_not_unlock_survey(self):
        r=self.client.post('/pilot/pack/refill?defer_oracle=true',headers=self.h,json={'request_id':str(uuid4())})
        self.assertEqual(r.status_code,409,r.text)
        self.assertFalse(self.questions()['can_submit'])

    def test_reset_requires_seven_new_uses_and_keeps_completion_history(self):
        for _ in range(7):self.tap()
        url=self.questions()['survey_url']
        self.assertEqual(self.client.post(url,headers=self.h,json=surveys.NEW_ANSWERS).status_code,200)
        r=self.client.post('/pilot/auth/reset',headers=self.h,json={'password':'survey-test-password'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertFalse(r.json()['survey_required'])
        from sqlalchemy import text
        with SessionLocal() as db:
            flags=db.execute(text('SELECT survey_completed,survey_ever_completed FROM pilot_user_survey_flags WHERE user_id=:id'),{'id':self.user_id}).one()
            self.assertEqual(tuple(flags),(0,1))
        self.client.post('/pilot/pack/start',headers=self.h)
        for _ in range(6):self.tap()
        self.assertFalse(self.questions()['can_submit'])
        self.tap()
        self.assertTrue(self.questions()['can_submit'])
        self.assertNotEqual(self.questions()['survey_url'],url)


if __name__=='__main__':unittest.main()
