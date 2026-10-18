"""python -m src.cli.maintain_partitions [--apply]; default is read-only."""

import argparse
import asyncio
import json

from src.core.database import async_session
from src.services.data_partitions import maintain_partitions, partition_status


async def run(apply: bool) -> None:
    async with async_session() as session:
        if apply:
            moved = await maintain_partitions(session)
            await session.commit()
            print(json.dumps({"moved_rows": moved}))
        print(json.dumps(await partition_status(session)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.apply))


if __name__ == "__main__":
    main()
