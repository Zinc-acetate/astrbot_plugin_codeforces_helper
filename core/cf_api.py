from urllib.parse import urlencode

from .rate_limit import CODEFORCES_API_RATE_LIMITER


async def request_cf_api(
    session,
    method_name: str,
    params: dict | None = None,
    timeout: int = 20,
    *,
    limiter=CODEFORCES_API_RATE_LIMITER,
    rate_limit_retries: int = 2,
):
    """Request one Codeforces API method with shared pacing and limit retries."""
    query = urlencode(params or {})
    url = f"https://codeforces.com/api/{method_name}"
    if query:
        url = f"{url}?{query}"

    for attempt in range(rate_limit_retries + 1):
        await limiter.wait()
        try:
            async with session.get(url, timeout=timeout) as response:
                response.raise_for_status()
                data = await response.json()
        except Exception as exc:
            if getattr(exc, "status", None) == 429 and attempt < rate_limit_retries:
                continue
            raise

        comment = str(data.get("comment", "")) if isinstance(data, dict) else ""
        limited = (
            isinstance(data, dict)
            and data.get("status") == "FAILED"
            and "call limit exceeded" in comment.lower()
        )
        if limited and attempt < rate_limit_retries:
            continue
        return data

    raise RuntimeError("Codeforces API request retry loop ended unexpectedly")
