# ==============================================================================
# Bonsai-2-27B (Qwen2.5-32B/27B base) Crypto Fin-Tuning with Unsloth / QLoRA
# Recommended Environment: AutoDL / RunPod with 1x RTX 4090 (24GB) or A100 (40GB/80GB)
# Cost: ~$0.20 - $0.40/hr (Total training takes ~1-2 hours)
# ==============================================================================

import torch
from unsloth import FastLanguageModel
from datasets import load_dataset
from trl import SFTTrainer
from transformers import TrainingArguments

# 1. Base Model & Tokenizer
max_seq_length = 8192
model_name = "Qwen/Qwen2.5-32B-Instruct"  # or specific Bonsai base if open-weights
load_in_4bit = True

print(f"[1/5] Loading {model_name} in 4-bit...")
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=model_name,
    max_seq_length=max_seq_length,
    load_in_4bit=load_in_4bit,
    dtype=torch.bfloat16,
)

# 2. Add LoRA Adapters (Targeting all projection layers)
print("[2/5] Injecting QLoRA adapters...")
model = FastLanguageModel.get_peft_model(
    model,
    r=16,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    lora_alpha=16,
    lora_dropout=0,
    bias="none",
    use_gradient_checkpointing="unsloth",
    random_state=3407,
)

# 3. Load Locally Exported Dataset
dataset_path = "crypto_sft_dataset.jsonl"
print(f"[3/5] Loading dataset from {dataset_path}...")

def format_prompts(batch):
    texts = []
    for sys, inp, out in zip(batch["system"], batch["input"], batch["output"]):
        text = f"<|im_start|>system\n{sys}<|im_end|>\n<|im_start|>user\n{inp}<|im_end|>\n<|im_start|>assistant\n{out}<|im_end|>"
        texts.append(text)
    return {"text": texts}

dataset = load_dataset("json", data_files=dataset_path, split="train")
dataset = dataset.map(format_prompts, batched=True)

# 4. Training Arguments
print("[4/5] Starting SFT Training...")
trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset,
    dataset_text_field="text",
    max_seq_length=max_seq_length,
    dataset_num_proc=2,
    packing=False,
    args=TrainingArguments(
        per_device_train_batch_size=2,
        gradient_accumulation_steps=4,
        warmup_steps=10,
        max_steps=100,  # Adjust based on dataset size (e.g., 3-5 epochs)
        learning_rate=2e-4,
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        logging_steps=5,
        optim="adamw_8bit",
        weight_decay=0.01,
        lr_scheduler_type="cosine",
        seed=3407,
        output_dir="outputs",
    ),
)

trainer_stats = trainer.train()
print("[5/5] Training complete! Exporting LoRA to GGUF format...")

# 5. Export LoRA for llama.cpp
model.save_pretrained_merged("bonsai_crypto_lora", tokenizer, save_method="lora")
print("LoRA saved to ./bonsai_crypto_lora. Download this folder to your local machine!")
