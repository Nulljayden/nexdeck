"""Extensions: integrations an administrator imports as YAML files."""

from __future__ import annotations

import logging

from fastapi import APIRouter, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..deps import AdminUser, DbSession, error
from ..models import Integration
from ..services import extensions

router = APIRouter(prefix="/api/v1/extensions", tags=["integrations"])
logger = logging.getLogger("nexdeck.extensions")


class ExtensionImport(BaseModel):
    text: str = Field(min_length=1, max_length=extensions.MAX_BYTES)


def _listing(db: DbSession) -> dict:
    counts = dict(db.execute(
        select(Integration.kind, func.count(Integration.id))
        .where(Integration.kind.startswith(extensions.PREFIX))
        .group_by(Integration.kind)
    ).all())
    return {
        "folder": str(extensions.directory()),
        "extensions": [
            {**extensions.summary(adapter), "connections": int(counts.get(adapter.kind, 0))}
            for adapter in sorted(extensions.LOADED.values(), key=lambda one: one.label.lower())
        ],
        "broken": [{"file": name, "error": reason} for name, reason in sorted(extensions.BROKEN.items())],
    }


@router.get("", summary="The imported extensions, and files that could not be read")
def list_extensions(user: AdminUser, db: DbSession) -> dict:
    return _listing(db)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Import an extension, or replace one with the same id")
def import_extension(body: ExtensionImport, user: AdminUser, db: DbSession) -> dict:
    try:
        adapter = extensions.install(body.text)
    except extensions.ExtensionError as failure:
        raise error("bad_extension", str(failure)) from failure
    logger.info("Extension %r (%s) imported by %s.", adapter.spec.id, adapter.spec.version, user.username)
    return extensions.summary(adapter)


@router.post("/reload", summary="Read the extension folder again")
def reload_extensions(user: AdminUser, db: DbSession) -> dict:
    extensions.reload()
    return _listing(db)


@router.get("/{extension_id}/source", response_class=PlainTextResponse, summary="The extension's file")
def extension_source(extension_id: str, user: AdminUser) -> str:
    adapter = extensions.LOADED.get(extensions.PREFIX + extension_id)
    if adapter is None or adapter.path is None:
        raise error("not_found", "There is no such extension.", status.HTTP_404_NOT_FOUND)
    return adapter.path.read_text(encoding="utf-8")


@router.delete("/{extension_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove an extension")
def remove_extension(extension_id: str, user: AdminUser) -> None:
    try:
        extensions.remove(extension_id)
    except extensions.ExtensionError as failure:
        raise error("not_found", str(failure), status.HTTP_404_NOT_FOUND) from failure
    logger.info("Extension %r removed by %s.", extension_id, user.username)
