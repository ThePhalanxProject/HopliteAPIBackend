"""Amazon discovery and explicit Oracle language rules; no external API calls."""
import json
import os
import unittest
from threading import Barrier
from unittest.mock import Mock, patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import agent
import pilot_amazon
import pilot_oracle


def offer(source="Carrefour", price=10, link="https://www.carrefour.fr/item"):
    return {"source": source, "title": "Ariel Liquid Laundry Detergent 50 washes",
            "price": f"EUR {price}", "extracted_price": price, "link": link}


class AmazonSearchTests(unittest.TestCase):
    def search(self, general, amazon, **kwargs):
        with patch.dict(os.environ, {"SERPAPI_API_KEY":"test-key"}), \
                patch("pilot_oracle.shopping_results", return_value=general), \
                patch("pilot_oracle.amazon_results", return_value=amazon) as lookup:
            result = pilot_oracle.search_price_offers("Ariel", "fr", "Amiens", "fr", **kwargs)
            self.assertEqual(lookup.call_count, 1)
            return result

    def test_amazon_is_searched_even_with_plenty_of_chain_results(self):
        rows = [offer(source, i+1, f"https://example.test/{i}") for i, source in
                enumerate(("Carrefour","E.Leclerc","Intermarché","Auchan","Super U","Lidl"))]
        result = self.search(rows, [offer("Amazon.fr", 30, "https://www.amazon.fr/dp/B000000001")],
                             refresh=True, postal_code="80000")
        self.assertEqual(len(result["offers"]), 5)
        amazon = next(o for o in result["offers"] if o["retailer"]=="Amazon.fr")
        self.assertEqual(amazon["price_per_wash"], 0.6)
        self.assertEqual(amazon["outlet_url"], "https://www.amazon.fr/")
        self.assertTrue(pilot_oracle.major_retailer("Amazon.fr", "fr"))

    def test_amazon_must_pass_brand_consumer_location_and_wash_rules(self):
        rows = [
            offer("Amazon.fr", 2, "https://www.amazon.fr/dp/B000000001"),
            {**offer("Amazon.fr", 2, "https://www.amazon.fr/dp/B000000002"), "title":"Persil Liquid 50 washes"},
            {**offer("Amazon.fr", 2, "https://www.amazon.fr/dp/B000000003"), "title":"Ariel liquid laundry detergent"},
            {**offer("Amazon.fr", 2, "https://www.amazon.fr/dp/B000000004"), "price":"EUR 2 ex VAT"},
            {**offer("Amazon.fr", 2, "https://www.amazon.fr/dp/B000000005"), "delivery":["Delivery to Berlin"]},
            offer("Amazon.de", 2, "https://www.amazon.de/dp/B000000006"),
            offer("Amazon Business", 2, "https://www.amazon.fr/dp/B000000007"),
        ]
        result = self.search([], rows)
        self.assertEqual(len(result["offers"]), 1)
        self.assertIn("B000000001", result["offers"][0]["url"])

    def test_deduplicates_the_same_amazon_product_from_both_sources(self):
        result = self.search(
            [offer("Amazon.fr", 10, "https://www.amazon.fr/dp/B000000001?tag=first")],
            [offer("Amazon.fr", 10, "https://www.amazon.fr/product/dp/B000000001?tag=second")],
        )
        self.assertEqual(len(result["offers"]), 1)

    def test_searches_are_parallel_and_each_source_can_fail_independently(self):
        barrier = Barrier(2)
        def general(*args, **kwargs):
            barrier.wait(timeout=3)
            return [offer()]
        def amazon(*args, **kwargs):
            barrier.wait(timeout=3)
            return []
        with patch("pilot_oracle.shopping_results", side_effect=general), \
                patch("pilot_oracle.amazon_results", side_effect=amazon):
            rows = pilot_oracle.combined_shopping_results("Ariel","fr","Amiens","fr","key")
            self.assertEqual(len(rows), 1)
        error = pilot_oracle.HTTPException(502, "unavailable")
        for failed in ("shopping_results", "amazon_results"):
            other = "amazon_results" if failed=="shopping_results" else "shopping_results"
            with patch("pilot_oracle."+failed, side_effect=error), \
                    patch("pilot_oracle."+other, return_value=[offer()]):
                self.assertEqual(len(pilot_oracle.combined_shopping_results("Ariel","fr","Amiens","fr","key")), 1)

    def test_uses_local_amazon_engine_and_delivery_parameters(self):
        response = Mock()
        response.json.return_value = {"organic_results":[
            {**offer(), "link_clean":"https://www.amazon.fr/dp/B000000001"},
            {**offer(), "link_clean":"https://amazon.fr.example.test/dp/B000000002"},
        ]}
        with patch("pilot_amazon.requests.get", return_value=response) as get:
            rows = pilot_amazon.amazon_results("Ariel","en","Amiens","fr","key",refresh=True,postal_code="80000")
            params = get.call_args.kwargs["params"]
            self.assertEqual(params["engine"],"amazon")
            self.assertEqual(params["amazon_domain"],"amazon.fr")
            self.assertEqual(params["delivery_zip"],"80000")
            self.assertEqual(params["shipping_location"],"FR")
            self.assertEqual(params["no_cache"],"true")
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]["source"],"Amazon.fr")
        response.json.return_value = {"organic_results":[]}
        for country, city in (("ae","Dubai"),("sa","Riyadh"),("eg","Cairo")):
            with patch("pilot_amazon.requests.get", return_value=response) as get:
                pilot_amazon.amazon_results("Ariel","fr",city,country,"key",postal_code="12345")
                params=get.call_args.kwargs["params"]
                self.assertEqual(params["delivery_city"],city)
                self.assertNotIn("delivery_zip",params)
                self.assertEqual(params["amazon_domain"],"amazon."+country)

    def test_fallback_still_targets_amazon_and_drops_unrelated_sellers(self):
        response=Mock()
        response.json.return_value={"shopping_results":[offer(), offer("Amazon",10,"https://www.amazon.com/dp/B000000001")]}
        with patch("pilot_amazon.requests.get",return_value=response) as get:
            rows=pilot_amazon.amazon_results("Ariel","en","Unknown","zz","key")
            self.assertEqual(get.call_args.kwargs["params"]["engine"],"google_shopping")
            self.assertIn("Amazon",get.call_args.kwargs["params"]["q"])
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]["source"],"Amazon")


