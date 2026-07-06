"""Create Timeline schema objects."""

from __future__ import annotations

import argparse
import asyncio

from app.db.schema import create_schema


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-vector", action="store_true", help="Create non-vector tables only")
    args = parser.parse_args()
    status = await create_schema(include_vector=not args.skip_vector)
    for key, value in status.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    asyncio.run(main())
