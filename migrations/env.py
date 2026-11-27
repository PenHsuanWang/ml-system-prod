from alembic import context
from sqlalchemy import create_engine, pool

from bootstrap.ml_config import MLSettings
from src.infrastructure.ingestion.persistence.schema import metadata
from src.infrastructure.persistence.dataset_build_schema import dataset_build_jobs
from src.infrastructure.persistence.dataset_lineage_schema import dataset_lineage
from src.infrastructure.persistence.prediction_contract_schema import public_contracts

assert public_contracts.metadata is metadata
assert dataset_lineage.metadata is metadata
assert dataset_build_jobs.metadata is metadata

config = context.config
url = config.attributes.get("database_url") or MLSettings.from_env().database_url


def run() -> None:
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(url, poolclass=pool.NullPool)
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=metadata)
            with context.begin_transaction():
                context.run_migrations()
        engine.dispose()


run()
