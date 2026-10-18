"""Operator-only archive CLI; mutations require explicit --apply."""

import argparse
import asyncio
import json
import re
from pathlib import Path
from uuid import UUID
from typing import Any
from src.core.database import engine
from src.services.archive.runtime import archive_service
from src.services.conversation_lifecycle import ArchiveBlocked
from src.services.archive.schemas import ArchiveFormatError
from src.services.rag_archive_client import RagArchiveError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("list", "run", "resume", "export", "restore"):
        item = sub.add_parser(command)
        item.add_argument("--org-id", type=UUID, required=True)
        if command == "run":
            target = item.add_mutually_exclusive_group(required=True)
            target.add_argument("--conversation-id", type=UUID)
            target.add_argument("--audit", action="store_true")
        if command in ("resume", "export", "restore"):
            item.add_argument("--archive-id", type=UUID, required=True)
        if command in ("run", "resume", "restore"):
            mode = item.add_mutually_exclusive_group()
            mode.add_argument("--apply", action="store_true")
            mode.add_argument("--dry-run", action="store_true")
        if command == "export":
            item.add_argument("--output", type=Path, required=True)
    return root


async def run(args: argparse.Namespace) -> None:
    engine.echo = False
    engine.sync_engine.hide_parameters = True
    result: Any
    try:
        async with archive_service() as service:
            if args.command == "list":
                result = await service.list(args.org_id)
            elif args.command == "run":
                result = (
                    await service.run_audit(args.org_id, apply=args.apply)
                    if args.audit
                    else await service.run(args.org_id, args.conversation_id, apply=args.apply)
                )
            elif args.command == "resume":
                if not args.apply:
                    result = {"apply_required": True}
                else:
                    await service.resume(args.org_id, args.archive_id)
                    result = {"archive_id": args.archive_id, "resumed": True}
            elif args.command == "restore":
                result = await service.restore(args.org_id, args.archive_id, apply=args.apply)
            else:
                await service.export(args.org_id, args.archive_id, args.output)
                result = {"archive_id": args.archive_id, "exported": True}
            print(json.dumps(result, default=str, sort_keys=True))
    except (ArchiveBlocked, ArchiveFormatError, RagArchiveError) as error:
        reason = str(error)
        print(
            json.dumps(
                {
                    "error": reason
                    if re.fullmatch(r"[a-z_]{1,64}", reason)
                    else "archive_operation_blocked"
                }
            )
        )
        raise SystemExit(1) from None
    except Exception:
        # Raw SQL/HTTP exceptions may contain archived payload or credentials.
        print(json.dumps({"error": "archive_operation_blocked"}))
        raise SystemExit(1) from None
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    main()
