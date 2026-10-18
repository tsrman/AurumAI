"""Operator receipt observer. APP_DATABASE_URL must use lifecycle role credentials."""

import argparse
import asyncio
import logging

from temporalio.client import Client
from src.core.config import settings
from src.core.database import async_session, engine
from src.services.execution_closure import observe_pending_executions


async def run(once: bool) -> None:
    engine.echo = False
    engine.sync_engine.hide_parameters = True
    client = await Client.connect(settings.TEMPORAL_HOST, namespace=settings.TEMPORAL_NAMESPACE)
    try:
        while True:
            try:
                await observe_pending_executions(client, async_session)
            except Exception:
                logging.getLogger(__name__).warning("execution_observation_unavailable")
                if once:
                    raise SystemExit(1) from None
            if once:
                return
            await asyncio.sleep(60)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    asyncio.run(run(parser.parse_args().once))


if __name__ == "__main__":
    main()
