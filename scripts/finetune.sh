# #!/bin/bash

# This script simply finetunes the model on a given dataset.
export dataset="PII";   # [TOFU, Harry, ZSRE]
export master_port=18765;
export model=qwen2.5-32b;

#export BNB_CUDA_VERSION=121
export split=full_with_qa;    
export lr=1e-5;
export batch_size=2;
export GA=1;
export epoch=2;
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export use_deepspeed=true;  # Use deepspeed for training

export CUDA_VISIBLE_DEVICES=0,1,2,3
export data_path=$PWD/data/${dataset}/${split}.json;
export save_file=$PWD/save_model/${dataset}/${split}_${model}_B${batch_size}_G${GA}_E${epoch}_lr${lr};
#export NCCL_DEBUG=INFO;

export data_path="$PWD/data/${dataset}/${split}.json"
export run_name="FineTune_${dataset}_${model}_B${batch_size}_G${GA}_E${epoch}_lr${lr}"

export project_name="FTQwen2.5-32B"
export file_path="/projects/0/hpmlprjs/LLM/danp/UGBench/finetune.py"

deepspeed ${file_path} --config-name=finetune.yaml \
    batch_size=${batch_size} gradient_accumulation_steps=${GA} \
    dataset=${dataset} data_path=${data_path} lr=${lr} num_epochs=${epoch}\
    model_family=${model} save_dir=${save_file} \
    project_name=${project_name} \
    run_name=$run_name \
    use_deepspeed=${use_deepspeed} \