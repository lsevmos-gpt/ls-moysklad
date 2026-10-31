"""Read-only validation of the three specified sales orders and supplier costs.

Source documents and their approved amendments are verified against live
MoySklad customer order positions, not inferred from filenames alone.
"""
from __future__ import annotations

import json
from decimal import Decimal

from .bridge import (
    BASE_URL, CRUSH_PRO_ORGANIZATION_ID, BridgeError, _linked_entity_id,
    _order_positions, _simple_line, ensure_uuid, fmt_rub
)

SUPPLIER_ID = "3f51ee71-6a00-11e7-7a6c-d2a9000b16f1"
SOURCES = [
    ("ЛС Героев", "create-heroes-order-20261021-v1", "apply-add-heroes-20261031-a1", "19950.00"),
    ("Сергей Севастополь", "create-sevastopol-isolate-20261031-v2", None, "54000.00"),
    ("ЛС Конная", "create-konnaya-new-20261031-a1", None, "10500.00"),
]
EXPECTED_STORE_ID = "0652dd44-3c3a-11e8-9109-f8fc0015f6b0"


def _read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BridgeError("Не удалось прочитать исходные документы: " + path.name) from None
    if not isinstance(data, dict):
        raise BridgeError("Документ должен быть объектом JSON: " + path.name)
    return data


def _price_minor(s):
    return int((Decimal(str(s)) * 100).to_integral_value())


def _lines_from_source(req):
    out = {}
    for row in req.get("positions", []):
        if not isinstance(row, dict):
            raise BridgeError("Некорректные строки заказа")
        typ = row.get("assortment_type")
        pid = ensure_uuid(row.get("assortment_id"), "assortment_id")
        key = (typ, pid)
        if key in out:
            raise BridgeError("Повторение товара в исходном заказе")
        out[key] = (Decimal(row["quantity"]), _price_minor(row["price_rub"]))
    return out


def _actual_order_lines(api, order_id):
    result = {}
    for row in _order_positions(api, order_id):
        typ, pid, qty, price, _ = _simple_line(row)
        key = (typ, pid)
        if key in result:
            raise BridgeError("Повторение товара в фактическом заказе")
        result[key] = (qty, price)
    return result


def _catalog_buy_price(obj):
    buy = obj.get("buyPrice")
    if not isinstance(buy, dict):
        return None
    value = buy.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0 or not float(value).is_integer():
        return None
    return int(value)


def _history_prices(api, max_orders=12):
    res = api.call("GET", "/entity/purchaseorder", params={"limit": 30, "order": "moment,desc"})
    rows = res.get("rows")
    if not isinstance(rows, list):
        raise BridgeError("Нет истории закупочных заказов")
    prices = {}
    docs = []
    for po in rows:
        if len(docs) >= max_orders:
            break
        if not isinstance(po, dict):
            continue
        try:
            supplier = _linked_entity_id(po.get("agent"), "counterparty")
            order_id = ensure_uuid(po.get("id"), "purchaseorder id")
        except BridgeError:
            continue
        if supplier != SUPPLIER_ID:
            continue
        data = api.call("GET", f"/entity/purchaseorder/{order_id}/positions", params={"limit": 100})
        lines = data.get("rows")
        if not isinstance(lines, list):
            continue
        size = data.get("meta", {}).get("size") if isinstance(data.get("meta"), dict) else None
        if isinstance(size, int) and size != len(lines):
            continue
        for line in lines:
            try:
                typ, pid, qty, price, _posid = _simple_line(line)
            except BridgeError:
                continue
            key = (typ, pid)
            if key not in prices and price > 0:
                prices[key] = {"minor": price, "order_name": po.get("name"), "moment": po.get("moment")}
        docs.append({"name": po.get("name"), "id": order_id})
    return prices, docs