class OracleLanguageTests(unittest.TestCase):
    def test_language_flag_controls_actual_agent_prompt_for_all_pilot_tasks(self):
        response=Mock()
        response.json.return_value={"message":json.dumps({"message":"Test"})}
        for language, expected, country in (("fr","French","us"),("en","English","fr")):
            for task in ("stock_update","shopping_update"):
                with patch.dict(os.environ,{"HOSTMAN_AGENT_ACCESS_ID":"test","HOSTMAN_AGENT_BEARER_TOKEN":"test"}), \
                        patch("agent.requests.post",return_value=response) as post:
                    agent.call_hoplite_agent({"task":task,"language":language,"country":country},pilot_mode=True)
                    prompt=post.call_args.kwargs["json"]["message"]
                    self.assertIn("strictly in "+expected,prompt)
                    self.assertIn("overrides the country",prompt)

    def test_summary_keeps_selected_language_prices_and_amazon_homepage(self):
        for language, label in (("fr","Par utilisation"),("en","Per use")):
            result={"reference_brand":"Ariel","location":"Amiens","country":"fr","language":language,
                    "offers":[{"title":"Ariel 50 washes","listed_price":"EUR 10","price_per_wash":0.2,
                               "currency":"€","retailer":"Amazon.fr","url":"https://www.amazon.fr/dp/B000000001",
                               "outlet_url":"https://www.amazon.fr/"}]}
            message="- Ariel | Pack: 10 | Use: 0.2 | Amazon https://www.amazon.fr/"
            with patch("pilot_oracle.oracle_copy",return_value=(message,"kimi")) as kimi:
                saved=pilot_oracle.summarize_price_offers(result,language,"fr")
                self.assertEqual(kimi.call_args.args[0]["language"],language)
                self.assertIn(label,saved["message"])
                self.assertIn("https://www.amazon.fr/",saved["message"])
                self.assertEqual(saved["language_policy_version"],agent.PILOT_LANGUAGE_POLICY_VERSION)



class SavedLanguageCacheTests(unittest.TestCase):
    def setUp(self):
        import test_pilot_surveys as surveys
        from database import Base, engine
        from fastapi.testclient import TestClient
        from main import app
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.client=TestClient(app)
        self.user_id,self.headers=surveys.SurveyTests.account(self,"fr")

    def test_saved_french_wins_over_legacy_request_flag_and_old_stock_cache(self):
        from database import SessionLocal
        from models import PilotUsageOracle
        self.client.post("/pilot/pack/start",headers=self.headers)
        from uuid import uuid4
        r=self.client.post("/pilot/pack/usage?defer_oracle=true&language=en",headers=self.headers,
                           json={"request_id":str(uuid4())})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()["language"],"fr")
        with SessionLocal.begin() as db:
            saved=db.query(PilotUsageOracle).one()
            payload=dict(saved.payload)
            payload.pop("language_policy_version")
            saved.payload=payload
            saved.message="Old English stock message"
        with patch("pilot.oracle_copy",return_value=("Votre stock est suffisant.","kimi")) as kimi:
            refreshed=self.client.post(r.json()["oracle_url"],headers=self.headers)
            self.assertEqual(refreshed.status_code,200,refreshed.text)
            self.assertEqual(kimi.call_args.args[0]["language"],"fr")
            self.assertEqual(kimi.call_args.args[0]["language_policy_version"],agent.PILOT_LANGUAGE_POLICY_VERSION)
            self.assertEqual(refreshed.json()["message"],"Votre stock est suffisant.")

    def test_old_same_language_price_cache_is_regenerated(self):
        from datetime import datetime, timezone
        from uuid import uuid4
        from database import SessionLocal
        from models import PilotPriceSearch
        key=str(uuid4())
        with SessionLocal.begin() as db:
            db.add(PilotPriceSearch(id=key,user_id=self.user_id,created_at=datetime.now(timezone.utc),
                status="complete",result={"reference_brand":"Ariel","location":"Amiens","country":"fr",
                                          "language":"fr","offers":[],"message":"Old English price summary"}))
        first=self.client.get("/pilot/prices/"+key,headers=self.headers)
        self.assertEqual(first.status_code,200,first.text)
        self.assertIsNone(first.json()["message"])
        final=self.client.get("/pilot/prices/"+key,headers=self.headers).json()
        self.assertEqual(final["language"],"fr")
        self.assertEqual(final["language_policy_version"],agent.PILOT_LANGUAGE_POLICY_VERSION)
        self.assertIn("Je",final["message"])
        self.assertNotIn("Old English",final["message"])


if __name__=="__main__":
    unittest.main()
