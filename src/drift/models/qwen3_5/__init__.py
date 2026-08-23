from drift.models.qwen3_5.block import WrappedQwen3_5Block
from drift.models.qwen3_5.config import DistributedQwen3_5Config
from drift.models.qwen3_5.model import DistributedQwen3_5ForCausalLM, DistributedQwen3_5Model
from drift.utils.auto_config import register_model_classes

register_model_classes(
    config=DistributedQwen3_5Config,
    model=DistributedQwen3_5Model,
    model_for_causal_lm=DistributedQwen3_5ForCausalLM,
)
