task=astormer-base
dataset=$1
seed=999

plm=$2
encode_method=rgatsql
encoder_hidden_size=512
encoder_num_layers=2
num_heads=8

decode_method=ast
decode_order=dfs+l2r
decoder_cell=transformer
decoder_hidden_size=512
decoder_num_layers=2

dropout=0.2
batch_size=20
test_batch_size=50
grad_accumulate=1
lr=2e-4
l2=0.1
layerwise_decay=0.8
warmup_ratio=0.1
lr_schedule=linear
eval_after_iter=30
max_iter=50
max_norm=5
beam_size=5
n_best=5

params="--task $task --dataset $dataset --seed $seed --encode_method $encode_method --plm $plm --encoder_hidden_size $encoder_hidden_size --encoder_num_layers $encoder_num_layers --num_heads $num_heads --decode_method $decode_method --decode_order $decode_order --decoder_cell $decoder_cell --decoder_hidden_size $decoder_hidden_size --decoder_num_layers $decoder_num_layers --dropout $dropout --batch_size $batch_size --test_batch_size $test_batch_size --grad_accumulate $grad_accumulate --eval_after_iter $eval_after_iter --max_iter $max_iter --lr $lr --l2 $l2 --layerwise_decay $layerwise_decay --warmup_ratio $warmup_ratio --lr_schedule $lr_schedule --max_norm $max_norm --beam_size $beam_size --n_best $n_best"

GPU_PER_NODE=${GPU_PER_NODE:-1}
NUM_NODES=${NUM_NODES:-1}
if [ "$GPU_PER_NODE" -gt 1 ] || [ "$NUM_NODES" -gt 1 ] ; then
    NODE_RANK=${NODE_RANK:-0}
    MASTER_ADDR=${MASTER_ADDR:-"127.0.0.1"}
    MASTER_PORT=${MASTER_PORT:-23456}
    python3 -um torch.distributed.launch --nproc_per_node $GPU_PER_NODE --nnodes $NUM_NODES --node_rank $NODE_RANK --master_addr $MASTER_ADDR --master_port $MASTER_PORT scripts/train_and_eval.py --ddp $params
else
    python3 -u scripts/train_and_eval.py $params
fi