def inspect_consolidated_supply(req, api, results_dir):
    if req.get("kind") != "purchaseorder":
        raise BridgeError("Разрешено только объединение для заказа поставщику")
    entries = {}
    snapshots = []
    for label, src_id, amend_id, expected_total in SOURCES:
        orig = _read_json(results_dir.parent / "requests" / (src_id + ".json"))
        record = _read_json(results_dir / (src_id + ".json"))
        if orig.get("action") != "create_order" or orig.get("kind") != "customerorder" or orig.get("confirmed") is not True:
            raise BridgeError("Нет подтвержденного исходного заказа: " + label)
        if record.get("request_id") != src_id or record.get("status") not in ("created", "already_exists"):
            raise BridgeError("Не подтверждено создание заказа: " + label)
        if orig.get("store_id") != EXPECTED_STORE_ID:
            raise BridgeError("Неверный склад заказа: " + label)
        order_id = ensure_uuid(record.get("document_id"), "document_id")
        if record.get("externalCode") != "CPAI-" + src_id:
            raise BridgeError("Исходный заказ имеет неверный внешний код")
        doc = api.call("GET", f"/entity/customerorder/{order_id}")
        if doc.get("id") != order_id or doc.get("name") != record.get("document_name"):
            raise BridgeError("Номер заказа изменился: " + label)
        if doc.get("externalCode") != record.get("externalCode"):
            raise BridgeError("Внешний код заказа изменился: " + label)
        if _linked_entity_id(doc.get("agent"), "counterparty") != orig.get("agent_id"):
            raise BridgeError("Изменился покупатель в заказе: " + label)
        if _linked_entity_id(doc.get("organization"), "organization") != CRUSH_PRO_ORGANIZATION_ID:
            raise BridgeError("Изменилась организация: " + label)
        expected = _lines_from_source(orig)
        if amend_id:
            amend = _read_json(results_dir.parent / "requests" / (amend_id + ".json"))
            status = _read_json(results_dir / (amend_id + ".json"))
            if amend.get("action") != "apply_order_additions" or status.get("status") not in ("updated", "already_updated"):
                raise BridgeError("Нет подтвержденного дополнения для Героев")
            if status.get("order_id") != order_id or status.get("new_total_rub") != expected_total:
                raise BridgeError("Сумма дополнения для Героев не совпадает")
            for row in amend["additions"]:
                key = (row["assortment_type"], ensure_uuid(row["assortment_id"], "id"))
                qty, price = Decimal(row["quantity"]), _price_minor(row["price_rub"])
                if key in expected:
                    old_qty, old_price = expected[key]
                    if price != old_price:
                        raise BridgeError("Цена изменяемой позиции не совпадает")
                    expected[key] = (old_qty + qty, price)
                else:
                    expected[key] = (qty, price)
        live = _actual_order_lines(api, order_id)
        changed = []
        if live != expected:
            for key in sorted(set(live) | set(expected)):
                before = expected.get(key)
                after = live.get(key)
                if before != after:
                    changed.append({
                        "assortment_type": key[0], "assortment_id": key[1],
                        "expected_quantity": str(before[0]) if before else None,
                        "actual_quantity": str(after[0]) if after else None,
                        "expected_price_rub": fmt_rub(before[1]) if before else None,
                        "actual_price_rub": fmt_rub(after[1]) if after else None,
                    })
        actual_minor = sum((qty * price for qty, price in live.values()), Decimal(0))
        if (not isinstance(doc.get("sum"), (int, float))
            or isinstance(doc.get("sum"), bool)
            or doc.get("sum") != actual_minor):
            raise BridgeError("Сумма текущего заказа не соответствует позициям: " + label)
        for key, (qty, _sale_price) in live.items():
            by_source = entries.setdefault(key, {"quantity": Decimal(0), "sources": {}})
            by_source["quantity"] += qty
            by_source["sources"][label] = str(qty)
        snapshots.append({"label": label, "order_name": doc.get("name"),
                          "total_rub": fmt_rub(actual_minor),
                          "original_total_rub": expected_total,
                          "positions": len(live),
                          "units": str(sum((v[0] for v in live.values()), Decimal(0))),
                          "changes_from_original": changed})
    history_prices, history_docs = _history_prices(api)
    rows = []
    for (typ, pid), data in entries.items():
        item = api.get_entity(typ, pid)
        name = item.get("name")
        direct = _catalog_buy_price(item)
        parent_cost = None
        if typ == "variant":
            try:
                parent_id = _linked_entity_id(item.get("product"), "product")
                parent = api.get_entity("product", parent_id)
                parent_cost = _catalog_buy_price(parent)
            except BridgeError:
                pass
        history = history_prices.get((typ, pid))
        rows.append({
            "assortment_type": typ, "assortment_id": pid, "name": name,
            "quantity": str(data["quantity"]), "by_source": data["sources"],
            "catalog_buy_rub": fmt_rub(direct) if direct is not None else None,
            "parent_buy_rub": fmt_rub(parent_cost) if parent_cost is not None else None,
            "last_purchase_rub": fmt_rub(history["minor"]) if history else None,
            "last_purchase_order": history["order_name"] if history else None
        })
    rows.sort(key=lambda row: (row["assortment_type"], row["name"] or ""))
    qty = sum((int(Decimal(r["quantity"])) for r in rows), 0)
    changed_sources = [x["label"] for x in snapshots if x["changes_from_original"]]
    return {
        "status": "ok", "supplier": {"id": SUPPLIER_ID, "name": 'ООО "КРАШ КОМПАНИ"'},
        "source_orders": snapshots, "rows": rows, "total_quantity": qty,
        "unique_positions": len(rows), "changed_sources": changed_sources,
        "purchase_history_checked": history_docs,
        "missing_requested_item": "Изотоник Вишня 1 шт. отсутствует в каталоге и заказе Конной.",
        "note": "Проверено с реальными позициями и суммами трех текущих заказов. Текущие цены закупки и прошлые цены показаны раздельно. Никакой документ не создан."
    }
