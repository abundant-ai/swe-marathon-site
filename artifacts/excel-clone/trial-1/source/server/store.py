"""Workbook registry: in-memory cache + persistence under /app/data."""
from __future__ import annotations
import glob
import json
import os
import threading
from typing import Dict, List, Optional

from .workbook import Workbook, DATA_DIR


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self._cache: Dict[int, Workbook] = {}
        self._next_id: int = 1
        self._load_all()

    def _load_all(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        max_id = 0
        for path in glob.glob(os.path.join(DATA_DIR, "*.json")):
            base = os.path.basename(path)
            if not base.endswith(".json"): continue
            stem = base[:-5]
            try:
                wb_id = int(stem)
            except ValueError:
                continue
            try:
                with open(path) as f:
                    data = json.load(f)
                wb = Workbook.from_json(data)
                self._cache[wb.id] = wb
                if wb.id > max_id:
                    max_id = wb.id
            except Exception as e:
                print(f"[store] failed loading {path}: {e}")
        self._next_id = max_id + 1

    def list(self) -> List[dict]:
        with self.lock:
            return [self._summary(wb) for wb in self._cache.values()]

    def _summary(self, wb: Workbook) -> dict:
        return {"id": wb.id, "name": wb.name, "sheets": list(wb.sheet_order)}

    def create(self, name: str = "Untitled") -> Workbook:
        with self.lock:
            wb_id = self._next_id
            self._next_id += 1
            wb = Workbook(wb_id, name)
            wb.add_sheet("Sheet1")
            self._cache[wb_id] = wb
            wb.save()
            return wb

    def get(self, wb_id: int) -> Optional[Workbook]:
        return self._cache.get(wb_id)

    def delete(self, wb_id: int) -> bool:
        with self.lock:
            wb = self._cache.pop(wb_id, None)
            path = os.path.join(DATA_DIR, f"{wb_id}.json")
            if os.path.exists(path):
                os.remove(path)
            return wb is not None
