"""Small deterministic card and clock fakes."""

import copy
import json
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, Optional


def request_key(request: Dict[str, Any]) -> str:
    return json.dumps(request, sort_keys=True, separators=(",", ":"))


class ScriptedClient:
    def __init__(
        self,
        scripted: Optional[Dict[str, Iterable[Dict[str, Any]]]] = None,
        defaults: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.device = "/dev/cu.usbmodemNOTE1"
        self.scripted = defaultdict(deque)
        for key, values in (scripted or {}).items():
            self.scripted[key].extend(copy.deepcopy(list(values)))
        self.defaults = copy.deepcopy(defaults or {})
        self.requests = []

    def _response(self, request: Dict[str, Any]) -> Dict[str, Any]:
        self.requests.append(copy.deepcopy(request))
        key = request_key(request)
        if self.scripted[key]:
            return copy.deepcopy(self.scripted[key].popleft())
        name = request.get("req", request.get("cmd", ""))
        if name in self.defaults:
            return copy.deepcopy(self.defaults[name])
        return {}

    def inspect(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return self._response(request)

    def request(
        self, request: Dict[str, Any], raise_on_error: bool = True
    ) -> Dict[str, Any]:
        response = self._response(request)
        if raise_on_error and response.get("err"):
            raise RuntimeError(response["err"])
        return response

class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds
