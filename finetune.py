from data_module import CommonDataset, custom_data_collator
from dataloader import CustomTrainer
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, AutoConfig, set_seed
import hydra 
import transformers
import os
from peft import LoraConfig, get_peft_model
from pathlib import Path
from omegaconf import OmegaConf
from utils import get_model_identifiers_from_yaml
import wandb

def find_all_linear_names(model):
    cls = torch.nn.Linear
    lora_module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, cls):
            names = name.split('.')
            lora_module_names.add(names[0] if len(names) == 1 else names[-1])
    if 'lm_head' in lora_module_names: 
        lora_module_names.remove('lm_head')
    return list(lora_module_names)


def print_trainable_parameters(model):
    """
    Prints the number of trainable parameters in the model.
    """
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    print(
        f"trainable params: {trainable_params} || all params: {all_param} || trainable%: {100 * trainable_params / all_param}"
    )

import logging
import argparse
import sys

# Handle DeepSpeed's --local_rank argument
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--local_rank', type=int, default=0)
args, remaining_args = parser.parse_known_args()

# Remove --local_rank from sys.argv so Hydra doesn't see it
sys.argv = [sys.argv[0]] + remaining_args


@hydra.main(version_base=None, config_path="config", config_name="finetune")
def main(cfg):
    if os.environ.get('LOCAL_RANK') is not None:
        local_rank = int(os.environ.get('LOCAL_RANK', '0'))
        device_map = {'': local_rank}
    set_seed(cfg.seed)
    #os.environ["WANDB_DISABLED"] = "true"
    model_cfg = get_model_identifiers_from_yaml(cfg.model_family)
    model_id = model_cfg["hf_key"]

    Path(cfg.save_dir).mkdir(parents=True, exist_ok=True)
    # save the cfg file
    #if master process
    if os.environ.get('LOCAL_RANK') is None or local_rank == 0:
        with open(f'{cfg.save_dir}/cfg.yaml', 'w') as f:
            OmegaConf.save(cfg, f)

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    tokenizer.pad_token = tokenizer.eos_token
    max_seq_length = tokenizer.model_max_length
    tokenizer.model_max_length = max_seq_length

    max_length = 500
    torch_format_dataset = CommonDataset(cfg.dataset, cfg.data_path, tokenizer=tokenizer, model_family = cfg.model_family, max_length=max_length)
    if cfg.ds_size:
        torch_format_dataset.data = {key: torch_format_dataset.data[key] for key in range(min(cfg.ds_size, len(torch_format_dataset.data)))}
        
    
    batch_size = cfg.batch_size
    gradient_accumulation_steps = cfg.gradient_accumulation_steps
    num_devices = int(os.environ.get('WORLD_SIZE', 1))
    print(f"num_devices: {num_devices}")
    
    if cfg.bf16 is True:
        torch_dtype = torch.bfloat16
    else:
        torch_dtype = torch.float16


    # Declare wandb
    os.environ["WANDB_PROJECT"] = cfg.project_name
    os.environ["WANDB_DIR"] = cfg.log_dir
    wandb.init(name=cfg.run_name)


    if cfg.use_deepspeed:
        deepspeed_config = "config/ds_config.json"
    else:
        deepspeed_config = None


    steps_per_epoch = len(torch_format_dataset) // (batch_size * gradient_accumulation_steps * num_devices)
    max_steps = int(cfg.num_epochs * steps_per_epoch)
    
    print(f"max_steps: {max_steps}")
    training_args = transformers.TrainingArguments(
            per_device_train_batch_size=batch_size,
            per_device_eval_batch_size=batch_size,
            gradient_accumulation_steps=gradient_accumulation_steps,
            warmup_steps=max(1, max_steps//cfg.num_epochs),
            max_steps=max_steps,
            learning_rate=cfg.lr,
            bf16=cfg.bf16, 
            bf16_full_eval=cfg.bf16, 
            logging_steps=max(1,max_steps//20),
            logging_dir=f'{cfg.save_dir}/logs',
            output_dir=cfg.save_dir,
            optim="paged_adamw_8bit",
            #optim="adamw_torch",
            save_steps=max_steps,
            save_only_model=True,
            ddp_find_unused_parameters= False,
            #evaluation_strategy="no",
            deepspeed=deepspeed_config,
            weight_decay = cfg.weight_decay,
            seed = cfg.seed,

            report_to='wandb',
            lr_scheduler_type='cosine'
            #lr_scheduler_type='constant_with_warmup' # DP: Add this to make training more stable wrt learning rate for the forget rows

        )

    model = AutoModelForCausalLM.from_pretrained(model_id, \
        use_flash_attention_2=model_cfg["flash_attention2"]=="true", \
        torch_dtype=torch_dtype, trust_remote_code = True)
    
    model.generation_config.do_sample = True

    if model_cfg["gradient_checkpointing"] == "true":
        model.gradient_checkpointing_enable()

    if cfg.LoRA.r != 0:
        config = LoraConfig(
            r=cfg.LoRA.r, 
            lora_alpha=cfg.LoRA.alpha, 
            target_modules=find_all_linear_names(model), 
            lora_dropout=cfg.LoRA.dropout,
            bias="none", 
            task_type="CAUSAL_LM"
        )
        model = get_peft_model(model, config)
        model.enable_input_require_grads()    

    trainer = CustomTrainer(
        model=model,
        train_dataset=torch_format_dataset,
        eval_dataset=torch_format_dataset,
        args=training_args,
        data_collator=custom_data_collator,
    )
    model.config.use_cache = False  # silence the warnings. Please re-enable for inference!
    trainer.train()

    #save the model
    if cfg.LoRA.r != 0:
        model = model.merge_and_unload()


    model.save_pretrained(cfg.save_dir)
    tokenizer.save_pretrained(cfg.save_dir)

if __name__ == "__main__":
    main()
