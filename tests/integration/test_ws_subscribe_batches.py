from screener.infrastructure.bybit.ws import WebSocketManager


def test_subscribe_batches_respect_char_budget() -> None:
    mgr = WebSocketManager(
        "wss://example",
        on_message=lambda m: None,  # type: ignore[arg-type]
        args_char_budget=200,
    )
    symbols = [f"COIN{i}USDT" for i in range(50)]
    batches = mgr._subscribe_batches(symbols)
    assert batches
    for batch in batches:
        # rough JSON array length
        payload = str(batch)
        assert len(payload) <= 220
