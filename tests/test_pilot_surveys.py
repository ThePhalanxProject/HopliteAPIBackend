"""Run with python -m unittest discover -s tests -v; uses a temporary SQLite DB."""
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from uuid import uuid4

# Configure an isolated database before importing the application.
_test_dir = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(_test_dir.name) / 'surveys.sqlite')
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from database import Base, SessionLocal, engine
from main import app
from models import PilotPack, PilotRefillSurvey, PilotSurveyAdditionalAnswers, PilotSurveyAnswers, PilotUser
from pilot import token_digest

OLD_ANSWERS = dict(enjoyed_application=True, would_use_connected_product=False,
                   found_useful=True, experience_feedback='Please add more products.')
VERSION_TWO_ANSWERS = dict(OLD_ANSWERS, share_anonymous_usage_data=False, would_pay_for_device=True)
NEW_ANSWERS = dict(enjoyed_application=True, price_comparison_beneficial=False,
                   would_buy_automatic_device=True, would_pay_for_device=False,
                   share_anonymous_usage_and_demographic_data=True, comments='More brands please.')


class SurveyTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.client = TestClient(app)

    def account(self, language='en'):
        code = uuid4().hex
        token = uuid4().hex
        with SessionLocal.begin() as db:
            user = PilotUser(participant_code=code, token_hash=token_digest(token))
            db.add(user)
            db.flush()
            user_id = user.id
            db.add(PilotPack(user_id=user_id))
        r = self.client.post('/pilot/auth/enroll', json=dict(
            email=code+'@example.test', password='survey-test-password',
            display_name='Test', invitation_token=token, language=language))
        self.assertEqual(r.status_code, 200, r.text)
        return user_id, {'Authorization': 'Bearer '+r.json()['access_token']}

    def activate(self, headers):
        self.assertEqual(self.client.post('/pilot/pack/start', headers=headers).status_code, 200)
        r = self.client.post('/pilot/pack/usage?defer_oracle=true', headers=headers,
                             json={'request_id': str(uuid4())})
        self.assertEqual(r.status_code, 200, r.text)
        return self.client.get('/pilot/surveys/questions', headers=headers).json()

    def test_six_questions_localization_storage_and_isolation(self):
        user_id, h = self.account('fr')
        preview = self.client.get('/pilot/surveys/questions', headers=h).json()
        self.assertFalse(preview['can_submit'])
        self.assertEqual(preview['version'], 3)
        q = self.activate(h)
        self.assertEqual(len(q['questions']), 6)
        self.assertEqual([x['answer_type'] for x in q['questions']],
                         ['yes_no', 'yes_no', 'yes_no', 'yes_no', 'yes_no', 'text'])
        self.assertEqual(q['questions'][5]['max_length'], 2000)
        self.assertIn('anonymement', q['questions'][4]['text'])
        self.assertIn('20 € et 30 €', q['questions'][3]['text'])
        r = self.client.post(q['survey_url'], headers=h, json=NEW_ANSWERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['answers'], NEW_ANSWERS)
        self.assertEqual(self.client.post(q['survey_url'], headers=h, json=NEW_ANSWERS).status_code, 200)
        changed = dict(NEW_ANSWERS, would_pay_for_device=True)
        self.assertEqual(self.client.post(q['survey_url'], headers=h, json=changed).status_code, 409)
        with SessionLocal() as db:
            saved = db.get(PilotSurveyAnswers, q['survey_id'])
            self.assertTrue(saved.share_anonymous_usage_and_demographic_data)
            self.assertFalse(saved.would_pay_for_device)
            self.assertFalse(saved.price_comparison_beneficial)
            self.assertTrue(saved.would_buy_automatic_device)
            self.assertEqual(db.get(PilotRefillSurvey, q['survey_id']).user_id, user_id)
        _, other = self.account()
        self.assertEqual(self.client.get(q['survey_url'], headers=other).status_code, 404)
        self.assertEqual(self.client.get('/pilot/surveys', headers=other).json(), [])
        self.client.put('/pilot/preferences/message-language', headers=h, json={'language':'en'})
        result = self.client.get(q['survey_url'], headers=h).json()
        self.assertEqual(result['questions'][4]['text'],
                         'Do you agree to share your usage and demographic data anonymously?')
        self.assertIn('I used it button', result['questions'][2]['text'])
        self.assertEqual([x['id'] for x in result['questions']], list(NEW_ANSWERS))
        self.assertEqual([x['position'] for x in result['questions']], list(range(1, 7)))
        self.assertEqual(q['question_count'], 6)
        self.assertEqual(q['questions'][0]['options'], [{'value':True,'label':'Oui'},{'value':False,'label':'Non'}])

    def test_required_strict_boolean_and_feedback_validation(self):
        _, h = self.account('fr')
        q = self.activate(h)
        for values in (OLD_ANSWERS, dict(NEW_ANSWERS, share_anonymous_usage_and_demographic_data='yes'),
                       dict(NEW_ANSWERS, would_pay_for_device=None),
                       dict(NEW_ANSWERS, comments='x'*2001),
                       dict(NEW_ANSWERS, comments='   ')):
            r = self.client.post(q['survey_url'], headers=h, json=values)
            self.assertEqual(r.status_code, 422, r.text)
            self.assertEqual(r.headers['Content-Language'], 'fr')
        self.assertTrue(self.client.get('/pilot/surveys/questions', headers=h).json()['can_submit'])
        values = dict(NEW_ANSWERS, comments='x'*2000)
        self.assertEqual(self.client.post(q['survey_url'], headers=h, json=values).status_code, 200)

    def test_legacy_completed_and_pending_surveys(self):
        user_id, h = self.account()
        survey_id = str(uuid4())
        with SessionLocal.begin() as db:
            db.add(PilotRefillSurvey(id=survey_id, user_id=user_id, refill_request_id=str(uuid4()),
                refilled_at=datetime.now(timezone.utc), submitted_at=datetime.now(timezone.utc),
                language='en', version=1, **OLD_ANSWERS))
        url = '/pilot/surveys/'+survey_id
        result = self.client.get(url, headers=h).json()
        self.assertEqual(result['version'], 1)
        self.assertEqual(len(result['questions']), 4)
        self.assertEqual(result['answers'], OLD_ANSWERS)
        self.assertFalse(self.client.get('/pilot/surveys/questions', headers=h).json()['can_submit'])
        self.assertEqual(self.client.post(url, headers=h, json=OLD_ANSWERS).status_code, 200)
        # A pending invitation from the old release upgrades when submitted.
        user_id, h = self.account()
        with SessionLocal.begin() as db:
            survey_id = str(uuid4())
            db.add(PilotRefillSurvey(id=survey_id, user_id=user_id, refill_request_id=str(uuid4()),
                refilled_at=datetime.now(timezone.utc), language='en', version=1))
        q = self.client.get('/pilot/surveys/questions', headers=h).json()
        self.assertEqual(q['survey_id'], survey_id)
        self.assertEqual(q['version'], 3)
        self.assertEqual(len(self.client.get(q['survey_url'], headers=h).json()['questions']), 6)
        r = self.client.post(q['survey_url'], headers=h, json=NEW_ANSWERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['version'], 3)

    def test_version_two_history_keeps_original_meanings(self):
        user_id, h = self.account()
        survey_id = str(uuid4())
        with SessionLocal.begin() as db:
            db.add(PilotRefillSurvey(id=survey_id, user_id=user_id, refill_request_id=str(uuid4()),
                refilled_at=datetime.now(timezone.utc), submitted_at=datetime.now(timezone.utc),
                language='en', version=2, **OLD_ANSWERS))
            db.flush()
            db.add(PilotSurveyAdditionalAnswers(survey_id=survey_id,
                share_anonymous_usage_data=False, would_pay_for_device=True))
        url = '/pilot/surveys/'+survey_id
        r = self.client.get(url, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        result = r.json()
        self.assertEqual(result['version'], 2)
        self.assertEqual(result['answers'], VERSION_TWO_ANSWERS)
        self.assertIn('refill button', result['questions'][5]['text'])
        self.assertNotIn('share_anonymous_usage_and_demographic_data', result['answers'])
        self.assertEqual(self.client.post(url, headers=h, json=VERSION_TWO_ANSWERS).status_code, 200)

    def test_reset_preserves_answers_and_reopens_after_usage(self):
        _, h = self.account()
        q = self.activate(h)
        self.assertEqual(self.client.post(q['survey_url'], headers=h, json=NEW_ANSWERS).status_code, 200)
        # Further usage does not issue another survey.
        self.client.post('/pilot/pack/usage?defer_oracle=true', headers=h, json={'request_id': str(uuid4())})
        self.assertFalse(self.client.get('/pilot/surveys/questions', headers=h).json()['can_submit'])
        r = self.client.post('/pilot/auth/reset', headers=h, json={'password':'survey-test-password'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get(q['survey_url'], headers=h).json()['answers'], NEW_ANSWERS)
        self.assertEqual(self.client.post(q['survey_url'], headers=h, json=NEW_ANSWERS).status_code, 409)
        self.assertFalse(self.client.get('/pilot/surveys/questions', headers=h).json()['can_submit'])
        new = self.activate(h)
        self.assertNotEqual(new['survey_id'], q['survey_id'])
        self.assertEqual(self.client.post(new['survey_url'], headers=h, json=NEW_ANSWERS).status_code, 200)
        self.assertEqual(len(self.client.get('/pilot/surveys', headers=h).json()), 2)

    def test_existing_schema_startup_requires_no_column_alteration(self):
        legacy = create_engine('sqlite://')
        old_tables = [t for t in Base.metadata.sorted_tables if t.name != 'pilot_survey_answers']
        Base.metadata.create_all(legacy, tables=old_tables)
        columns = [(c['name'], str(c['type']), c['nullable']) for c in inspect(legacy).get_columns('pilot_refill_surveys')]
        Base.metadata.create_all(legacy)
        self.assertEqual(columns, [(c['name'], str(c['type']), c['nullable']) for c in inspect(legacy).get_columns('pilot_refill_surveys')])
        self.assertIn('pilot_survey_answers', inspect(legacy).get_table_names())
        legacy.dispose()


if __name__ == '__main__':
    unittest.main()
