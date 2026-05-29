"""Confluence API client wrapper."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config.settings import (
    CONFLUENCE_API_TOKEN,
    CONFLUENCE_SPACE_KEY,
    CONFLUENCE_URL,
    CONFLUENCE_USERNAME,
    PARENT_BUSINESS_TITLE,
    PARENT_TECHNICAL_TITLE,
    require_confluence,
)


@dataclass(frozen=True)
class ConfluencePage:
    page_id: str
    title: str
    url: str
    space_key: str


class ConfluenceError(RuntimeError):
    pass


_PARENT_TITLE_BY_TYPE = {
    "business": PARENT_BUSINESS_TITLE,
    "technical": PARENT_TECHNICAL_TITLE,
}


class ConfluenceClient:
    def __init__(self) -> None:
        require_confluence()
        from atlassian import Confluence  # type: ignore

        self._client = Confluence(
            url=CONFLUENCE_URL,
            username=CONFLUENCE_USERNAME,
            password=CONFLUENCE_API_TOKEN,
            cloud=True,
        )
        self.space_key = CONFLUENCE_SPACE_KEY

    # --------------------------------------------------------------- helpers

    def _build_page_url(self, page_id: str) -> str:
        base = CONFLUENCE_URL.rstrip("/")
        return f"{base}/wiki/spaces/{self.space_key}/pages/{page_id}"

    def _parent_title_for(self, type_: str) -> str:
        try:
            return _PARENT_TITLE_BY_TYPE[type_]
        except KeyError as exc:
            raise ConfluenceError(f"Unknown page type: {type_!r}") from exc

    def _ensure_parent_page(self, type_: str) -> str:
        title = self._parent_title_for(type_)
        existing = self._client.get_page_by_title(
            space=self.space_key, title=title
        )
        if existing:
            return str(existing["id"])
        try:
            created = self._client.create_page(
                space=self.space_key,
                title=title,
                body=f"<p>Diretório raiz para conteúdo do tipo <strong>{type_}</strong>.</p>",
                representation="storage",
            )
        except Exception as exc:  # atlassian-python-api raises generic errors
            raise ConfluenceError(
                f"Failed to create parent page '{title}': {exc}"
            ) from exc
        return str(created["id"])

    # ------------------------------------------------------------------ read

    def find_page(self, title: str) -> Optional[ConfluencePage]:
        try:
            row = self._client.get_page_by_title(space=self.space_key, title=title)
        except Exception as exc:
            raise ConfluenceError(
                f"Failed to query page '{title}': {exc}"
            ) from exc
        if not row:
            return None
        page_id = str(row["id"])
        return ConfluencePage(
            page_id=page_id,
            title=title,
            url=self._build_page_url(page_id),
            space_key=self.space_key,
        )

    # ------------------------------------------------------------ write path

    def upsert_page(
        self,
        *,
        title: str,
        body_xhtml: str,
        type_: str,
    ) -> ConfluencePage:
        parent_id = self._ensure_parent_page(type_)
        existing = self.find_page(title)
        try:
            if existing:
                self._client.update_page(
                    page_id=existing.page_id,
                    title=title,
                    body=body_xhtml,
                    parent_id=parent_id,
                    representation="storage",
                )
                page_id = existing.page_id
            else:
                created = self._client.create_page(
                    space=self.space_key,
                    title=title,
                    body=body_xhtml,
                    parent_id=parent_id,
                    representation="storage",
                )
                page_id = str(created["id"])
        except Exception as exc:
            raise ConfluenceError(
                f"Failed to upsert page '{title}': {exc}"
            ) from exc

        return ConfluencePage(
            page_id=page_id,
            title=title,
            url=self._build_page_url(page_id),
            space_key=self.space_key,
        )
