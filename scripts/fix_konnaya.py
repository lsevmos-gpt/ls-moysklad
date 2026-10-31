"""Guarded, idempotent correction of the sole missing raspberry isotonic in Konnaia 00080.

Restores the full user-requested list and does not touch the supplier purchase order.
"""
from __future__ import annotations

import json
from decimal import Decimal

from .bridge import (
    BridgeError, _linked_entity_id, entity_meta, fmt_rub,
)
from .verify_three_orders import (
    CHERRY, RASPBERRY, KONNAYA_TARGET, EXPECTED_IDS, SUPPLIER,
    validate_doc, positions, description_diff, source_expected,
)

FIXED_PRICE_MINOR = 110000
PRICE_TIER = "Опт для залов"


def restore_konnaya_raspberry(req, api, results_dir, *, writes_enabled):
    if req.get("confirmed") is not True:
        raise BridgeError("Требуется подтверждение внесения исправления")
    if req.get("expected_order_number") != "00080":
        raise BridgeError("Неожиданный номер заказа")
    if req.get("approved_audit_id") != "audit-konnaya-supplier-20261031-v1":
        raise BridgeError("Нет утвержденной сверки заказа")
    audit_path = results_dir / "audit-konnaya-supplier-20261031-v1.json"
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BridgeError("Не удалось проверить сохраненный аудит") from None
    kon = audit.get("customer_orders", {}).get("konnaya", {}) if isinstance(audit, dict) else {}
    supp = audit.get("supplier", {}) if isinstance(audit, dict) else {}
    if (audit.get("status") != "ok" or kon.get("number") != "00080"
        or supp.get("number") != "00031"
        or supp.get("matches_three_requested_orders") is not True):
        raise BridgeError("Аудит источников и заказа поставщику не прошёл проверку")

    konnaia_doc = validate_doc(api, "customerorder", *EXPECTED_IDS["konnaya"])
    if konnaia_doc.get("applicable") is not False:
        raise BridgeError("Заказ Конной сейчас проведен; автоматическое исправление не разрешено")
    if konnaia_doc.get("shippedSum", 0) not in (0, None):
        raise BridgeError("Есть отгрузки, исправление не выполняется")
    live = positions(api, "customerorder", EXPECTED_IDS["konnaya"][0])
    actual_total = sum((qty * price for qty, price, _ in live.values()), Decimal(0))
    if konnaia_doc.get("sum") != actual_total:
        raise BridgeError("Сумма заказа Конной не соответствует строкам")
    missing_only = dict(KONNAYA_TARGET)
    del missing_only[RASPBERRY]
    missing_diff = description_diff(missing_only, live)
    target_diff = description_diff(KONNAYA_TARGET, live)
    if target_diff and missing_diff:
        raise BridgeError("Текущий заказ Конной отличается не только отсутствующим изотоником Малина")
    mode = "already_updated" if not target_diff else "ready"
    if mode == "ready" and (actual_total != 1050000 or len(live) != 7):
        raise BridgeError("Неподтвержденная сумма или число строк в заказе Конной")
    if mode == "already_updated" and (actual_total != 1160000 or len(live) != 8):
        raise BridgeError("Итоговые строки заказа Конной неконсистентны")

    original_po = source_expected(results_dir, "create-supplier-three-20261031-v1")
    po = validate_doc(api, "purchaseorder", *SUPPLIER)
    po_live = positions(api, "purchaseorder", SUPPLIER[0])
    if (description_diff(original_po, po_live) or po.get("sum") != 6295000):
        raise BridgeError("Заказ поставщику изменился; нужна новая сверка")
    if po.get("applicable") is not False:
        raise BridgeError("Заказ поставщику теперь проведён, корректировку нужно согласовать отдельно")

    product = api.get_entity("variant", RASPBERRY[1])
    if product.get("name") != "Изотоник витаминно - минеральный 900 г. (банка) (Малина)":
        raise BridgeError("Название модификации изотоника изменилось")
    tier = [
        x.get("value") for x in product.get("salePrices", [])
        if isinstance(x, dict) and isinstance(x.get("priceType"), dict)
        and x["priceType"].get("name") == PRICE_TIER
    ]
    if tier != [FIXED_PRICE_MINOR]:
        raise BridgeError("Цена изотоника Малина в каталоге изменилась; остановлено")

    base = {
        "order_number": "00080", "order_id": EXPECTED_IDS["konnaya"][0],
        "supplier_order_number": "00031",
        "item_added": "Изотоник витаминно - минеральный 900 г. (банка) (Малина)",
        "added_quantity": "1", "unit_price_rub": "1100.00",
        "original_total_rub": "10500.00",
        "new_total_rub": "11600.00",
    }
    if mode == "already_updated":
        return {"status": "already_updated", **base,
                "note": "Позиции полностью совпадают; повторного добавления нет."}
    if not writes_enabled:
        raise BridgeError("Запись в МойСклад отключена")

    payload = [{"id": v[2]} for v in live.values()]
    payload.append({
        "quantity": 1.0, "price": FIXED_PRICE_MINOR,
        "assortment": entity_meta("variant", RASPBERRY[1]),
    })
    api.call("PUT", "/entity/customerorder/" + EXPECTED_IDS["konnaya"][0], body={"positions": payload})
    after = validate_doc(api, "customerorder", *EXPECTED_IDS["konnaya"])
    live_after = positions(api, "customerorder", EXPECTED_IDS["konnaya"][0])
    if (description_diff(KONNAYA_TARGET, live_after)
        or after.get("sum") != 1160000 or after.get("applicable") is not False
        or len(live_after) != 8):
        raise BridgeError("Проверка после записи не прошла: проверьте заказ Конной вручную")
    po_after = validate_doc(api, "purchaseorder", *SUPPLIER)
    supp_after = positions(api, "purchaseorder", SUPPLIER[0])
    if description_diff(original_po, supp_after) or po_after.get("sum") != 6295000:
        raise BridgeError("Заказ поставщику изменился во время исправления; проверьте вручную")
    return {
        "status": "updated", **base, "positions_after": len(live_after),
        "units_after": "11", "supplier_unchanged": True,
        "note": "Добавлена ровно 1 Малина; Вишня и все остальные позиции сохранены. Заказ поставщику не менялся.",
    }
