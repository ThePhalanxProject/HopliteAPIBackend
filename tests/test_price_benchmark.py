from datetime import datetime,timedelta,timezone
import json
import os
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4

import test_pilot_surveys as surveys
from database import Base,SessionLocal,engine
from fastapi.testclient import TestClient
from main import app
from models import PilotPriceBenchmark
from pilot_oracle import price_offers_from_rows
import pilot_price_benchmark as bench

SETTINGS={'SERPAPI_API_KEY':'test','HOSTMAN_AGENT_ACCESS_ID':'test','HOSTMAN_AGENT_BEARER_TOKEN':'test'}
ROW={'source':'Amazon.fr','title':'Ariel Liquid Laundry Detergent 50 washes',
     'price':'EUR 10','extracted_price':10,'link':'https://www.amazon.fr/dp/B000000001'}


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.client=TestClient(app)
        self.user_id,self.h=surveys.SurveyTests.account(self,'fr')
        self.body={'brand':'Ariel','city':'Amiens','country':'fr','language':'en',
                   'confirm_kimi_web_search_enabled':True}

    def test_async_start_freezes_saved_language_and_authenticates_status(self):
        with patch.dict(os.environ,SETTINGS),patch('pilot.run_price_benchmark') as run:
            r=self.client.post('/pilot/prices/benchmarks',headers=self.h,json=self.body)
        self.assertEqual(r.status_code,202,r.text)
        self.assertEqual(r.json()['language'],'fr')
        self.assertEqual(run.call_args.args[2],'fr')
        url=r.json()['status_url']
        snapshot=self.client.get(url,headers=self.h)
        self.assertEqual(snapshot.status_code,200)
        self.assertEqual(snapshot.json()['input']['language'],'fr')
        self.assertTrue(snapshot.json()['input']['refresh'])
        self.assertEqual(self.client.get('/pilot/pack',headers=self.h).json()['user_points'],0)
        self.assertEqual(self.client.get(url).status_code,401)
        _,other=surveys.SurveyTests.account(self,'en')
        self.assertEqual(self.client.get(url,headers=other).status_code,404)
        with patch.dict(os.environ,SETTINGS):
            second=self.client.post('/pilot/prices/benchmarks',headers=self.h,json=self.body)
        self.assertEqual(second.status_code,409)

    def test_configuration_and_web_search_confirmation_required(self):
        with patch.dict(os.environ,SETTINGS):
            r=self.client.post('/pilot/prices/benchmarks',headers=self.h,
                               json={**self.body,'confirm_kimi_web_search_enabled':False})
            self.assertEqual(r.status_code,422)
            self.assertIn('Activez',r.json()['detail'])
        with patch.dict(os.environ,{'SERPAPI_API_KEY':''}):
            self.assertEqual(self.client.post('/pilot/prices/benchmarks',headers=self.h,json=self.body).status_code,503)

    def test_runner_persists_both_results_without_changing_production_mode(self):
        with patch.dict(os.environ,SETTINGS),patch('pilot.run_price_benchmark'):
            r=self.client.post('/pilot/prices/benchmarks',headers=self.h,json=self.body)
        offers=price_offers_from_rows([ROW],'Ariel','fr','Amiens','fr')['offers']
        current={'status':'complete','offers':offers,'amazon_present':True,'timings':{'total_seconds':2}}
        kimi={'status':'complete','offers':offers,'amazon_present':True,'timings':{'total_seconds':1},
              'web_search_verified':False}
        query=bench.PriceBenchmarkRequest(**self.body)
        with patch('pilot_price_benchmark.run_current',return_value=current),patch('pilot_price_benchmark.run_kimi',return_value=kimi):
            bench.run_price_benchmark(r.json()['benchmark_id'],query,'fr')
        result=self.client.get(r.json()['status_url'],headers=self.h).json()
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['comparison']['kimi_time_saved_seconds'],1)
        self.assertFalse(result['comparison']['independent_price_accuracy_verified'])
        self.assertEqual(result['comparison']['price_consistency'][0]['status'],'matches_current_offer')
        self.assertEqual(self.client.get('/pilot/pack',headers=self.h).json()['user_points'],0)

    def test_live_search_not_claimed_is_not_a_successful_benchmark(self):
        query=bench.PriceBenchmarkRequest(**self.body)
        with patch('pilot_price_benchmark.call_kimi_discovery',return_value={'offers':[ROW],'web_search_performed':False}):
            result=bench.run_kimi(query,'fr',{})
        self.assertEqual(result['status'],'no_live_search')
        self.assertEqual(result['offer_count'],0)
        self.assertFalse(bench.compare_results({'status':'failed'},result)['comparable'])

    def test_kimi_uses_shared_filters_and_never_proves_prices_by_self_report(self):
        query=bench.PriceBenchmarkRequest(**self.body)
        bad={**ROW,'title':'Persil liquid 50 washes','brand':'Ariel','link':'https://www.amazon.fr/dp/B000000002'}
        with patch('pilot_price_benchmark.call_kimi_discovery',return_value={
            'offers':[ROW,bad],'web_search_performed':True,'amazon_searched':True,'message':None}):
            result=bench.run_kimi(query,'fr',{})
        self.assertEqual(result['offer_count'],1)
        self.assertTrue(result['amazon_present'])
        self.assertFalse(result['web_search_verified'])
        self.assertFalse(result['amazon_search_verified'])
        self.assertIn('Par utilisation',result['message'])

    def test_one_agent_call_requests_live_prices_and_french(self):
        response=Mock()
        response.json.return_value={'message':json.dumps({'offers':[],'web_search_performed':False})}
        query=bench.PriceBenchmarkRequest(**self.body)
        with patch.dict(os.environ,SETTINGS),patch('pilot_price_benchmark.requests.post',return_value=response) as post:
            bench.call_kimi_discovery(query,'fr')
        self.assertEqual(post.call_count,1)
        prompt=post.call_args.kwargs['json']['message']
        self.assertIn('strictly in French',prompt)
        self.assertIn('Always attempt an Amazon search',prompt)
        self.assertIn('Use live web-search tools now',prompt)

    def test_expiry_timeout_and_profile_reset(self):
        key=str(uuid4());old=str(uuid4())
        with SessionLocal.begin() as db:
            db.add(PilotPriceBenchmark(id=key,user_id=self.user_id,created_at=datetime.now(timezone.utc)-timedelta(minutes=6),status='running',result={}))
            db.add(PilotPriceBenchmark(id=old,user_id=self.user_id,created_at=datetime.now(timezone.utc)-timedelta(hours=2),status='complete',result={}))
        self.assertEqual(self.client.get('/pilot/prices/benchmarks/'+key,headers=self.h).json()['status'],'timed_out')
        self.assertEqual(self.client.get('/pilot/prices/benchmarks/'+old,headers=self.h).status_code,404)
        r=self.client.post('/pilot/auth/reset',headers=self.h,json={'password':'survey-test-password'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.get('/pilot/prices/benchmarks/'+key,headers=self.h).status_code,404)

    def test_existing_search_pipeline_records_stage_timings(self):
        query=bench.PriceBenchmarkRequest(**self.body)
        result=price_offers_from_rows([ROW],'Ariel','fr','Amiens','fr')
        with patch('pilot_price_benchmark.search_price_offers',return_value=result),patch('pilot_oracle.oracle_copy',return_value=(None,'unavailable')):
            measured=bench.run_current(query,'fr',{})
        for key in ('search_seconds','outlet_resolution_seconds','summary_seconds','total_seconds'):
            self.assertGreaterEqual(measured['timings'][key],0)
        self.assertTrue(measured['amazon_present'])


if __name__=='__main__':unittest.main()
