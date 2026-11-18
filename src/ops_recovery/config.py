from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Settings:
    mode: str = "offline"
    base_url: str = ""
    model_id: str = ""
    api_key: str = field(default="", repr=False)
    input_price: float = 0
    output_price: float = 0
    budget: float = 100
    max_output: int = 2048
    model_timeout: float = 45
    agent_deadline: float = 120
    api_style: str = "openai"
    state_dir: Path = Path(".state")

    @classmethod
    def load(cls, mode: str | None = None) -> Settings:
        load_dotenv(Path.cwd() / ".env", override=False)
        result = cls(
            mode=mode or os.getenv("OPS_MODEL_MODE", "offline"),
            base_url=os.getenv("OPS_API_BASE_URL", "").rstrip("/"),
            model_id=os.getenv("OPS_MODEL_ID", ""),
            api_key=os.getenv("OPS_API_KEY", ""),
            input_price=float(os.getenv("OPS_INPUT_PRICE", "0")),
            output_price=float(os.getenv("OPS_OUTPUT_PRICE", "0")),
            budget=float(os.getenv("OPS_BUDGET_CNY", "100")),
            max_output=int(os.getenv("OPS_MAX_OUTPUT_TOKENS", "2048")),
            model_timeout=float(os.getenv("OPS_MODEL_TIMEOUT", "45")),
            agent_deadline=float(os.getenv("OPS_AGENT_DEADLINE", "120")),
            api_style=os.getenv("OPS_API_STYLE", "openai"),
            state_dir=Path(os.getenv("OPS_STATE_DIR", ".state")).resolve(),
        )
        if result.mode not in {"offline", "online"}:
            raise ValueError("OPS_MODEL_MODE must be offline or online")
        if not 0 < result.budget <= 100:
            raise ValueError("The project budget must be greater than zero and at most CNY 100")
        if not 1 <= result.max_output <= 4096 or not 1 <= result.agent_deadline <= 180:
            raise ValueError("Invalid output limit or agent deadline")
        if result.mode == "online":
            if not all((result.base_url, result.model_id, result.api_key)):
                raise ValueError("Configure OPS_API_BASE_URL, OPS_MODEL_ID and OPS_API_KEY locally")
            if result.input_price <= 0 or result.output_price <= 0:
                raise ValueError("Online evaluation requires explicit conservative token prices")
            if not result.base_url.startswith("https://"):
                raise ValueError("Online credentials require an HTTPS provider endpoint")
        return result
