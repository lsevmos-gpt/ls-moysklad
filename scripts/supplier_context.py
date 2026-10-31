"""Read-only purchase-order history and supplier discovery.

Never returns customer contact information or account credentials.
"""
from __future__ import annotations

from .bridge import BridgeError, _linked_entity_id


def inspect_purchase_context(req, api):
    limit = req.get("limit", 25)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 40:
        raise BridgeError("limit должен быть от 1 до 40")
    data = api.call("GET", "/entity/purchaseorder", params={"limit": limit, "order": "moment,desc"})
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise BridgeError("МойСклад не вернул список заказов поставщику")
    suppliers = {}
    orders = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        agent = item.get("agent", {})
        try:
            agent_id = _linked_entity_id(agent, "counterparty")
        except BridgeError:
            continue
        if agent_id not in suppliers:
            found = api.get_entity("counterparty", agent_id)
            suppliers[agent_id] = {"id": agent_id, "name": found.get("name"),
                                   "supplier": found.get("supplier", None)}
        total = item.get("sum")
        orders.append({
            "document_id": item.get("id"),
            "document_name": item.get("name"),
            "moment": item.get("moment"),
            "agent_id": agent_id,
            "agent_name": suppliers[agent_id].get("name"),
            "applicable": item.get("applicable"),
            "sum_rub": str(total / 100) if isinstance(total, (int, float)) and not isinstance(total, bool) else None,
        })
    return {"status": "ok", "purchase_orders": orders, "suppliers": list(suppliers.values()),
            "returned": len(orders), "note": "История заказов поставщикам из подключённого МоегоСклада, только чтение."}
