export CUDA_VISIBLE_DEVICES='0,1,2,3'
export OMP_NUM_THREADS=1
PYTHONPATH=. python -m torch.distributed.launch --nproc_per_node 4 mnist_ddp.py --gpus 4\
