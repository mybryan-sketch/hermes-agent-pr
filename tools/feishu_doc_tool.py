"""Feishu Document Tool -- read document content via Feishu/Lark API.

Provides ``feishu_doc_read`` for reading document content as plain text.
Uses the same lazy-import + BaseRequest pattern as feishu_comment.py.
"""

import json
import logging
import os
import re
import threading

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Thread-local storage for the lark client injected by feishu_comment handler.
_local = threading.local()
_fallback_client = None
_fallback_client_key = None
_fallback_client_lock = threading.Lock()


def set_client(client):
    """Store a lark client for the current thread (called by feishu_comment)."""
    _local.client = client


def get_client():
    """Return the lark client for the current thread, or None."""
    client = getattr(_local, "client", None)
    if client is not None:
        return client
    return _get_fallback_client()


def _get_fallback_client():
    """Build a Feishu client from profile env for normal chat contexts."""
    global _fallback_client, _fallback_client_key

    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    domain_name = os.getenv("FEISHU_DOMAIN", "feishu").strip().lower()
    if not app_id or not app_secret:
        return None

    cache_key = (app_id, app_secret, domain_name)
    with _fallback_client_lock:
        if _fallback_client is not None and _fallback_client_key == cache_key:
            return _fallback_client

        try:
            import lark_oapi as lark
            from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
        except ImportError:
            return None

        domain = LARK_DOMAIN if domain_name == "lark" else FEISHU_DOMAIN
        _fallback_client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        _fallback_client_key = cache_key
        return _fallback_client


# ---------------------------------------------------------------------------
# feishu_doc_read
# ---------------------------------------------------------------------------

_RAW_CONTENT_URI = "/open-apis/docx/v1/documents/:document_id/raw_content"
_WIKI_GET_NODE_URI = "/open-apis/wiki/v2/spaces/get_node"
_FEISHU_DOC_URL_RE = re.compile(
    r"(?:feishu\.cn|larkoffice\.com|larksuite\.com|lark\.suite\.com)"
    r"/(?P<doc_type>wiki|doc|docx|sheet|sheets|slides|mindnote|bitable|base|file)"
    r"/(?P<token>[A-Za-z0-9_-]{10,80})"
)
_DOC_TYPE_ALIASES = {
    "sheets": "sheet",
    "base": "bitable",
}
_READABLE_DOC_TYPES = {"", "doc", "docx"}

FEISHU_DOC_READ_SCHEMA = {
    "name": "feishu_doc_read",
    "description": (
        "Read the full content of a Feishu/Lark document as plain text. "
        "Useful when you need more context beyond the quoted text in a comment."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "doc_token": {
                "type": "string",
                "description": "The document token or Feishu/Lark document URL.",
            },
            "file_type": {
                "type": "string",
                "description": "Optional Feishu file type such as wiki, docx, doc, or sheet.",
            },
        },
        "required": ["doc_token"],
    },
}


def _check_feishu():
    # Use ``importlib.util.find_spec`` — it checks whether ``lark_oapi``
    # is importable without actually executing its ``__init__``.
    # Executing the real import here costs ~5 seconds (the SDK eagerly
    # loads websockets, dispatcher, every api/v2 model) and this probe
    # fires at every ``hermes`` startup during tool-availability
    # evaluation.  Correctness is preserved because the actual tool
    # handler still does the real import when invoked.
    import importlib.util
    try:
        return importlib.util.find_spec("lark_oapi") is not None
    except (ImportError, ValueError):
        return False


def _normalise_doc_ref(value: str, file_type: str = "") -> tuple[str, str]:
    """Return ``(doc_type, token)`` from a URL, raw token, or hinted token."""
    raw = (value or "").strip()
    if not raw:
        return "", ""
    match = _FEISHU_DOC_URL_RE.search(raw)
    if match:
        doc_type = match.group("doc_type").lower()
        return _DOC_TYPE_ALIASES.get(doc_type, doc_type), match.group("token")
    doc_type = (file_type or "").strip().lower()
    return _DOC_TYPE_ALIASES.get(doc_type, doc_type), raw


def _build_request(method: str, uri: str, paths=None, queries=None):
    from lark_oapi import AccessTokenType
    from lark_oapi.core.enum import HttpMethod
    from lark_oapi.core.model.base_request import BaseRequest

    http_method = getattr(HttpMethod, method.upper())
    builder = (
        BaseRequest.builder()
        .http_method(http_method)
        .uri(uri)
        .token_types({AccessTokenType.TENANT})
    )
    if paths:
        builder = builder.paths(paths)
    if queries:
        builder = builder.queries(queries)
    return builder.build()


