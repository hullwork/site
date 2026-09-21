"""The one way any Broker process brings the database schema up to date.

``db.migrate`` applies only the steps registered so far, and a step is registered when
the module that owns its tables is imported. The gateway used to migrate before it
built its routes, and the worker never imports the runtime modules at all, so the
ledger, approval and thread tables were never created and the first flow or agent
call failed with "relation does not exist". Importing every owning module here first
makes the schema independent of what the calling process happens to have imported.
"""
from __future__ import annotations

from sites.broker import db as broker_db

# Modules whose import registers migrations. runtime_api imports ledger, tools and
# agent_bridge, which are the three that register "runtime" steps.
_COMPONENT_MODULES = ("sites.broker.runtime_api",)


def migrate_all(db: broker_db.BrokerDatabase) -> None:
    import importlib

    for name in _COMPONENT_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            # A build without the runtime half still migrates its own tables.
            if exc.name != name:
                raise
    broker_db.migrate(db)
