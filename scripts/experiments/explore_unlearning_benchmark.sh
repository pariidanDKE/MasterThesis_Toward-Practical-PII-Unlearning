#!/bin/bash
### This script is used to evaluate the PerMU in-text method with grid search over C and P parameters
export dataset="PII"
export MASTER_PORT=18765
export forget_data_path="$PWD/data/${dataset}"

### Hyperparameters
export cache="$PWD/cache"
export retain_weight=1
export num_epochs=8
export neftune_noise_alpha=False

### MULTI GPU PARAMS
export CUDA_VISIBLE_DEVICES=0
export use_deepspeed=False
export batch_size=16 ## Should increase the batch size to 8 (Would just make it all faster, no other difference)
export gradaccum=2
export eval_batch_size=64

export optimizer="paged_adamw_8bit"
export optimal_neighbours_generation=False

### Unlearning Params
export forget_loss="PerMU"
export project_name="Misc"
export use_quantization=False
export split="forget10"
export remove_model_tensors=True
C_values=(0.1)
P_values=(1.2)
num_runs=1
#model_list=("llama3.1-8b")
model_list=("llama2-7b")

token_level_values=(True)
export num_epochs=2
export token_replace_prob=0.1
export lr=1e-5

echo "Starting grid search with modified P-value logic:"
echo "- When token_level=False: ${#C_values[@]} C values × ${#P_values[@]} P values × ${num_runs} runs"
echo "- When token_level=True: ${#C_values[@]} C values × 1 P value × ${num_runs} runs"
echo "============================================"

for model in "${model_list[@]}"; do
    export cache_path="/projects/0/hpmlprjs/LLM/danp/UGBench/models/neighbourhood_cache/${model}/${model}.pkl"
        # export batch_size=1

    # Grid search over C, P, token_level values, and multiple runs
    for token_level in "${token_level_values[@]}"; do
        for C in "${C_values[@]}"; do
            # Only loop through P values when token_level is False
            if [[ "$token_level" == "False" ]]; then
                P_loop=("${P_values[@]}")
            else
                # When token_level is True, use only the first P value (or you can set a default)
                P_loop=(1)  # Using first P value as default when token_level=True
            fi
            
            for P in "${P_loop[@]}"; do
                for run in $(seq 1 $num_runs); do
                    echo "Running experiment with C=${C}, P=${P}, token_level=${token_level}, run=${run}"

                    # Set optimal_neighbours_generation based on token_level value
                    if [[ "$token_level" == "True" ]]; then
                        export optimal_neighbours_generation=True
                    else
                        export optimal_neighbours_generation=False
                    fi

                    export run_name="${model}_E${num_epochs}_B${batch_size}_C${C}_P_${P}_token_level${token_level}_run${run}_test"
                    export save_dir="$PWD/experiment/${dataset}/${model}/${split}/_AllExperiments/PIIAnalysis/$run_name"
                    if [ $run -ne 10 ]; then
                        #-------- Run Training --------
                        python forget.py --config-name=forget_pii.yaml \
                        dataset=$dataset split=$split \
                        forget_data_path=$forget_data_path \
                        retain_data_path=$forget_data_path \
                        forget_loss=$forget_loss batch_size=$batch_size \
                        retain_weight=$retain_weight \
                        gradient_accumulation_steps=$gradaccum model_family=$model lr=$lr \
                        save_dir=$save_dir cache_dir=$cache num_epochs=$num_epochs \
                        use_quantization=$use_quantization \
                        project_name=$project_name \
                        run_name=$run_name \
                        token_level=$token_level \
                        logging.corrupted_subjects=True \
                        optimal_neighbours_generation=$optimal_neighbours_generation \
                        neftune_noise_alpha=$neftune_noise_alpha \
                        C=$C \
                        P=$P \
                        use_deepspeed=$use_deepspeed \
                        optimizer=$optimizer \
                        optimal_neighbours_generation=$optimal_neighbours_generation \
                        cache_path=$cache_path \

                        # Check if training was successful
                        if [ $? -ne 0 ]; then
                            echo "Training failed for C=${C}, P=${P}, token_level=${token_level}, run=${run}. Continuing to next combination..."
                            continue
                        fi
                    else
                        echo "Skipping training for run 3 as it is already done."
                        fi

                    # -------- Evaluate Model --------
                    python evaluate_PII.py --config-name=eval_pii.yaml \
                        model_family=$model dataset=$dataset \
                        split=$split batch_size=$eval_batch_size \
                        model_path=$save_dir forget_loss=$forget_loss \
                        generation.max_length=200 \
                        save_dir=$save_dir/eval_results \

                    # Check if evaluation was successful
                    if [ $? -ne 0 ]; then
                        echo "Evaluation failed for C=${C}, P=${P}, token_level=${token_level}, run=${run}. Continuing to next combination..."
                        continue
                    fi

                    # # -------- Aggregate Evaluation --------
                    python aggregate_eval_stat.py \
                        ckpt_result=$save_dir/eval_results/eval_log_aggregated.json \
                        method_name=$forget_loss \
                        save_file=$save_dir/eval_results/eval.csv \
                        excel_file_path=$save_dir/eval_results/eval.xlsx \
                        submitted_by=who \
                        remove_model_tensors=$remove_model_tensors

                    echo "Finished run for C=${C}, P=${P}, token_level=${token_level}, run=${run}"
                    echo "--------------------------------------------"
                done
            done
        done
    done
done

echo "Finished all grid search experiments"
echo "============================================"