#!/bin/bash

## This script runs both training and evaluation for PerMU in-text method
## K-Distance Experiments: 4 configurations x 10 runs each = 40 total runs
export BNB_CUDA_VERSION=121
export dataset="PII"
export MASTER_PORT=18765
export model=llama3.1-8b;   # [phi, llama2-7b]
export num_epochs=8
export train_batch_size=16  # Updated from 16 as per your note
export eval_batch_size=64  # Updated from 16 as per your note
export gradaccum=2
export cache="$PWD/cache"
export retain_weight=1
export lr=1e-5
export optimizer="paged_adamw_8bit"



export CUDA_VISIBLE_DEVICES=0
export forget_loss="PerMU"
export split="forget10"
export project_name="LLama3.1_PerMuTok"
export use_lora=False
export use_quantization=False
export forget_data_path="$PWD/data/${dataset}"
## PerMU in-text base params
export in_text=True
export logging_timestats=False
export remove_model_tensors=True 
#export logging_permu_contrast_stats=True

export CUDA_LAUNCH_BLOCKING=1
export TORCH_USE_CUDA_DSA=1

# Fixed parameters
export num_runs=5
export token_replace_prob=1.0
export optimal_neighbours_generation=True


# Define experiment configurations
# Format: "k_neighbours:match_first_char:use_adaptive_k:config_name"
experiment_configs=(
    "1:True:False:k1_match_first"
    "2:False:False:k2_standard"
    "10:False:False:k10_standard"
    "10:False:True:k10_adaptive"
)

# Counter for run ID
run_counter=5
total_runs=$((${#experiment_configs[@]} * num_runs))

echo "Starting K-Distance experiment suite..."
echo "Total configurations: ${#experiment_configs[@]}"
echo "Runs per configuration: $num_runs"
echo "Total runs: $total_runs"
echo "============================================"

# Loop through each configuration
for config in "${experiment_configs[@]}"; do
    # Parse configuration
    IFS=':' read -r k_neighbours match_first_char use_adaptive_k config_name <<< "$config"
    
    echo ""
    echo "=========================================="
    echo "Starting Configuration: $config_name"
    echo "K Neighbours: $k_neighbours"
    echo "Match First Char: $match_first_char"
    echo "Use Adaptive K: $use_adaptive_k"
    echo "=========================================="
    
    # Run this configuration num_runs times
    for run_num in $(seq 1 $num_runs); do
        # Set current parameters
        export token_k_val="$k_neighbours"
        export match_first_char="$match_first_char"
        export use_adaptive_k="$use_adaptive_k"

        # Generate run name with all parameters
        export run_name="${project_name}_${model}_E${num_epochs}_B${train_batch_size}_${config_name}_run${run_num}"
        export cache_path="/projects/0/hpmlprjs/LLM/danp/UGBench/models/neighbourhood_cache/${model}/${model}.pkl"
        export save_dir="$PWD/experiment/${dataset}/${model}/${split}/_AllExperiments/KDistance/$run_name"
        
        # Create individual log file for this run
        
        echo ""
        echo "----------------------------------------"
        echo "Starting Run $run_counter/$total_runs"
        echo "Config: $config_name (Run $run_num/$num_runs)"
        echo "Token K Neighbours: $k_neighbours"
        echo "Match First Char: $match_first_char"
        echo "Use Adaptive K: $use_adaptive_k"
        echo "Token Replace Prob: $token_replace_prob"
        echo "Run Name: $run_name"
        echo "Save Dir: $save_dir"
        
        # Record start time
        start_time=$(date '+%Y-%m-%d %H:%M:%S')
        
        # Log experiment start to master log
        
        # Run the training and evaluation pipeline with logging
        {
            echo "=== TRAINING PHASE ==="
            
            #export batch_size=16  # Reset batch size for training
            
            #Run actual training
            python forget.py --config-name=forget_pii.yaml \
                dataset=$dataset split=$split \
                forget_data_path=$forget_data_path \
                retain_data_path=$forget_data_path \
                forget_loss=$forget_loss batch_size=$train_batch_size \
                retain_weight=$retain_weight \
                gradient_accumulation_steps=$gradaccum model_family=$model lr=$lr \
                save_dir=$save_dir cache_dir=$cache num_epochs=$num_epochs \
                use_lora=$use_lora \
                use_quantization=$use_quantization \
                project_name=$project_name \
                run_name=$run_name \
                in_text=$in_text \
                token_replace_prob=$token_replace_prob \
                token_k_neighbours=$token_k_val \
                logging.time_stats=$logging_timestats \
                logging.corrupted_subjects=$logging_corrupted_subjects \
                match_first_char=$match_first_char \
                use_adaptive_k=$use_adaptive_k \
                optimal_neighbours_generation=$optimal_neighbours_generation \
                logging.permu_contrast_stats=$logging_permu_contrast_stats \
                optimizer=$optimizer \
                cache_path=$cache_path \


            # Capture actual training exit code
            training_exit_code=$?
            
            if [ $training_exit_code -eq 0 ]; then
                echo ""
                echo "Training completed successfully!"
                echo "=== EVALUATION PHASE ==="
                
                # Check if model exists before evaluation
                if [ -d "$save_dir" ]; then
                    echo "Model directory found: $save_dir"

                    echo "Changed batch size to 64 for evaluation"
                    #export batch_size=64
                    # Evaluation
                    python evaluate_PII.py --config-name=eval_pii.yaml \
                        model_family=$model dataset=$dataset \
                        split=$split batch_size=$eval_batch_size \
                        model_path=$save_dir forget_loss=$forget_loss \
                        generation.max_length=200 \
                        use_lora=$use_lora \
                        save_dir=$save_dir/eval_results \
                    
                    eval_exit_code=$?
                else
                    echo "ERROR: Model directory not found after training: $save_dir"
                    eval_exit_code=1
                fi
                
                if [ $eval_exit_code -eq 0 ]; then
                    echo ""
                    echo "Evaluation completed successfully!"
                    echo "=== AGGREGATION PHASE ==="
                    
                    # Aggregation
                    python aggregate_eval_stat.py \
                        ckpt_result=$save_dir/eval_results/eval_log_aggregated.json \
                        method_name=$forget_loss \
                        save_file=$save_dir/eval_results/eval.csv \
                        excel_file_path=$save_dir/eval_results/eval.xlsx \
                        submitted_by=k_distance_experiment \
                        remove_model_tensors=$remove_model_tensors \

                    agg_exit_code=$?

                    if [ $agg_exit_code -eq 0 ]; then
                        echo "Aggregation completed successfully!"
                        final_status="SUCCESS"
                    else
                        echo "Aggregation failed!"
                        final_status="FAILED_AGGREGATION"
                    fi
                else
                    echo "Evaluation failed!"
                    final_status="FAILED_EVALUATION"
                fi
            else
                echo "Training failed with exit code: $training_exit_code"
                final_status="FAILED_TRAINING"
            fi
            
        } 2>&1
        
        # Record end time and status
        end_time=$(date '+%Y-%m-%d %H:%M:%S')
        
        # Update master log with completion status
        
        echo "Run $run_counter completed with status: $final_status"
        echo "End time: $end_time"
        
        # Increment counter
        ((run_counter++))
        
        # Optional: Add a small delay between runs
        sleep 5
    done
    
    echo ""
    echo "Configuration $config_name completed (${num_runs} runs)"
    
done

echo "============================================"
echo "All K-Distance experiments completed!"
echo "Total runs executed: $((run_counter - 1))"
