import yaml
import os
from pathlib import Path

# Lade .env Datei wenn vorhanden (lokal + Docker-Fallback)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "config.yaml"))

# Mapping: (config-section, config-key) → Umgebungsvariable
_ENV_OVERRIDES = {
    ("synology", "password"):  "SYNO_PASSWORD",
    ("ssh",       "password"):  "SSH_PASSWORD",
    ("paperless", "token"):     "PAPERLESS_TOKEN",
    ("portainer", "password"):  "PORTAINER_PASSWORD",
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    # Secrets aus Umgebungsvariablen überschreiben (haben Vorrang über config.yaml)
    for (section, key), env_var in _ENV_OVERRIDES.items():
        val = os.getenv(env_var)
        if val:
            cfg.setdefault(section, {})[key] = val

    return cfg


config = load_config()
