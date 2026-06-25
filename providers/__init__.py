"""Provider layer: interpret agent error payloads per LLM provider.

classify(provider_name, payload_text, error_handling) is the common entry — it
preliminarily parses the agent's payload (base.extract_signals), then dispatches
to the named provider module, falling back to default.

Provider identity comes from the key pool (the active key's provider), so the
agent stays provider-agnostic; the orchestration pairs them here.
"""
import json

from . import base, default, zhipu

# Map provider name (as declared in api-keys.json) to its interpretation module.
# Providers in the same family can share a module until they diverge.
REGISTRY = {
    "zhipu": zhipu,
}


def classify(provider_name, payload_text, error_handling):
    """Return 'action:disable' for an agent error payload under the given provider."""
    payload = _parse_payload(payload_text)
    signals = base.extract_signals(payload)
    module = REGISTRY.get(provider_name, default)
    return module.classify(signals, error_handling)


def _parse_payload(payload_text):
    """Parse the agent's JSON payload, tolerating empty/non-JSON input."""
    if not payload_text:
        return {}
    try:
        obj = json.loads(payload_text)
    except (ValueError, TypeError):
        return {"message": payload_text}
    return obj if isinstance(obj, dict) else {"message": str(obj)}
