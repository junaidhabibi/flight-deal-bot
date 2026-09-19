"""Config loading, validation and convenient lookups."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import yaml

if TYPE_CHECKING:
    from .layovers import LayoverRules


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, data: Dict[str, Any], path: Optional[Path] = None):
        self._d = data
        self.path = path
        self._validate()

    # ---------- loading ----------

    @classmethod
    def load(cls, path: str | Path = "config.yml") -> "Config":
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"Config file not found: {p.resolve()}")
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise ConfigError(f"Config file {p} did not parse to a mapping.")
        return cls(data, p)

    def _validate(self) -> None:
        required = ["origins", "destinations", "trip", "thresholds", "email"]
        missing = [k for k in required if k not in self._d]
        if missing:
            raise ConfigError(f"Config is missing required sections: {missing}")

        if not self.origins:
            raise ConfigError("At least one origin is required.")
        if not self.destinations:
            raise ConfigError("At least one destination is required.")

        # Validated by building the rules object, which enforces that the
        # dead zone actually exists.
        try:
            self.layover_rules
        except ValueError as e:
            raise ConfigError(f"trip.layover is invalid: {e}") from e

        t = self.thresholds
        tiers = t.get("tiers", {})
        for name in ["watch", "good", "great", "insane"]:
            if name not in tiers:
                raise ConfigError(f"thresholds.tiers.{name} is required")
        ordered = [tiers["watch"], tiers["good"], tiers["great"], tiers["insane"]]
        if ordered != sorted(ordered):
            raise ConfigError(
                "thresholds.tiers must increase: watch <= good <= great <= insane"
            )

    # ---------- raw access ----------

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._d
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    # ---------- typed properties ----------

    @property
    def origins(self) -> List[Dict[str, Any]]:
        return self._d.get("origins", [])

    @property
    def origin_codes(self) -> List[str]:
        return [o["code"] for o in self.origins]

    @property
    def destinations(self) -> List[Dict[str, Any]]:
        return self._d.get("destinations", [])

    @property
    def destination_codes(self) -> List[str]:
        return [d["code"] for d in self.destinations]

    @property
    def stopover_hubs(self) -> List[Dict[str, Any]]:
        return self._d.get("stopover_hubs", [])

    @property
    def trip(self) -> Dict[str, Any]:
        return self._d["trip"]

    @property
    def layover(self) -> Dict[str, Any]:
        """The layover section, accepting the older layover_hours shape too."""
        if "layover" in self.trip:
            return self.trip["layover"]
        legacy = self.trip.get("layover_hours", {})
        return {
            "quick_max_hours": 0.001,   # legacy config had no quick band
            "overnight_min_hours": legacy.get("min", 24),
            "overnight_max_hours": legacy.get("max", 120),
        }

    @property
    def layover_rules(self) -> "LayoverRules":
        from .layovers import LayoverRules

        lay = self.layover
        return LayoverRules(
            quick_max_hours=float(lay.get("quick_max_hours", 5)),
            overnight_min_hours=float(lay.get("overnight_min_hours", 14)),
            overnight_max_hours=float(lay.get("overnight_max_hours", 120)),
            require_night_below_hours=float(
                lay.get("require_night_below_hours", 24)
            ),
            night_core_start=int(lay.get("night_core_start", 1)),
            night_core_end=int(lay.get("night_core_end", 6)),
        )

    @property
    def layover_hours(self) -> Dict[str, float]:
        """Back-compat shim: the overnight band as min/max."""
        r = self.layover_rules
        return {"min": r.overnight_min_hours, "max": r.overnight_max_hours}

    @property
    def thresholds(self) -> Dict[str, Any]:
        return self._d["thresholds"]

    @property
    def alerts(self) -> Dict[str, Any]:
        return self._d.get("alerts", {})

    @property
    def sources(self) -> Dict[str, Any]:
        return self._d.get("sources", {})

    @property
    def email(self) -> Dict[str, Any]:
        return self._d["email"]

    @property
    def scan(self) -> Dict[str, Any]:
        return self._d.get("scan", {})

    @property
    def carry_on(self) -> Dict[str, Any]:
        return self._d.get("carry_on", {})

    @property
    def db_path(self) -> str:
        return self.get("storage", "db_path", default="data/prices.db")

    @property
    def currency(self) -> str:
        return self.trip.get("currency", "USD")

    # ---------- lookups ----------

    def destination(self, code: str) -> Optional[Dict[str, Any]]:
        for d in self.destinations:
            if d["code"] == code:
                return d
        return None

    def origin(self, code: str) -> Optional[Dict[str, Any]]:
        for o in self.origins:
            if o["code"] == code:
                return o
        return None

    def hub(self, code: str) -> Optional[Dict[str, Any]]:
        for h in self.stopover_hubs:
            if h["code"] == code:
                return h
        return None

    def baseline(self, dest_code: str) -> Optional[float]:
        d = self.destination(dest_code)
        return float(d["baseline_usd"]) if d and "baseline_usd" in d else None

    def priority(self, dest_code: str) -> float:
        d = self.destination(dest_code)
        return float(d.get("priority", 1.0)) if d else 1.0

    def origin_weight(self, origin_code: str) -> float:
        o = self.origin(origin_code)
        return float(o.get("weight", 1.0)) if o else 1.0

    def hub_appeal(self, hub_code: str) -> float:
        h = self.hub(hub_code)
        return float(h.get("appeal", 1.0)) if h else 1.0

    def city_name(self, code: str) -> str:
        d = self.destination(code)
        if d:
            return d.get("city", code)
        h = self.hub(code)
        if h:
            return h.get("city", code)
        o = self.origin(code)
        if o:
            return o.get("name", code)
        return code


# ---------- secrets ----------

_ENV_FILE_LOADED = False


def load_env_file(path: str | Path = ".env") -> int:
    """Load KEY=value lines from a .env file into the environment.

    Exists so you never have to remember `export` incantations. Shell
    exports vanish the moment you close Terminal, and one stray comma
    silently sets a variable to the wrong value -- which produces failures
    much later and far from the cause.

    Real environment variables always win, so GitHub Actions secrets are
    never overridden by a stray local file. No dependency needed: the
    format is too simple to justify one.
    """
    global _ENV_FILE_LOADED
    p = Path(path)
    if not p.exists():
        return 0

    loaded = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Clean up copy-paste damage, in this order: a trailing comma comes
        # OUTSIDE the quotes (`KEY="abc",`), so it has to go first or the
        # quote-stripping below won't see a matching pair.
        value = value.rstrip(",").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        value = value.rstrip(",").strip()
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1

    _ENV_FILE_LOADED = True
    return loaded


def env(name: str, required: bool = False, default: Optional[str] = None) -> Optional[str]:
    """Read a secret from the environment, or from a local .env file.

    Secrets never live in config.yml so the file is safe to commit.
    """
    if not _ENV_FILE_LOADED:
        load_env_file()
    val = os.environ.get(name, default)
    if required and not val:
        raise ConfigError(
            f"Environment variable {name} is required but not set. "
            f"See the README for which secrets to add."
        )
    return val
