import asyncio

from backend.config import load_config
from backend.llm import LLM


async def main() -> None:
    cfg = load_config()
    llm = LLM(cfg)
    character = {
        "name": "test",
        "prompt": "Return strict JSON with line test-ok and an empty actions array.",
        "prompt_file": "test",
        "player_nick": "test",
    }
    state = {
        "effective_caps": {"A": 0, "B": 0},
        "relay_status": "disconnected",
        "current": {"A": 0, "B": 0},
        "app_caps": {},
        "device_channels": {},
        "active_channels": {},
        "enabled_channels": {"A": False, "B": False},
        "baseline_strength": {"A": 0, "B": 0},
        "presets": [],
        "max_step": 10,
        "max_pulse_s": 10,
        "max_temp_s": 5,
        "estop": True,
        "dry_run": True,
    }
    try:
        line, actions = await llm.chat(
            character,
            [{"role": "user", "content": "Return test JSON."}],
            state,
        )
        print({"line": line, "actions": actions})
    finally:
        await llm.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
