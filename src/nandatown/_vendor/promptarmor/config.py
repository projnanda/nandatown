from dataclasses import dataclass


@dataclass
class PAConfig:
    base_url: str = "http://localhost:8000/v1"
    api_key: str = "EMPTY"
    model: str = "Qwen/Qwen3-32B"
    temperature: float = 0.0
    enable_thinking: bool = False        # True recommended for ~8B models
    include_definition: bool = False     # True only for weak/small (<8B) models
    on_detect: str = "remove"            # "remove" | "block" | "flag"
    removal_length_guard: float = 3.0    # max matched/extracted length ratio for fuzzy removal
    max_concurrency: int = 32
    use_guided_json: bool = True         # vLLM guided decoding for valid JSON
