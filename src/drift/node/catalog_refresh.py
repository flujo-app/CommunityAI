"""Periodic authenticated catalog refresh with idle-only node reconfiguration."""

import logging
import threading

from drift.model_catalog import SignedModelCatalog
from drift.node.catalog_bootstrap import CatalogBootstrapConfig, CatalogBootstrapInstaller


def load_configured_catalog(config):
    if config.catalog_path is None:
        return None
    bootstrap = CatalogBootstrapConfig.load(config.catalog_bootstrap_path)
    envelope = SignedModelCatalog.from_json(config.catalog_path.read_text(encoding="utf-8"))
    # Keep verified local fallback usable offline after catalog expiry. Community
    # selection separately requires current validity; refresh can renew the policy.
    return envelope.verify(bootstrap.trust_root, now=envelope.signed.issued_at_ms / 1000)


class CatalogRefreshService:
    def __init__(self, config, config_path, data_dir, manager, restart):
        self.config = config
        self.manager = manager
        self.restart = restart
        self.installer = CatalogBootstrapInstaller(
            CatalogBootstrapConfig.load(config.catalog_bootstrap_path),
            data_dir=data_dir,
            config_path=config_path,
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="drift-catalog-refresh", daemon=True)

    def start(self):
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)

    def _run(self):
        while not self._stop.wait(self.config.catalog_refresh_seconds):
            try:
                result = self.installer.refresh()
                if not result.created:
                    continue
                # Active generations own leases. Reconfiguration waits for all
                # leases and loads, then atomically closes admission before restart.
                while not self._stop.is_set():
                    if self.manager.begin_idle_restart():
                        self.restart()
                        return
                    self._stop.wait(1)
            except Exception:
                logging.getLogger(__name__).exception("Catalog refresh failed; retaining the active configuration")
