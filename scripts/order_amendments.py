"""Safe preview and single-request amendment of an existing customer order.

Requires the order's original confirmed request and its recorded creation result.
Never accepts a free-form document ID for amendment. Nothing is posted until
the exact order, original position set, live price tier and approval match.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from .bridge import (
    BASE_URL, CRUSH_PRO_ORGANIZATION_ID, BridgeError, MoySkladAPI,
    _linked_entity_id, _order_positions, _simple_line, entity_meta,
    ensure_request_id, ensure_uuid, fmt_rub, to_decimal, to_minor_rub,
)

ALLOWED_PRICE_TIER = "Опт для залов"


def _fingerprint(req: dict) -> str:
    body = {
        "source_order_request_id": req.get("source_order_request_id"),
        "expected_order_name": req.get("expected_order_name"),
        "expected_old_total_rub": req.get("expected_old_total_rub"),
        "price_type": req.get("price_type"),
        "additions": req.get("additions"),
    }
    packed = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _source_order(req: dict, results_dir: Path) -> tuple[dict, dict, str, int]:
    src_id = ensure_request_id(req.get("source_order_request_id"))
    req_path = results_dir.parent / "requests" / (src_id + ".json")
    res_path = results_dir / (src_id + ".json")
    if not req_path.is_file() or not res_path.is_file():
        raise BridgeError("Не найдены подтвержденная заявка и результат исходного заказа")
    try:
        original = json.loads(req_path.read_text(encoding="utf-8"))
        recorded = json.loads(res_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BridgeError("Невозможно прочитать документы исходного заказа") from None
    if not isinstance(original, dict) or not isinstance(recorded, dict):
        raise BridgeError("Исходные документы имеют неверный формат")
    if original.get("action") != "create_order" or original.get("kind") != "customerorder" or original.get("confirmed") is not True:
        raise BridgeError("Исходный документ не является подтвержденным заказом покупателя")
    if recorded.get("request_id") != src_id or recorded.get("status") not in {"created", "already_exists"}:
        raise BridgeError("Не подтверждено создание исходного заказа")
    order_id = ensure_uuid(recorded.get("document_id"), "document_id")
    if recorded.get("document_name") != req.get("expected_order_name"):
        raise BridgeError("Номер исходного заказа не совпадает с подтвержденным")
    if recorded.get("externalCode") != "CPAI-" + src_id:
        raise BridgeError("Исходный заказ имеет неожиданный externalCode")
    old_total = to_minor_rub(req.get("expected_old_total_rub"))
    if old_total != to_minor_rub(recorded.get("total_rub")):
        raise BridgeError("Исходная сумма заказа изменилась")
    return original, recorded, order_id, old_total


def _original_lines(source: dict) -> dict:
    lines = source.get("positions")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 100:
        raise BridgeError("Состав исходного заказа недоступен")
    out = {}
    for p in lines:
        if not isinstance(p, dict) or p.get("assortment_type") not in {"variant", "product"}:
            raise BridgeError("Неизвестная исходная позиция заказа")
        key = (p["assortment_type"], ensure_uuid(p.get("assortment_id"), "assortment_id"))
        if key in out:
            raise BridgeError("В исходном заказе повторяющиеся товары; безопасное обновление невозможно")
        qty = to_decimal(p.get("quantity"), "quantity")
        if qty <= 0:
            raise BridgeError("Неверное исходное количество")
        out[key] = (qty, to_minor_rub(p.get("price_rub")))
    return out


def _read_actual(api: MoySkladAPI, order_id: str) -> dict:
    rows = _order_positions(api, order_id)
    out = {}
    for row in rows:
        kind, item_id, qty, price, position_id = _simple_line(row)
        key = (kind, item_id)
        if key in out:
            raise BridgeError("В заказе уже есть дублированные строки; обновление остановлено")
        out[key] = (qty, price, position_id)
    return out


def _additions(req: dict, api: MoySkladAPI, baseline: dict) -> tuple[dict, dict, int]:
    if req.get("price_type") != ALLOWED_PRICE_TIER:
        raise BridgeError("Допускается только ценовой тип 'Опт для залов'")
    rows = req.get("additions")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10:
        raise BridgeError("Дополнения должны включать от 1 до 10 позиций")
    adds, names, extra = {}, {}, Decimal(0)
    for p in rows:
        if not isinstance(p, dict) or p.get("assortment_type") not in {"variant", "product"}:
            raise BridgeError("Недопустимый тип добавляемого товара")
        kind = p["assortment_type"]
        pid = ensure_uuid(p.get("assortment_id"), "assortment_id")
        key = (kind, pid)
        if key in adds:
            raise BridgeError("Повтор одной позиции в дополнениях")
        qty = to_decimal(p.get("quantity"), "quantity")
        if qty <= 0 or qty > 10000 or qty.as_tuple().exponent < -3:
            raise BridgeError("Неправильное добавляемое количество")
        price = to_minor_rub(p.get("price_rub"))
        if price <= 0:
            raise BridgeError("Нулевая цена в дополнении запрещена")
        item = api.get_entity(kind, pid)
        tier = [
            val.get("value")
            for val in item.get("salePrices", [])
            if isinstance(val, dict)
            and isinstance(val.get("priceType"), dict)
            and val["priceType"].get("name") == ALLOWED_PRICE_TIER
        ]
        if len(tier) != 1 or tier[0] != price:
            raise BridgeError("Цена каталога 'Опт для залов' не совпала для товара: " + str(item.get("name", "")))
        adds[key] = (qty, price)
        names[key] = str(item.get("name", "Товар"))
        extra += qty * price
        if key in baseline and baseline[key][1] != price:
            raise BridgeError("Старая и новая цены существующей позиции не совпали")
    if extra > Decimal(10000000000):
        raise BridgeError("Сумма дополнения превышает ограничение")
    return adds, names, int(extra)


def _latest_order(api: MoySkladAPI, agent_id: str, expected_id: str) -> None:
    agent_meta = BASE_URL + "/entity/counterparty/" + agent_id
    listing = api.call("GET", "/entity/customerorder", params={
        "filter": "agent=" + agent_meta,
        "order": "moment,desc",
        "limit": 100,
    })
    rows = listing.get("rows")
    if not isinstance(rows, list) or not rows:
        raise BridgeError("Не удалось определить последний заказ данного покупателя")
    ids = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            if _linked_entity_id(item.get("agent"), "counterparty") == agent_id:
                ids.append(ensure_uuid(item.get("id"), "id"))
        except BridgeError:
            continue
    if not ids or ids[0] != expected_id:
        raise BridgeError("Обнаружен другой последний заказ покупателя. Обновление отменено")


def _check_state(
    req: dict, api: MoySkladAPI, results_dir: Path
) -> tuple[str, dict, dict, dict, dict, int, int, dict, dict, str]:
    source, recorded, order_id, old_total = _source_order(req, results_dir)
    baseline = _original_lines(source)
    if source.get("agent_id") != "c27d35f7-b675-11ec-0a80-092f00101c99":
        raise BridgeError("Дополнения этой операции доступны только для покупателя ЛС Героев")
    if source.get("store_id") != "0652dd44-3c3a-11e8-9109-f8fc0015f6b0":
        raise BridgeError("Неверный склад исходного заказа")
    if sum((qty * price for qty, price in baseline.values()), Decimal(0)) != old_total:
        raise BridgeError("Исходный расчет позиций не совпадает с суммой")
    _latest_order(api, source["agent_id"], order_id)
    doc = api.call("GET", "/entity/customerorder/" + order_id)
    if doc.get("id") != order_id:
        raise BridgeError("Идентификатор исходного заказа не совпадает")
    if doc.get("name") != recorded["document_name"]:
        raise BridgeError("Номер заказа в МойСклад изменился; запись отменена")
    if doc.get("externalCode") != recorded["externalCode"]:
        raise BridgeError("Внешний код заказа в МойСклад изменился; запись отменена")
    if type(doc.get("applicable")) is not bool:
        raise BridgeError("МойСклад не вернул однозначное поле applicable; обновление заблокировано")
    if doc.get("shippedSum", 0) not in (None, 0):
        raise BridgeError("По заказу уже есть отгрузка; дополнение остановлено для проверки вручную")
    recorded = dict(recorded)
    recorded["applicable_before"] = doc["applicable"]
    if _linked_entity_id(doc.get("agent"), "counterparty") != source["agent_id"]:
        raise BridgeError("У заказа другой покупатель")
    if _linked_entity_id(doc.get("organization"), "organization") != CRUSH_PRO_ORGANIZATION_ID:
        raise BridgeError("У заказа другая организация")
    if _linked_entity_id(doc.get("store"), "store") != source["store_id"]:
        raise BridgeError("У заказа другой склад")
    actual = _read_actual(api, order_id)
    adds, names, extra = _additions(req, api, baseline)
    target = dict(baseline)
    for key, (qty, price) in adds.items():
        if key in target:
            target[key] = (target[key][0] + qty, price)
        else:
            target[key] = (qty, price)
    new_total = old_total + extra
    current_total = doc.get("sum")
    if isinstance(current_total, bool) or not isinstance(current_total, (int, float)) or not float(current_total).is_integer():
        raise BridgeError("Не удалось проверить сумму заказа")
    live = {key: (p[0], p[1]) for key, p in actual.items()}
    if live == baseline and int(current_total) == old_total:
        mode = "ready"
    elif live == target and int(current_total) == new_total:
        mode = "already_updated"
    else:
        raise BridgeError("Состав или сумма текущего заказа отличается от согласованного исходного состояния")
    return mode, source, recorded, baseline, actual, old_total, new_total, adds, names, order_id


def handle_order_amendment(req: dict, api: MoySkladAPI, results_dir: Path, *, writes_enabled: bool) -> dict:
    mode, source, recorded, baseline, actual, old_total, new_total, adds, names, order_id = _check_state(req, api, results_dir)
    is_apply = req.get("action") == "apply_order_additions"
    common = {
        "order_name": recorded["document_name"],
        "order_id": order_id,
        "document_url": recorded.get("document_url"),
        "customer": "ЛС Героев",
        "was_posted": recorded["applicable_before"],
        "old_total_rub": fmt_rub(old_total),
        "added_total_rub": fmt_rub(new_total - old_total),
        "new_total_rub": fmt_rub(new_total),
        "additions": [
            {"name": names[key], "quantity": str(qty), "price_rub": fmt_rub(price)}
            for key, (qty, price) in adds.items()
        ],
    }
    if not is_apply:
        return {
            "status": "ok", "mode": mode, **common,
            "preview_sha256": _fingerprint(req),
            "note": "Только проверка. Заказ не изменен.",
        }
    if req.get("confirmed") is not True:
        raise BridgeError("Для изменения заказа требуется явное подтверждение")
    preview_id = ensure_request_id(req.get("approved_preview_id"))
    preview_file = results_dir / (preview_id + ".json")
    if not preview_file.is_file():
        raise BridgeError("Предварительная проверка заказа не найдена")
    try:
        preview = json.loads(preview_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise BridgeError("Файл предварительной проверки поврежден") from None
    if not isinstance(preview, dict) or preview.get("action") != "inspect_order_additions" or preview.get("status") != "ok" or preview.get("request_id") != preview_id or preview.get("preview_sha256") != _fingerprint(req):
        raise BridgeError("Состав дополнений изменен после проверки; обновление запрещено")
    if mode == "already_updated":
        return {"status": "already_updated", **common, "note": "Дополнения уже присутствуют, повторно не добавлены."}
    if not writes_enabled:
        raise BridgeError("Запись в МойСклад отключена")
    payload = []
    for key, (_qty, _price) in baseline.items():
        position = {"id": actual[key][2]}
        if key in adds:
            position["quantity"] = float(baseline[key][0] + adds[key][0])
        payload.append(position)
    for key, (qty, price) in adds.items():
        if key not in baseline:
            kind, pid = key
            payload.append({
                "quantity": float(qty),
                "price": price,
                "assortment": entity_meta(kind, pid),
            })
    # One PUT for all the changes, keeping existing rows by ID.
    # No automatic retry: if the response is ambiguous, the next run compares
    # current state to the target instead of blindly resending the update.
    api.call("PUT", "/entity/customerorder/" + order_id, body={"positions": payload})
    verified = api.call("GET", "/entity/customerorder/" + order_id)
    rows = _read_actual(api, order_id)
    target = dict(baseline)
    for key, (qty, price) in adds.items():
        if key in target:
            target[key] = (target[key][0] + qty, price)
        else:
            target[key] = (qty, price)
    live = {key: (p[0], p[1]) for key, p in rows.items()}
    if (live != target or verified.get("sum") != new_total
        or verified.get("applicable") is not recorded["applicable_before"]
        or verified.get("externalCode") != recorded["externalCode"]):
        raise BridgeError("Ответ проверки после изменения не совпал; проверьте заказ вручную")
    return {
        "status": "updated", **common, "positions_before": len(baseline),
        "positions_after": len(live),
        "note": "Позиции изменены в существующем заказе с сохранением признака проведения. Оплата и отгрузка не выполнялись.",
    }
