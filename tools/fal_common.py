"""Shared FAL.ai SDK plumbing.

Holds :func:`import_fal_client` — lazy import + ``lazy_deps`` integration
so ``fal_client`` isn't pulled at cold start (it added ~64 ms per CLI
invocation when imported eagerly).

Stateful pieces (the cached ``fal_client`` module global,
``_submit_fal_request``) intentionally stay on
:mod:`tools.image_generation_tool`. That module is the patch target for
existing test suites (``tests/tools/test_image_generation.py``) and for
the ``plugins/image_gen/fal/`` plugin's ``_it`` indirection. See the
per-rule walkthrough at issue #26241 for details.
"""

from __future__ import annotations

from typing import Any


def import_fal_client() -> Any:
    """Import ``fal_client`` (via ``lazy_deps`` when available) and return
    the module reference.

    Callers are responsible for caching the result on their own module
    global — keeping per-module globals lets tests monkey-patch the
    target module's ``fal_client`` attribute and have the patched value
    stick for that module's call sites.

    Raises :class:`ImportError` if the package is genuinely unavailable.
    """
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("image.fal", prompt=False)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — lazy_deps surfaces install hints
        raise ImportError(str(exc))
    import fal_client  # type: ignore  # noqa: WPS433 — intentionally lazy
    return fal_client
