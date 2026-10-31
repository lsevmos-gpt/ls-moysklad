"""Check three live customer orders against the requested Konnaya list and purchase #00031.

Read-only. Preserves exact assortment UUIDs and checks quantities, prices, totals,
buyer/supplier and the parent organisation. Nothing is changed by this action.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from .bridge import (
    BASE_URL, CRUSH_PRO_ORGANIZATION_ID, BridgeError,
    _linked_entity_id, _order_positions, _simple_line,
    ensure_uuid, fmt_rub,
)

EXPECTED_IDS = {
    "heroes": ("eab549d9-cd50-11f1-0a80-03be007edc83", "00078", "c27d35f7-b675-11ec-0a80-092f00101c99"),
    "sergey": ("66393a40-d50f-11f1-0a80-10b60064aee0", "00079", "6523171d-2557-11ee-0a80-07f8000cd58d"),
    "konnaya": ("828f653a-d515-11f1-0a80-197c00664bed", "00080", "ba5b8eb5-d7e0-11ef-0a80-008d004045c0"),
}
SUPPLIER = ("fdda7205-d517-11f1-0a80-197c00673f81", "00031", "3f51ee71-6a00-11e7-7a6c-d2a9000b16f1")
STORE_ID = "0652dd44-3c3a-11e8-9109-f8fc0015f6b0"
RASPBERRY = ("variant", "3119b90a-b38d-11ea-0a80-010d00088608")
CHERRY = ("variant", "0887cbad-d517-11f1-0a80-1d670026da7c")

KONNAYA_TARGET = {
    ("product", "ae866e0b-6abf-11e7-7a69-9711000a2cae"): (2, 90000),
    ("product", "b5d259e4-9f9f-11ed-0a80-00aa0024bc2e"): (2, 150000),
    ("variant", "c8b1a01f-6abc-11e7-7a6c-d2a900142eea"): (1, 75000),
    ("variant", "32169bbb-7f01-11ee-0a80-13c60013abcc"): (1, 75000),
    ("variant", "ce15f055-7acb-11ec-0a80-02c8001144f1"): (1, 70000),
    RASPBERRY: (1, 110000),
    CHERRY: (1, 110000),
    ("product", "69d03c56-84ce-11ec-0a80-02f9001b756c"): (2, 120000),
}

def read_req(results_dir, name):
    try:
        p = results_dir.parent / "requests" / (name + ".json")
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request is not an object")
        return data
    except (OSError, ValueError):
        raise BridgeError("Не найден или поврежден исходный запрос " + name) from None

def positions(api, kind, oid):
    if kind == "customerorder":
        rows = _order_positions(api, oid)
    else:
        result = api.call("GET", "/entity/purchaseorder/" + oid + "/positions", params={"limit": 100})
        rows = result.get("rows")
        if not isinstance(rows, list):
            raise BridgeError("Поставщик: неизвестный формат строк заказа")
        size = result.get("meta", {}).get("size") if isinstance(result.get("meta"), dict) else None
        if isinstance(size, int) and size != len(rows):
            raise BridgeError("Поставщик: неполный список позиций")
    actual = {}
    for row in rows:
        typ, id_, qty, price, posid = _simple_line(row)
        key = (typ, id_)
        if key in actual:
            raise BridgeError("Повторение товара в заказе " + oid)
        actual[key] = (qty, price, posid)
    return actual

def description_diff(expected, actual):
    rows = []
    for key in sorted(set(expected) | set(actual)):
        target = expected.get(key)
        present = actual.get(key)
        pair = (present[0], present[1]) if present else None
        if target != pair:
            rows.append({
                "assortment_type": key[0], "assortment_id": key[1],
                "expected_quantity": str(target[0]) if target else None,
                "actual_quantity": str(present[0]) if present else None,
                "expected_price_rub": fmt_rub(target[1]) if target else None,
                "actual_price_rub": fmt_rub(present[1]) if present else None,
            })
    return rows

def source_expected(results_dir, name):
    r = read_req(results_dir, name)
    expected = {}
    for p in r.get("positions", []):
        key = (p["assortment_type"], ensure_uuid(p["assortment_id"], "assortment_id"))
        expected[key] = (Decimal(p["quantity"]), int(Decimal(p["price_rub"]) * 100))
    return expected

def validate_doc(api, kind, id_, name, agent):
    d = api.call("GET", "/entity/" + kind + "/" + id_)
    if d.get("id") != id_ or d.get("name") != name:
        raise BridgeError("Документ " + name + ": неверный id или номер")
    if _linked_entity_id(d.get("agent"), "counterparty") != agent:
        raise BridgeError("Документ " + name + ": неверный контрагент")
    if _linked_entity_id(d.get("organization"), "organization") != CRUSH_PRO_ORGANIZATION_ID:
        raise BridgeError("Документ " + name + ": неверная организация")
    if _linked_entity_id(d.get("store"), "store") != STORE_ID:
        raise BridgeError("Документ " + name + ": неверный склад")
    return d

def audit_three_orders(req, api, results_dir):
    actual = {}
    docs = {}
    for key, (oid, name, agent) in EXPECTED_IDS.items():
        doc = validate_doc(api, "customerorder", oid, name, agent)
        lines = positions(api, "customerorder", oid)
        total = sum((qty * price for qty, price, _ in lines.values()), Decimal(0))
        if doc.get("sum") != total:
            raise BridgeError("Заказ " + name + ": не совпадает сумма по позициям")
        actual[key] = lines
        docs[key] = {
            "number": name, "amount_rub": fmt_rub(total),
            "line_count": len(lines), "units": str(sum((v[0] for v in lines.values()), Decimal(0))),
            "applicable": doc.get("applicable"),
        }

    expected_heroes = source_expected(results_dir, "create-heroes-order-20261021-v1")
    extras = read_req(results_dir, "apply-add-heroes-20261031-a1")["additions"]
    for p in extras:
        k = (p["assortment_type"], ensure_uuid(p["assortment_id"], "assortment_id"))
        q, price = Decimal(p["quantity"]), int(Decimal(p["price_rub"]) * 100)
        if k in expected_heroes:
            oldq, oldprice = expected_heroes[k]
            if oldprice != price:
                raise BridgeError("Неверная цена в дополнении Героев")
            expected_heroes[k] = (oldq + q, price)
        else:
            expected_heroes[k] = (q, price)
    expected_sergey = source_expected(results_dir, "create-sevastopol-isolate-20261031-v2")
    expected = {"heroes": expected_heroes, "sergey": expected_sergey, "konnaya": KONNAYA_TARGET}
    for key, lines in actual.items():
        docs[key]["matches_requested"] = len(description_diff(expected[key], lines)) == 0
        docs[key]["differences"] = description_diff(expected[key], lines)

    original_supplier = read_req(results_dir, "create-supplier-three-20261031-v1")
    expected_purchase = source_expected(results_dir, "create-supplier-three-20261031-v1")
    po = validate_doc(api, "purchaseorder", *SUPPLIER)
    supplier_lines = positions(api, "purchaseorder", SUPPLIER[0])
    supplier_total = sum((qty * price for qty, price, _ in supplier_lines.values()), Decimal(0))
    if po.get("sum") != supplier_total:
        raise BridgeError("Сумма заказа поставщику не совпадает с позициями")
    original_diff = description_diff(expected_purchase, supplier_lines)
    requirement_counts = {}
    for lines in expected.values():
        for k, (qty, price) in lines.items():
            requirement_counts[k] = requirement_counts.get(k, Decimal(0)) + qty
    purchase_counts = {k: qty for k, (qty, price, pos_id) in supplier_lines.items()}
    qty_diff = []
    for k in sorted(set(requirement_counts) | set(purchase_counts)):
        e, a = requirement_counts.get(k, Decimal(0)), purchase_counts.get(k, Decimal(0))
        if e != a:
            qty_diff.append({
                "assortment_type": k[0], "assortment_id": k[1],
                "expected_quantity": str(e), "supplier_quantity": str(a),
            })
    return {
        "status": "ok", "customer_orders": docs,
        "konnaya_expected_units": str(sum((p[0] for p in KONNAYA_TARGET.values()), 0)),
        "konnaya_expected_total_rub": fmt_rub(sum((q * p for q,p in KONNAYA_TARGET.values()), 0)),
        "supplier": {
            "number": SUPPLIER[1], "agent": 'ООО "КРАШ КОМПАНИ"',
            "amount_rub": fmt_rub(supplier_total), "line_count": len(supplier_lines),
            "units": str(sum((v[0] for v in supplier_lines.values()), Decimal(0))),
            "applicable": po.get("applicable"),
            "matches_original_purchase_order": len(original_diff) == 0,
            "original_order_differences": original_diff,
            "matches_three_requested_orders": len(qty_diff) == 0,
            "quantity_differences": qty_diff,
        },
        "note": "Проверены реальные записи 00078, 00079, 00080, 00031 в МойСклад; действие только для чтения."
    }
