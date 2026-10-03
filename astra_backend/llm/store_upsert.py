"""Model persistence helpers for nested provider and flattened model records."""


def write_model_into_providers_local_list(*,
        caps,
        ctx_len,
        max_tokens,
        default_effort,
        desc,
        mid,
        name,
        prov,
        reasoning_type,
        api_format="",
        api_path=""):
    if not prov:
        return
    prov_models = prov.setdefault("models", [])
    p_existing = next((m for m in prov_models if m.get("id") == mid), None)
    values = {
        "name": name,
        "capabilities": caps,
        "reasoning_type": reasoning_type,
        "reasoning_effort": default_effort,
        "context_length": ctx_len,
        "max_tokens": max_tokens,
        "description": desc,
        "api_format": api_format or "",
        "api_path": api_path or "",
    }
    if p_existing:
        p_existing.update(values)
    else:
        prov_models.append({"id": mid, **values})


def write_model_into_top_level_list(*,
        api_format,
        api_key,
        base_url,
        caps,
        ctx_len,
        max_tokens,
        default_effort,
        desc,
        existing,
        mid,
        models,
        name,
        provider_id,
        provider_name,
        reasoning_type,
        api_path=""):
    values = {
        "name": name,
        "provider_id": provider_id or "openai",
        "provider_name": provider_name or "自定义",
        "base_url": base_url,
        "api_format": api_format,
        "api_path": api_path or "",
        "reasoning_type": reasoning_type,
        "reasoning_effort": default_effort,
        "capabilities": caps,
        "context_length": ctx_len,
        "max_tokens": max_tokens,
        "description": desc,
    }
    if existing:
        existing.update(values)
        if api_key:
            existing["api_key"] = api_key
    else:
        models.append({"id": mid, "api_key": api_key, **values})
