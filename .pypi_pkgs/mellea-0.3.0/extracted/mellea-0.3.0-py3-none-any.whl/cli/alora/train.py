import json
import os

import typer
from alora.config import aLoraConfig
from alora.peft_model_alora import aLoRAPeftModelForCausalLM
from datasets import Dataset
from peft import LoraConfig, PeftModelForCausalLM
from transformers import AutoModelForCausalLM, AutoTokenizer, TrainerCallback
from trl import DataCollatorForCompletionOnlyLM, SFTConfig, SFTTrainer


def load_dataset_from_json(json_path, tokenizer, invocation_prompt):
    data = []
    with open(json_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                data.append(json.loads(line))

    inputs = []
    targets = []
    for sample in data:
        item_text = sample.get("item", "")
        label_text = sample.get("label", "")
        prompt = f"{item_text}\nRequirement: <|end_of_text|>\n{invocation_prompt}"
        inputs.append(prompt)
        targets.append(label_text)
    return Dataset.from_dict({"input": inputs, "target": targets})


def formatting_prompts_func(example):
    return [
        f"{example['input'][i]}{example['target'][i]}"
        for i in range(len(example["input"]))
    ]


class SaveBestModelCallback(TrainerCallback):
    def __init__(self):
        self.best_eval_loss = float("inf")

    def on_evaluate(self, args, state, control, **kwargs):
        model = kwargs["model"]
        metrics = kwargs["metrics"]
        eval_loss = metrics.get("eval_loss")
        if eval_loss is not None and eval_loss < self.best_eval_loss:
            self.best_eval_loss = eval_loss
            model.save_pretrained(args.output_dir)


class SafeSaveTrainer(SFTTrainer):
    def save_model(self, output_dir: str | None = None, _internal_call: bool = False):
        if self.model is not None:
            self.model.save_pretrained(output_dir, safe_serialization=True)
            if self.tokenizer is not None:
                self.tokenizer.save_pretrained(output_dir)


def train_model(
    dataset_path: str,
    base_model: str,
    output_file: str,
    prompt_file: str | None = None,
    adapter: str = "alora",
    run_name: str = "multiclass_run",
    epochs: int = 6,
    learning_rate: float = 6e-6,
    batch_size: int = 2,
    max_length: int = 1024,
    grad_accum: int = 4,
):
    if prompt_file:
        # load the configurable variable invocation_prompt
        with open(prompt_file) as f:
            config = json.load(f)
        invocation_prompt = config["invocation_prompt"]
    else:
        invocation_prompt = "<|start_of_role|>check_requirement<|end_of_role|>"

    tokenizer = AutoTokenizer.from_pretrained(
        base_model, padding_side="right", trust_remote_code=True
    )
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.add_special_tokens = False

    dataset = load_dataset_from_json(dataset_path, tokenizer, invocation_prompt)
    dataset = dataset.shuffle(seed=42)
    split_idx = int(len(dataset) * 0.8)
    train_dataset = dataset.select(range(split_idx))
    val_dataset = dataset.select(range(split_idx, len(dataset)))

    model_base = AutoModelForCausalLM.from_pretrained(
        base_model, device_map="auto", use_cache=False
    )

    collator = DataCollatorForCompletionOnlyLM(invocation_prompt, tokenizer=tokenizer)

    output_dir = os.path.dirname(output_file)
    os.makedirs(output_dir, exist_ok=True)

    if adapter == "alora":
        peft_config = aLoraConfig(
            invocation_string=invocation_prompt,
            r=32,
            lora_alpha=32,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=["q_proj", "k_proj", "v_proj"],
        )
        response_token_ids = tokenizer(
            invocation_prompt, return_tensors="pt", add_special_tokens=False
        )["input_ids"]
        model = aLoRAPeftModelForCausalLM(
            model_base, peft_config, response_token_ids=response_token_ids
        )

        sft_args = SFTConfig(
            output_dir=output_dir,
            dataset_kwargs={"add_special_tokens": False},
            num_train_epochs=epochs,
            learning_rate=learning_rate,
            max_seq_length=max_length,
            per_device_train_batch_size=batch_size,
            gradient_accumulation_steps=grad_accum,
            fp16=True,
        )

        trainer = SafeSaveTrainer(
            model,
            train_dataset=train_dataset,
            eval_dataset=val_dataset,
            args=sft_args,
            formatting_func=formatting_prompts_func,
            data_collator=collator,
            callbacks=[SaveBestModelCallback()],
        )
        trainer.train()
        model.save_pretrained(output_file)

    else:
        peft_config = LoraConfig(
            r=6,
            lora_alpha=32,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=["q_proj", "k_proj", "v_proj"],
        )
        model = PeftModelForCausalLM(model_base, peft_config)

        sft_args = SFTConfig(
            output_dir=output_dir,
            dataset_kwargs={"add_special_tokens": False},
            num_train_epochs=epochs,
            learning_rate=learning_rate,
            max_seq_length=max_length,
            per_device_train_batch_size=batch_size,
            gradient_accumulation_steps=grad_accum,
            fp16=True,
        )

        trainer = SafeSaveTrainer(
            model,
            train_dataset=train_dataset,
            eval_dataset=val_dataset,
            args=sft_args,
            formatting_func=formatting_prompts_func,
            data_collator=collator,
        )
        trainer.train()
        model.save_pretrained(output_file, safe_serialization=True)
