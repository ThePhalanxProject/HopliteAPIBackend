"""The SQL-only flags track submission/reset data without any app response fields."""
from datetime import datetime,timedelta,timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

os.environ.setdefault('DATABASE_URL','sqlite:///:memory:')
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from database import Base
from models import PilotUser,PilotRefillSurvey,PilotSurveyCycle


class SurveyDatabaseFlagTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        sql=(Path(__file__).resolve().parents[1]/'sql'/'pilot_user_survey_flags.sql').read_text()
        # SQLite lacks CREATE OR REPLACE VIEW; the SELECT is the exact PG view body.
        with self.engine.begin() as conn:
            conn.exec_driver_sql(sql.replace('CREATE OR REPLACE VIEW','CREATE VIEW',1))
        self.db=Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.now=datetime.now(timezone.utc)
        self.user=PilotUser(participant_code='FLAGS-A',token_hash='a'*64)
        self.other=PilotUser(participant_code='FLAGS-B',token_hash='b'*64)
        self.db.add_all([self.user,self.other]);self.db.commit()

    def flags(self,user=None):
        return self.db.execute(text('SELECT * FROM pilot_user_survey_flags WHERE user_id=:id'),
                               {'id':(user or self.user).id}).mappings().one()

    def survey(self,version=3,at=None):
        row=PilotRefillSurvey(id=str(uuid4()),user_id=self.user.id,
            refill_request_id=str(uuid4()),refilled_at=at or self.now,language='en',version=version)
        self.db.add(row);self.db.commit()
        return row

    def test_new_and_pending_users_are_false(self):
        self.assertFalse(self.flags()['survey_completed'])
        self.survey()
        self.assertFalse(self.flags()['survey_completed'])
        self.assertFalse(self.flags()['survey_ever_completed'])
        self.assertIsNone(self.flags()['last_survey_submitted_at'])

    def test_submission_changes_flags_without_refresh_or_flag_write(self):
        row=self.survey()
        row.submitted_at=self.now+timedelta(seconds=5);self.db.commit()
        self.assertTrue(self.flags()['survey_completed'])
        self.assertTrue(self.flags()['survey_ever_completed'])
        self.assertIsNotNone(self.flags()['last_survey_submitted_at'])
        self.assertFalse(self.flags(self.other)['survey_completed'])
        self.assertFalse(self.flags(self.other)['survey_ever_completed'])

    def test_reset_clears_current_flag_and_retains_history(self):
        row=self.survey()
        row.submitted_at=self.now;self.db.commit()
        reset_at=self.now+timedelta(hours=1)
        self.db.add(PilotSurveyCycle(user_id=self.user.id,started_at=reset_at));self.db.commit()
        self.assertFalse(self.flags()['survey_completed'])
        self.assertTrue(self.flags()['survey_ever_completed'])
        self.assertIsNotNone(self.flags()['last_survey_submitted_at'])
        next_survey=self.survey(at=reset_at+timedelta(seconds=10))
        self.assertFalse(self.flags()['survey_completed'])
        next_survey.submitted_at=reset_at+timedelta(seconds=20);self.db.commit()
        self.assertTrue(self.flags()['survey_completed'])

    def test_all_submitted_versions_count_and_old_invites_stay_old_after_reset(self):
        for version in (1,2,3):
            self.survey(version=version).submitted_at=self.now
        self.db.commit()
        self.assertTrue(self.flags()['survey_completed'])
        self.db.add(PilotSurveyCycle(user_id=self.user.id,started_at=self.now+timedelta(hours=1)))
        self.db.commit()
        # Even a late submitted_at on an old invite cannot complete the new cycle.
        old=self.db.query(PilotRefillSurvey).first()
        old.submitted_at=self.now+timedelta(hours=2);self.db.commit()
        self.assertFalse(self.flags()['survey_completed'])
        self.assertTrue(self.flags()['survey_ever_completed'])

    def test_flags_not_added_to_public_api_schema(self):
        from main import app
        hidden={'survey_completed','survey_ever_completed','last_survey_submitted_at'}
        for schema in app.openapi()['components']['schemas'].values():
            self.assertTrue(hidden.isdisjoint(schema.get('properties',{})))


if __name__=='__main__':unittest.main()
