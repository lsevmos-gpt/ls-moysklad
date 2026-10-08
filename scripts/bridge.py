"""Minimal, fail-closed MoySklad order bridge (Python 3.12, stdlib only).

Never put API tokens or passwords in request JSON. The runner injects
MOYSKLAD_TOKEN from a GitHub Actions repository secret.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib import error, parse, request

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
# This is a standalone Crush-PRO accounting project; all order documents belong to this organization.
CRUSH_PRO_ORGANIZATION_ID = "62774749-69ef-11e7-7a34-5acf0009fff7"
EXTERNAL_ORDER_PREFIX = "CPAI-"
VALID_TYPES = {"product", "variant", "service"}
VALID_LOOKUP_TYPES = VALID_TYPES | {"counterparty", "organization", "store"}
VALID_KINDS = {"customerorder", "purchaseorder"}
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-zA-Z_-]{8,60}$")
MAX_POSITION_COUNT = 150
MAX_ORDER_RUB = Decimal("100000000.00")


class BridgeError(Exception):
    """User-actionable safe bridge error."""


def ensure_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or not UUID_PATTERN.fullmatch(value):
        raise BridgeError(f"{field}: требуется UUID записи из МоегоСклада")
    return value.lower()


def ensure_request_id(value: Any) -> str:
    if not isinstance(value, str) or not REQUEST_ID_PATTERN.fullmatch(value):
        raise BridgeError("request_id: используйте 8–60 символов a-z, A-Z, 0-9, _ или -")
    return value


def to_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str):
        raise BridgeError(f"{field}: необходимо указать строку с числом, например '3' или '1290.50'")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise BridgeError(f"{field}: некорректное число") from None
    if not number.is_finite():
        raise BridgeError(f"{field}: число должно быть конечным")
    return number


def to_minor_rub(value: Any) -> int:
    n = to_decimal(value, "price_rub")
    if n < 0 or n > MAX_ORDER_RUB:
        raise BridgeError("price_rub: цена вне допустимого диапазона")
    if n != n.quantize(Decimal("0.01")):
        raise BridgeError("price_rub: не более 2 знаков после запятой")
    return int((n * 100).to_integral_value(rounding=ROUND_HALF_UP))


def canonical_request(req: dict) -> dict:
    """Only business-critical inputs contribute to preview approval hash."""
    return {
        "kind": req.get("kind"),
        "agent_id": req.get("agent_id"),
        "organization_id": CRUSH_PRO_ORGANIZATION_ID,
        "store_id": req.get("store_id"),
        "description": req.get("description", ""),
        "positions": req.get("positions"),
    }


def preview_fingerprint(req: dict) -> str:
    packed = json.dumps(canonical_request(req), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def validate_order(req: dict) -> dict:
    kind = req.get("kind")
    if kind not in VALID_KINDS:
        raise BridgeError("kind: разрешены только customerorder и purchaseorder")
    agent = ensure_uuid(req.get("agent_id"), "agent_id")
    requested_org = req.get("organization_id")
    if requested_org is not None:
        requested_org = ensure_uuid(requested_org, "organization_id")
        if requested_org != CRUSH_PRO_ORGANIZATION_ID:
            raise BridgeError("Доступна только организация Crush-PRO из подключённого МоегоСклада")
    organization = CRUSH_PRO_ORGANIZATION_ID
    store = ensure_uuid(req.get("store_id"), "store_id")
    description = req.get("description", "")
    if not isinstance(description, str) or len(description) > 1200:
        raise BridgeError("description: текст до 1200 символов")
    positions = req.get("positions")
    if not isinstance(positions, list) or not 1 <= len(positions) <= MAX_POSITION_COUNT:
        raise BridgeError(f"positions: от 1 до {MAX_POSITION_COUNT} позиций")
    clean = []
    total_minor = Decimal(0)
    for index, p in enumerate(positions, start=1):
        if not isinstance(p, dict):
            raise BridgeError(f"positions[{index}]: ожидается объект")
        type_ = p.get("assortment_type")
        if type_ not in VALID_TYPES:
            raise BridgeError(f"positions[{index}].assortment_type: разрешены product, variant, service")
        item_id = ensure_uuid(p.get("assortment_id"), f"positions[{index}].assortment_id")
        qty = to_decimal(p.get("quantity"), f"positions[{index}].quantity")
        if qty <= 0 or qty > 1_000_000 or qty.as_tuple().exponent < -3:
            raise BridgeError(f"positions[{index}].quantity: положительное количество до 3 знаков после запятой")
        price_minor = to_minor_rub(p.get("price_rub"))
        if price_minor == 0 and p.get("free_of_charge") is not True:
            raise BridgeError(f"positions[{index}]: нулевая цена требует free_of_charge=true")
        line_minor = qty * price_minor
        total_minor += line_minor
        clean.append({"assortment_type": type_, "assortment_id": item_id, "quantity": qty, "price_minor": price_minor, "line_minor": line_minor})
    if total_minor / 100 > MAX_ORDER_RUB:
        raise BridgeError("Сумма заказа превышает допустимый предел")
    return {"kind": kind, "agent_id": agent, "organization_id": organization, "store_id": store,
            "description": description, "positions": clean, "total_minor": total_minor}


def fmt_rub(minor: Decimal | int) -> str:
    return str((Decimal(minor) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def entity_meta(entity_type: str, entity_id: str) -> dict:
    return {"meta": {"href": f"{BASE_URL}/entity/{entity_type}/{entity_id}",
                     "type": entity_type, "mediaType": "application/json"}}


class MoySkladAPI:
    def __init__(self, token: str):
        if not token:
            raise BridgeError("Не настроен GitHub Secret MOYSKLAD_TOKEN")
        self.token = token

    def call(self, method: str, endpoint: str, *, params: dict | None = None, body: dict | None = None) -> dict:
        if not endpoint.startswith("/") or "//" in endpoint or ".." in endpoint or "?" in endpoint:
            raise BridgeError("Недопустимый путь API")
        url = BASE_URL + endpoint
        if params:
            url += "?" + parse.urlencode(params)
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/json;charset=utf-8",
                   "Accept-Encoding": "gzip"}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        max_attempts = 3 if method.upper() == "GET" else 1  # Never replay a POST blindly.
        for attempt in range(max_attempts):
            req = request.Request(url, data=payload, headers=headers, method=method.upper())
            try:
                with request.urlopen(req, timeout=25) as response:
                    raw = response.read(3_000_000)
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    parsed = json.loads(raw.decode("utf-8"))
                    if not isinstance(parsed, dict):
                        raise BridgeError("API вернул неожиданный формат ответа")
                    return parsed
            except error.HTTPError as exc:
                if method.upper() == "GET" and exc.code in {429, 500, 502, 503, 504} and attempt + 1 < max_attempts:
                    time.sleep(1 + attempt * 2)
                    continue
                # Expose only official numeric API error codes; never echo raw responses
                # as they may contain personal records, headers or a bearer token.
                error_codes = []
                try:
                    data = exc.read(20_000)
                    if exc.headers.get("Content-Encoding", "").lower() == "gzip":
                        data = gzip.decompress(data)
                    err = json.loads(data.decode("utf-8"))
                    if isinstance(err, dict):
                        for e in err.get("errors", [])[:4]:
                            if isinstance(e, dict) and isinstance(e.get("code"), int):
                                error_codes.append(str(e["code"]))
                except (OSError, ValueError, EOFError):
                    pass
                code_suffix = f" (API codes: {','.join(error_codes)})" if error_codes else ""
                raise BridgeError(f"МойСклад API: HTTP {exc.code}{code_suffix} при {method.upper()} {endpoint}") from None
            except (error.URLError, TimeoutError):
                if method.upper() == "GET" and attempt + 1 < max_attempts:
                    time.sleep(1 + attempt * 2)
                    continue
                raise BridgeError(f"Нет связи с МоимСкладом при {method.upper()} {endpoint}; проверьте вручную") from None
        raise BridgeError("Не удалось выполнить API-запрос")

    def get_entity(self, kind: str, entity_id: str) -> dict:
        if kind not in VALID_LOOKUP_TYPES:
            raise BridgeError("Неподдерживаемый тип сущности")
        return self.call("GET", f"/entity/{kind}/{ensure_uuid(entity_id, 'id')}")


def safe_summary(item: dict, entity_type: str) -> dict:
    fields = ["id", "name", "code"]
    if entity_type in VALID_TYPES:
        fields += ["article", "buyPrice", "salePrices", "uom"]
    # Avoid contacts, addresses, bank info, customer notes and internal ACL in Github outputs.
    result = {key: item[key] for key in fields if key in item and key != "uom"}
    if "salePrices" in result:
        prices = result["salePrices"]
        result["salePrices"] = [
            {"value_rub": fmt_rub(x.get("value", 0)), "priceType": x.get("priceType", {}).get("name")}
            for x in prices[:8] if isinstance(x, dict)
        ] if isinstance(prices, list) else []
    if "buyPrice" in result:
        val = result["buyPrice"]
        result["buyPrice_rub"] = fmt_rub(val.get("value", 0)) if isinstance(val, dict) else None
        del result["buyPrice"]
    return result


def do_lookup(req: dict, api: MoySkladAPI) -> dict:
    entity_type = req.get("entity")
    if entity_type not in VALID_LOOKUP_TYPES:
        raise BridgeError("entity: product, variant, service, counterparty, organization, store")
    term = req.get("query", "")
    if not isinstance(term, str) or len(term) > 120:
        raise BridgeError("query: строка до 120 символов")
    if term.strip() and len(term.strip()) < 2:
        raise BridgeError("Поисковая строка должна содержать минимум 2 символа")
    limit = req.get("limit", 30)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise BridgeError("limit: целое число 1–100")
    if not term.strip() and limit > 30:
        raise BridgeError("Для просмотра без поисковой строки допускается не больше 30 записей")
    params = {"limit": limit}
    if term.strip():
        params["search"] = term.strip()
    data = api.call("GET", f"/entity/{entity_type}", params=params)
    rows = data.get("rows", [])
    if not isinstance(rows, list):
        raise BridgeError("Некорректный ответ о списке сущностей")
    return {"status": "ok", "entity": entity_type, "query": term,
            "matches": [safe_summary(row, entity_type) for row in rows if isinstance(row, dict)],
            "returned": len(rows), "truncated": len(rows) == limit}


def build_preview(req: dict, api: MoySkladAPI) -> dict:
    clean = validate_order(req)
    agent = api.get_entity("counterparty", clean["agent_id"])
    org = api.get_entity("organization", clean["organization_id"])
    store = api.get_entity("store", clean["store_id"])
    lines = []
    for p in clean["positions"]:
        item = api.get_entity(p["assortment_type"], p["assortment_id"])
        lines.append({"type": p["assortment_type"], "id": p["assortment_id"],
                      "name": item.get("name", "(нет наименования)"),
                      "quantity": str(p["quantity"]), "price_rub": fmt_rub(p["price_minor"]),
                      "sum_rub": fmt_rub(p["line_minor"])})
    return {"status": "ok", "kind": clean["kind"], "agent": {"id": clean["agent_id"], "name": agent.get("name")},
            "organization": {"id": clean["organization_id"], "name": org.get("name")},
            "store": {"id": clean["store_id"], "name": store.get("name")},
            "positions": lines, "total_rub": fmt_rub(clean["total_minor"]),
            "preview_sha256": preview_fingerprint(req),
            "note": "Предпросмотр, в МоемСкладе документ не создан. Цена берётся из заявки и не заменяется автоматически. Остатки отдельно не проверены."}


def build_order_payload(req: dict, clean: dict) -> dict:
    positions = []
    for p in clean["positions"]:
        positions.append({"quantity": float(p["quantity"]), "price": p["price_minor"],
                          "assortment": entity_meta(p["assortment_type"], p["assortment_id"])})
    return {"externalCode": EXTERNAL_ORDER_PREFIX + ensure_request_id(req["request_id"]),
            "applicable": False, "description": clean["description"],
            "organization": entity_meta("organization", clean["organization_id"]),
            "store": entity_meta("store", clean["store_id"]),
            "agent": entity_meta("counterparty", clean["agent_id"]),
            "positions": positions}


def do_create(req: dict, api: MoySkladAPI, results_dir: Path, *, writes_enabled: bool) -> dict:
    clean = validate_order(req)
    if req.get("confirmed") is not True:
        raise BridgeError("Создание запрещено: требуется confirmed=true после явного подтверждения заказа")
    preview_id = ensure_request_id(req.get("approved_preview_id"))
    path = results_dir / f"{preview_id}.json"
    if not path.is_file():
        raise BridgeError("Нет сохранённого предварительного просмотра заказа")
    try:
        preview = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise BridgeError("Некорректный файл предварительного просмотра") from None
    if preview.get("status") != "ok" or preview.get("action") != "preview_order" or preview.get("request_id") != preview_id or preview.get("preview_sha256") != preview_fingerprint(req):
        raise BridgeError("Заказ изменён после предварительного просмотра: сформируйте и подтвердите новый")
    if not writes_enabled:
        raise BridgeError("Запись отключена: установите GitHub Actions variable MOYSKLAD_WRITE_ENABLED=YES после проверки проекта")
    payload = build_order_payload(req, clean)
    kind = clean["kind"]
    existing = api.call("GET", f"/entity/{kind}", params={"filter": "externalCode=" + payload["externalCode"], "limit": 2})
    records = existing.get("rows")
    if not isinstance(records, list):
        raise BridgeError("Не удалось проверить наличие заказа по externalCode; создание остановлено")
    if len(records) > 1:
        raise BridgeError("Обнаружено несколько документов с этим externalCode: необходимо проверить вручную")
    if len(records) == 1:
        doc = records[0]
        if not isinstance(doc, dict) or doc.get("externalCode") != payload["externalCode"]:
            raise BridgeError("Некорректный результат проверки дубля: создание остановлено")
        return {"status": "already_exists", "kind": kind, "document_id": doc.get("id"), "document_name": doc.get("name"), "externalCode": payload["externalCode"], "total_rub": fmt_rub(clean["total_minor"])}
    doc = api.call("POST", f"/entity/{kind}", body=payload)
    if not isinstance(doc.get("id"), str):
        raise BridgeError("Ответ API не содержит id документа; проверьте МойСклад вручную, не повторяйте запрос без проверки")
    preview_lines = preview.get("positions", [])
    result = {"status": "created", "kind": kind,
              "document_id": doc.get("id"), "document_name": doc.get("name"),
              "document_url": doc.get("meta", {}).get("uuidHref") if isinstance(doc.get("meta"), dict) else None,
              "externalCode": payload["externalCode"], "total_rub": fmt_rub(clean["total_minor"]),
              "note": "Документ создан непроведённым. Telegram, оплата, приёмка и списания не выполнялись."}
    if kind == "purchaseorder":
        lines = [f"{p.get('name', 'Товар')} — {p.get('quantity')} шт. × {p.get('price_rub')} ₽" for p in preview_lines]
        result["telegram_text"] = "Заказ поставщику\n" + "\n".join(lines) + f"\nИтого: {result['total_rub']} ₽"
    return result



def _linked_entity_id(item: dict, entity_type: str) -> str:
    if not isinstance(item, dict):
        raise BridgeError(f"Отсутствует ссылка на {entity_type}")
    meta = item.get("meta")
    href = meta.get("href") if isinstance(meta, dict) else None
    prefix = f"{BASE_URL}/entity/{entity_type}/"
    if not isinstance(href, str) or not href.startswith(prefix):
        raise BridgeError(f"Некорректная ссылка на {entity_type} в заказе")
    return ensure_uuid(href[len(prefix):], f"{entity_type}.id")


def _order_positions(api: MoySkladAPI, order_id: str) -> list[dict]:
    positions = api.call("GET", f"/entity/customerorder/{order_id}/positions", params={"limit": 100})
    rows = positions.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise BridgeError("Не удалось получить все позиции заказа")
    size = positions.get("meta", {}).get("size") if isinstance(positions.get("meta"), dict) else None
    if isinstance(size, int) and size != len(rows):
        raise BridgeError("Список позиций заказа неполный; обновление отменено")
    return rows


def _simple_line(row: dict) -> tuple[str, str, Decimal, int, str]:
    """Return (entity type, item id, quantity, price in kopecks, position id)."""
    if not isinstance(row, dict):
        raise BridgeError("Некорректная позиция заказа")
    pos_id = ensure_uuid(row.get("id"), "position.id")
    assort = row.get("assortment")
    if not isinstance(assort, dict) or not isinstance(assort.get("meta"), dict):
        raise BridgeError("Нет товара в позиции заказа")
    kind = assort["meta"].get("type")
    if kind not in VALID_TYPES:
        raise BridgeError("Неподдерживаемый тип товара в заказе")
    item_id = _linked_entity_id(assort, kind)
    try:
        qty = Decimal(str(row.get("quantity")))
    except InvalidOperation:
        raise BridgeError("Неверное количество в позиции заказа") from None
    price = row.get("price")
    if not qty.is_finite() or isinstance(price, bool) or not isinstance(price, (int, float)) or not float(price).is_integer():
        raise BridgeError("Некорректные количества или цены в позициях")
    return kind, item_id, qty, int(price), pos_id


def refresh_customer_order_line(req: dict, api: MoySkladAPI, *, writes_enabled: bool, inspect_only: bool = False) -> dict:
    """Price update of exactly one line, keeping all other lines intact.

    Refresh target price from catalog's named price type and compare with
    an explicit approved amount. The expected full line set protects against
    unintended edits or accidental replacement of the order.
    """
    if req.get("confirmed") is not True and not inspect_only:
        raise BridgeError("Нет подтверждения пользователя на изменение цены")
    order_id = ensure_uuid(req.get("order_id"), "order_id")
    customer_id = ensure_uuid(req.get("customer_id"), "customer_id")
    product_id = ensure_uuid(req.get("product_id"), "product_id")
    expected_name = req.get("expected_order_name")
    expected_code = req.get("expected_external_code")
    price_type = req.get("price_type")
    if not isinstance(expected_name, str) or not expected_name or len(expected_name) > 50:
        raise BridgeError("Номер заказа не указан")
    if not isinstance(expected_code, str) or not expected_code.startswith(EXTERNAL_ORDER_PREFIX) or len(expected_code) > 100:
        raise BridgeError("Недопустимый внешний код заказа")
    if price_type != "Опт для залов":
        raise BridgeError("Разрешён только тип цены 'Опт для залов'")
    old_price = to_minor_rub(req.get("expected_old_price_rub"))
    new_price = to_minor_rub(req.get("expected_new_price_rub"))
    old_total = to_minor_rub(req.get("expected_order_total_rub"))
    if old_price == new_price:
        raise BridgeError("Старая и новая цены совпадают")
    expected = req.get("expected_positions")
    if not isinstance(expected, list) or not 1 <= len(expected) <= 20:
        raise BridgeError("Нужно явно указать все исходные позиции заказа")
    expected_map = {}
    for p in expected:
        if not isinstance(p, dict) or p.get("assortment_type") not in VALID_TYPES:
            raise BridgeError("Неверная позиция для проверки")
        kind = p["assortment_type"]
        item_id = ensure_uuid(p.get("assortment_id"), "assortment_id")
        key = (kind, item_id)
        if key in expected_map:
            raise BridgeError("Повторяющиеся товары в проверяемом заказе не поддерживаются")
        qty = to_decimal(p.get("quantity"), "quantity")
        if qty <= 0:
            raise BridgeError("Некорректное ожидаемое количество")
        expected_map[key] = (qty, to_minor_rub(p.get("price_rub")))
    target_key = ("product", product_id)
    if target_key not in expected_map:
        raise BridgeError("Изменяемый товар отсутствует в ожидаемых позициях")
    if expected_map[target_key][1] != old_price:
        raise BridgeError("Ожидаемая старая цена товара не совпадает")
    if expected_map[target_key][0] != Decimal("1"):
        raise BridgeError("Изменение цены допустимо только для одной единицы товара")

    # Read live catalog price from the selected tier rather than trusting chat text.
    product = api.get_entity("product", product_id)
    sale_prices = product.get("salePrices")
    if not isinstance(sale_prices, list):
        raise BridgeError("Цены товара в каталоге недоступны")
    matches = [x["value"] for x in sale_prices
               if isinstance(x, dict) and isinstance(x.get("priceType"), dict)
               and x["priceType"].get("name") == price_type
               and isinstance(x.get("value"), (int, float)) and not isinstance(x.get("value"), bool) and float(x.get("value")).is_integer()]
    if len(matches) != 1:
        raise BridgeError("Невозможно однозначно определить актуальную цену товара")
    if matches[0] != new_price:
        raise BridgeError(f"Цена каталога {fmt_rub(matches[0])} ₽ не совпадает с ожидаемой {fmt_rub(new_price)} ₽")

    order = api.call("GET", f"/entity/customerorder/{order_id}")
    if order.get("id") != order_id or order.get("name") != expected_name or order.get("externalCode") != expected_code:
        raise BridgeError("Номер, идентификатор или внешний код заказа изменился; изменение отменено")
    if _linked_entity_id(order.get("organization"), "organization") != CRUSH_PRO_ORGANIZATION_ID:
        raise BridgeError("Заказ относится к другой организации")
    if _linked_entity_id(order.get("agent"), "counterparty") != customer_id:
        raise BridgeError("Заказ относится к другому покупателю")
    if order.get("applicable") is not False:
        raise BridgeError("Заказ проведён или имеет неизвестное состояние; цена не меняется")
    rows = _order_positions(api, order_id)
    actual = {}
    for row in rows:
        kind, item_id, qty, price, pos_id = _simple_line(row)
        key = (kind, item_id)
        if key in actual:
            raise BridgeError("Дублированная позиция товара; изменение отменено")
        actual[key] = (qty, price, pos_id)
    if set(actual) != set(expected_map):
        raise BridgeError("Состав заказа изменён после подтверждения; изменение отменено")
    target = actual[target_key]
    for key, (qty, price, pos_id) in actual.items():
        expected_qty, expected_price = expected_map[key]
        if qty != expected_qty or (price != expected_price and not (key == target_key and price == new_price)):
            raise BridgeError("Количество или цена другой позиции заказа изменена; изменение отменено")
    updated_total = old_total + new_price - old_price
    current_total = order.get("sum")
    if isinstance(current_total, bool) or not isinstance(current_total, (int, float)) or not float(current_total).is_integer():
        raise BridgeError("Невозможно подтвердить общую сумму заказа")
    if target[1] == new_price:
        if int(current_total) != updated_total:
            raise BridgeError("Цена товара уже обновлена, но итог заказа не совпадает")
        return {"status": "already_updated", "order_name": expected_name, "order_id": order_id,
                "product_name": product.get("name"), "old_price_rub": fmt_rub(old_price),
                "new_price_rub": fmt_rub(new_price), "total_rub": fmt_rub(current_total),
                "changed_positions": 0, "document_url": order.get("meta", {}).get("uuidHref")}
    if target[1] != old_price or int(current_total) != old_total:
        raise BridgeError("Фактическая цена или сумма заказа отличаются от ожидаемых; изменение отменено")
    if inspect_only:
        return {"status": "ok", "order_name": expected_name, "order_id": order_id,
                "product_name": product.get("name"), "old_price_rub": fmt_rub(old_price),
                "new_price_rub": fmt_rub(new_price), "old_total_rub": fmt_rub(old_total),
                "new_total_rub": fmt_rub(updated_total), "position_id": target[2],
                "note": "Только проверка. Изменения в заказ не внесены."}
    if not writes_enabled:
        raise BridgeError("Запись в МойСклад выключена; изменение отменено")
    endpoint = f"/entity/customerorder/{order_id}/positions/{target[2]}"
    api.call("PUT", endpoint, body={"price": new_price})
    # A non-idempotent network error is never blindly retried.
    verified = api.call("GET", endpoint)
    _, verified_id, verified_qty, verified_price, verified_pos = _simple_line(verified)
    check_doc = api.call("GET", f"/entity/customerorder/{order_id}")
    if verified_id != product_id or verified_qty != target[0] or verified_price != new_price or verified_pos != target[2]:
        raise BridgeError("Ответ проверки позиции после записи отличается: проверьте заказ вручную")
    if check_doc.get("sum") != updated_total:
        raise BridgeError("Итог заказа после изменения отличается от ожидаемого: проверьте вручную")
    return {"status": "updated", "order_name": expected_name, "order_id": order_id,
            "product_name": product.get("name"), "old_price_rub": fmt_rub(old_price),
            "new_price_rub": fmt_rub(new_price), "old_total_rub": fmt_rub(old_total),
            "total_rub": fmt_rub(updated_total), "changed_positions": 1,
            "document_url": check_doc.get("meta", {}).get("uuidHref")}




def refresh_from_confirmed_order(req: dict, api: MoySkladAPI, results_dir: Path, *, writes_enabled: bool) -> dict:
    """Reuse an already-created, approved order as the only baseline."""
    original_id = ensure_request_id(req.get("source_order_request_id"))
    original_file = results_dir.parent / "requests" / f"{original_id}.json"
    result_file = results_dir / f"{original_id}.json"
    if not original_file.is_file() or not result_file.is_file():
        raise BridgeError("Подтверждённый исходный заказ и результат создания не найдены")
    try:
        source = json.loads(original_file.read_text(encoding="utf-8"))
        result = json.loads(result_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise BridgeError("Некорректные исходные документы") from None
    if not isinstance(source, dict) or not isinstance(result, dict):
        raise BridgeError("Некорректные исходные документы")
    if source.get("action") != "create_order" or source.get("kind") != "customerorder" or source.get("confirmed") is not True:
        raise BridgeError("Исходный заказ не является подтверждённым заказом покупателя")
    if result.get("status") not in {"created", "already_exists"} or result.get("request_id") != original_id:
        raise BridgeError("Нет подтверждения создания исходного заказа")
    product_id = ensure_uuid(req.get("product_id"), "product_id")
    expected_lines = source.get("positions")
    if not isinstance(expected_lines, list):
        raise BridgeError("Не найдены позиции исходного заказа")
    targeted = [p for p in expected_lines if isinstance(p, dict)
                and p.get("assortment_type") == "product" and p.get("assortment_id") == product_id]
    if len(targeted) != 1:
        raise BridgeError("Нужный товар не найден или повторяется в исходном заказе")
    derived = dict(req)
    derived["order_id"] = result.get("document_id")
    derived["customer_id"] = source.get("agent_id")
    derived["expected_order_name"] = result.get("document_name")
    derived["expected_external_code"] = result.get("externalCode")
    derived["expected_old_price_rub"] = targeted[0].get("price_rub")
    derived["expected_order_total_rub"] = result.get("total_rub")
    derived["expected_positions"] = expected_lines
    return refresh_customer_order_line(derived, api, writes_enabled=writes_enabled, inspect_only=False)



def execute(req: dict, api: MoySkladAPI, results_dir: Path, *, writes_enabled: bool = False) -> dict:
    if not isinstance(req, dict):
        raise BridgeError("Заявка должна быть JSON-объектом")
    rid = ensure_request_id(req.get("request_id"))
    action = req.get("action")
    if action == "lookup":
        output = do_lookup(req, api)
    elif action == "preview_order":
        output = build_preview(req, api)
    elif action == "create_order":
        output = do_create(req, api, results_dir, writes_enabled=writes_enabled)
    elif action == "inspect_order_price":
        output = refresh_customer_order_line(req, api, writes_enabled=False, inspect_only=True)
    elif action == "refresh_order_price":
        output = refresh_customer_order_line(req, api, writes_enabled=writes_enabled)
    elif action == "refresh_from_confirmed_order":
        output = refresh_from_confirmed_order(req, api, results_dir, writes_enabled=writes_enabled)
    else:
        raise BridgeError("action: допустимы lookup, preview_order, create_order, inspect_order_price, refresh_order_price, refresh_from_confirmed_order")
    return {"request_id": rid, "action": action, **output}
