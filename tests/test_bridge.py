"""Offline contract tests: no API token, no connection, no real documents."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.bridge import (BridgeError, build_order_payload, do_create, do_lookup,
                            entity_meta, execute, fmt_rub, preview_fingerprint,
                            to_minor_rub, validate_order)


P = "65a4f178-17cd-41cc-a6f3-111111111111"
A = "65a4f178-17cd-41cc-a6f3-222222222222"
O = "65a4f178-17cd-41cc-a6f3-333333333333"
S = "65a4f178-17cd-41cc-a6f3-444444444444"
PREVIEW = "preview-test-0001"
CREATE = "create-test-0001"


def order_req(action="preview_order", kind="purchaseorder"):
    return {"request_id": PREVIEW if action == "preview_order" else CREATE,
            "action": action, "kind": kind, "agent_id": A, "organization_id": O, "store_id": S,
            "description": "Заказ для проверки", "positions": [
                {"assortment_type": "product", "assortment_id": P, "quantity": "2", "price_rub": "149.90"}]}


class MockAPI:
    def __init__(self, existing=False):
        self.existing = existing
        self.posts = []
        self.calls = []

    def get_entity(self, kind, id_):
        return {"id": id_, "name": {"counterparty": "Поставщик А", "organization": "Линия спорта",
                                   "store": "Главный склад", "product": "Вода 0,5"}.get(kind, kind)}

    def call(self, method, endpoint, *, params=None, body=None):
        self.calls.append((method, endpoint, params))
        if method == "GET":
            if endpoint.startswith("/entity/") and params is not None and "filter" in params:
                return {"rows": [{"id": P, "name": "Заказ 5", "externalCode": params["filter"].split("=", 1)[1]}] if self.existing else []}
            return {"rows": [{"id": P, "name": "Вода 0,5", "buyPrice": {"value": 12990},
                               "salePrices": [{"value": 14990, "priceType": {"name": "Розничная"}}]}]}
        self.posts.append(body)
        return {"id": P, "name": "00001", "meta": {"uuidHref": "https://online.moysklad.ru/app/#test"}}


class BridgeTests(unittest.TestCase):
    def test_price_in_kopecks(self):
        self.assertEqual(to_minor_rub("149.90"), 14990)
        self.assertEqual(to_minor_rub("0"), 0)
        self.assertEqual(fmt_rub(14990), "149.90")
        with self.assertRaises(BridgeError):
            to_minor_rub("1.999")
        with self.assertRaises(BridgeError):
            to_minor_rub("nan")
        with self.assertRaises(BridgeError):
            to_minor_rub(10.20)

    def test_invalid_quantity_and_price_rejected(self):
        req = order_req()
        req["positions"][0]["quantity"] = "0"
        with self.assertRaises(BridgeError):
            validate_order(req)
        req = order_req()
        req["positions"][0]["price_rub"] = "0"
        with self.assertRaises(BridgeError):
            validate_order(req)

    def test_invalid_order_type_rejected(self):
        req = order_req()
        req["kind"] = "demand"
        with self.assertRaises(BridgeError):
            validate_order(req)

    def test_preview_is_read_only_and_precise(self):
        api = MockAPI()
        with tempfile.TemporaryDirectory() as tmp:
            preview = execute(order_req(), api, Path(tmp))
        self.assertEqual(preview["status"], "ok")
        self.assertEqual(preview["total_rub"], "299.80")
        self.assertEqual(preview["positions"][0]["name"], "Вода 0,5")
        self.assertFalse(api.posts)

    def test_preview_hash_changes_when_order_changes(self):
        first = order_req()
        second = order_req()
        second["positions"][0]["quantity"] = "3"
        self.assertNotEqual(preview_fingerprint(first), preview_fingerprint(second))

    def test_no_create_without_explicit_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = MockAPI()
            req = order_req("create_order")
            with self.assertRaises(BridgeError):
                execute(req, api, Path(tmp), writes_enabled=True)
            self.assertFalse(api.posts)

    def test_write_disabled_even_with_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            preview = execute(order_req(), MockAPI(), path)
            (path / (PREVIEW + ".json")).write_text(json.dumps(preview))
            req = order_req("create_order")
            req.update({"confirmed": True, "approved_preview_id": PREVIEW})
            with self.assertRaisesRegex(BridgeError, "Запись отключена"):
                execute(req, MockAPI(), path, writes_enabled=False)

    def test_changed_order_cannot_use_previous_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            preview = execute(order_req(), MockAPI(), path)
            (path / (PREVIEW + ".json")).write_text(json.dumps(preview))
            req = order_req("create_order")
            req.update({"confirmed": True, "approved_preview_id": PREVIEW})
            req["positions"][0]["quantity"] = "100"
            with self.assertRaisesRegex(BridgeError, "изменён"):
                execute(req, MockAPI(), path, writes_enabled=True)

    def test_exact_approved_order_creates_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            preview = execute(order_req(), MockAPI(), path)
            (path / (PREVIEW + ".json")).write_text(json.dumps(preview))
            req = order_req("create_order")
            req.update({"confirmed": True, "approved_preview_id": PREVIEW})
            api = MockAPI()
            created = execute(req, api, path, writes_enabled=True)
            self.assertEqual(created["status"], "created")
            self.assertEqual(created["total_rub"], "299.80")
            self.assertEqual(created["document_name"], "00001")
            self.assertIn("Вода 0,5", created["telegram_text"])
            self.assertEqual(len(api.posts), 1)
            body = api.posts[0]
            self.assertEqual(body["externalCode"], "LSAI-" + CREATE)
            self.assertFalse(body["applicable"])
            self.assertEqual(body["positions"][0]["price"], 14990)
            self.assertEqual(body["store"], entity_meta("store", S))

    def test_idempotence_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            preview = execute(order_req(), MockAPI(), path)
            (path / (PREVIEW + ".json")).write_text(json.dumps(preview))
            req = order_req("create_order")
            req.update({"confirmed": True, "approved_preview_id": PREVIEW})
            api = MockAPI(existing=True)
            existing = execute(req, api, path, writes_enabled=True)
            self.assertEqual(existing["status"], "already_exists")
            self.assertEqual(api.posts, [])

    def test_lookup_redacts_counterparty_contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = MockAPI()
            result = execute({"request_id":"lookup-test-001", "action":"lookup", "entity":"counterparty", "query":"Пост"}, api, Path(tmp))
            self.assertEqual(result["status"], "ok")
            self.assertEqual(set(result["matches"][0]), {"id", "name"})

    def test_quote_reports_price_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = do_lookup({"entity":"product", "query":"вода"}, MockAPI())
            self.assertEqual(result["matches"][0]["salePrices"][0]["value_rub"], "149.90")
            self.assertEqual(result["matches"][0]["buyPrice_rub"], "129.90")


if __name__ == "__main__":
    unittest.main()
