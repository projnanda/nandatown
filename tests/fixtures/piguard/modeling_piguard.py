# modeling_injecguard.py
from transformers import DebertaV2ForSequenceClassification, DebertaV2Config
from transformers.modeling_outputs import SequenceClassifierOutput
import torch

class PIGuardConfig(DebertaV2Config):
    model_type = "piguard"

PIGuardConfig.register_for_auto_class()

class PIGuard(DebertaV2ForSequenceClassification):
    config_class = PIGuardConfig

    def __init__(self, config):
        super().__init__(config)
        self.classifier = torch.nn.Linear(config.hidden_size, config.num_labels)

    def forward(self, input_ids, attention_mask, **kwargs):
        outputs = self.deberta(
            input_ids=input_ids,
            attention_mask=attention_mask,
            output_hidden_states=False
        )

        pooled_output = outputs.last_hidden_state[:, 0, :]
        logits = self.classifier(pooled_output)
        return SequenceClassifierOutput(logits=logits)

PIGuard.register_for_auto_class("AutoModelForSequenceClassification")
