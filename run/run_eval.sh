#!/bin/bash

read_model_path=$1 # directory which stores the model.bin and params.json, e.g., exp/task_astormer/electra-transformer/
test_batch_size=50
beam_size=5
n_best=5

python -u scripts/train_and_eval.py --read_model_path $read_model_path --test_batch_size $test_batch_size --testing --beam_size $beam_size --n_best $n_best
