"""Leitstand: meldet, welche Modelle ComfyUI gerade im Speicher hat.

Wird nach ComfyUI/custom_nodes verlinkt (install.sh) und haengt die Route
GET /leitstand/models an den ComfyUI-Server. Keine Nodes.
"""

import comfy.model_management as mm
from aiohttp import web
from server import PromptServer

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


def _describe(lm):
    patcher = lm.model
    if patcher is None:
        return None
    inner = getattr(patcher, "model", None)
    entry = {
        "class": type(inner).__name__ if inner is not None else type(patcher).__name__,
        "device": str(lm.device),
        "in_use": bool(lm.currently_used),
    }
    try:
        entry["size"] = int(lm.model_memory())
        entry["loaded"] = int(lm.model_loaded_memory())
    except Exception:
        entry["size"] = entry["loaded"] = None
    return entry


@PromptServer.instance.routes.get("/leitstand/models")
async def leitstand_models(request):
    models = [d for d in (_describe(lm) for lm in list(mm.current_loaded_models)) if d]
    return web.json_response(models)