def _decode_response(response) -> tuple[int | None, str, dict]:
    code = getattr(response, "code", None)
    msg = getattr(response, "msg", "")
    data: dict = {}

    raw = getattr(response, "raw", None)
    raw_content = getattr(raw, "content", None)
    if raw_content:
        try:
            if isinstance(raw_content, bytes):
                raw_content = raw_content.decode("utf-8")
            body = json.loads(raw_content)
            if isinstance(body, dict):
                data = body.get("data") or {}
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            data = {}

    if not data:
        response_data = getattr(response, "data", None)
        if isinstance(response_data, dict):
            data = response_data.get("data") or response_data
        elif response_data and hasattr(response_data, "__dict__"):
            data = vars(response_data)

    return code, msg, data


def _request_json(client, method: str, uri: str, paths=None, queries=None):
    request = _build_request(method, uri, paths=paths, queries=queries)
    response = client.request(request)
    return _decode_response(response)


def _resolve_wiki_token(client, wiki_token: str):
    code, msg, data = _request_json(
        client,
        "GET",
        _WIKI_GET_NODE_URI,
        queries=[("token", wiki_token)],
    )
    if code != 0:
        return None, f"code={code} msg={msg}"

    node = data.get("node") if isinstance(data, dict) else None
    if not isinstance(node, dict):
        node = data if isinstance(data, dict) else {}

    raw_obj_type = str(node.get("obj_type", "")).lower()
    obj_type = _DOC_TYPE_ALIASES.get(raw_obj_type, raw_obj_type)
    obj_token = str(node.get("obj_token", "")).strip()
    if not obj_type or not obj_token:
        return None, "wiki node response did not include obj_type/obj_token"
    return {"doc_type": obj_type, "token": obj_token}, None


def _read_docx_raw_content(client, doc_token: str):
    code, msg, data = _request_json(
        client,
        "GET",
        _RAW_CONTENT_URI,
        paths={"document_id": doc_token},
    )
    if code != 0:
        return None, f"code={code} msg={msg}"
    content = data.get("content", "") if isinstance(data, dict) else ""
    return content, None


def _handle_feishu_doc_read(args: dict, **kwargs) -> str:
    doc_ref = (args.get("doc_token") or args.get("url") or "").strip()
    if not doc_ref:
        return tool_error("doc_token is required")

    client = get_client()
    if client is None:
        return tool_error("Feishu client not available (missing FEISHU_APP_ID/FEISHU_APP_SECRET)")

    doc_type, token = _normalise_doc_ref(doc_ref, args.get("file_type", ""))
    if not token:
        return tool_error("doc_token is required")

    resolved_from_wiki = False
    try:
        if doc_type == "wiki":
            resolved, err = _resolve_wiki_token(client, token)
            if err:
                return tool_error(f"Failed to resolve wiki document: {err}")
            doc_type = resolved["doc_type"]
            token = resolved["token"]
            resolved_from_wiki = True

        if doc_type not in _READABLE_DOC_TYPES:
            return tool_error(f"Unsupported Feishu document type for text read: {doc_type}")

        content, read_err = _read_docx_raw_content(client, token)
        if read_err is None:
            return tool_result(
                success=True,
                content=content,
                resolved_type=doc_type or "docx",
                resolved_token=token,
                resolved_from_wiki=resolved_from_wiki,
            )

        # Normal Feishu chat often gives the model only a wiki node token.  If
        # the docx read failed and the caller did not know it was a wiki URL,
        # resolve it as a wiki node and retry before surfacing the original API
        # error.
        if not resolved_from_wiki:
            resolved, wiki_err = _resolve_wiki_token(client, token)
            if resolved and resolved["doc_type"] in _READABLE_DOC_TYPES:
                wiki_content, wiki_read_err = _read_docx_raw_content(client, resolved["token"])
                if wiki_read_err is None:
                    return tool_result(
                        success=True,
                        content=wiki_content,
                        resolved_type=resolved["doc_type"] or "docx",
                        resolved_token=resolved["token"],
                        resolved_from_wiki=True,
                        wiki_token=token,
                    )
            elif resolved:
                return tool_error(
                    f"Unsupported Feishu document type for text read: {resolved['doc_type']}",
                    wiki_token=token,
                    resolved_token=resolved["token"],
                )
            logger.debug("Feishu wiki fallback failed for token %s: %s", token, wiki_err)
    except ImportError:
        return tool_error("lark_oapi not installed")

    return tool_error(f"Failed to read document: {read_err}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="feishu_doc_read",
    toolset="feishu_doc",
    schema=FEISHU_DOC_READ_SCHEMA,
    handler=_handle_feishu_doc_read,
    check_fn=_check_feishu,
    requires_env=[],
    is_async=False,
    description="Read Feishu document content",
    emoji="\U0001f4c4",
)
