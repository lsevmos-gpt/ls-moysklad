"""GitHub Actions entrypoint. Results are committed separately, never secrets."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .bridge import BridgeError, MoySkladAPI, execute, ensure_request_id

ROOT = Path(__file__).resolve().parents[1]
REQUESTS = ROOT / "requests"
RESULTS = ROOT / "results"


def main() -> int:
    REQUESTS.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    for file in sorted(REQUESTS.glob("*.json")):
        output_file = RESULTS / file.name
        if output_file.exists():
            try:
                old = json.loads(output_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                old = {}
            if old.get("status") in {"ok", "created", "already_exists"}:
                print(f"{file.name}: already processed")
                continue
        try:
            obj = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(obj, dict):
                raise BridgeError("Заявка должна быть объектом")
            rid = ensure_request_id(obj.get("request_id"))
            if file.stem != rid:
                raise BridgeError("Имя файла и request_id должны совпадать")
            api = MoySkladAPI(os.environ.get("MOYSKLAD_TOKEN", ""))
            result = execute(obj, api, RESULTS, writes_enabled=os.environ.get("MOYSKLAD_WRITE_ENABLED") == "YES")
        except (BridgeError, ValueError, OSError) as exc:
            result = {"request_id": file.stem, "status": "error", "message": str(exc)}
        output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"{file.name}: {result.get('status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
