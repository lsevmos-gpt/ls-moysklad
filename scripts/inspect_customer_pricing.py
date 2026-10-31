"""Read-only checks for the two catalog products in a specific existing customer's past orders."""
from __future__ import annotations

from .bridge import BASE_URL, BridgeError, _linked_entity_id, _order_positions, _simple_line, fmt_rub

CUSTOMER_ID = "903facc5-fd6e-11ea-0a80-00ab0003ccb2"
CREATINE_ID = "9ec2009a-b38a-11ea-0a80-010d000866d6"
ISOLATE_COOKIE_ID = "9d91b347-e23f-11ef-0a80-08b50035d181"

def inspect_customer_pricing(req, api):
    customer = api.get_entity("counterparty", CUSTOMER_ID)
    data = api.call("GET", "/entity/customerorder", params={
        "limit": 30,
        "order": "moment,desc",
        "filter": "agent=" + BASE_URL + "/entity/counterparty/" + CUSTOMER_ID,
    })
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise BridgeError("Не удалось прочитать историю заказов покупателя")
    docs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            if _linked_entity_id(row.get("agent"), "counterparty") != CUSTOMER_ID:
                continue
        except BridgeError:
            continue
        doc_id = row.get("id")
        relevant = []
        pos = _order_positions(api, doc_id)
        for line in pos:
            typ, prod, qty, price, _ = _simple_line(line)
            if prod in (CREATINE_ID, ISOLATE_COOKIE_ID):
                relevant.append({"item_id": prod, "qty": str(qty), "price_rub": fmt_rub(price)})
        docs.append({"number": row.get("name"), "date": row.get("moment"), "relevant_products": relevant})
        if len(docs) >= 12:
            break
    return {
       "status":"ok",
       "buyer":{"id":CUSTOMER_ID,"name":customer.get("name")},
       "customer_price_type": (customer.get("priceType") or {}).get("name") if isinstance(customer.get("priceType"),dict) else None,
       "recent_order_history":docs,
       "message":"Read-only inspection; no orders created."
    }
