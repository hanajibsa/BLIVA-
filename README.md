# Improving VQA Using MLLM
![image](https://github.com/pej0918/BLIVA/assets/79118751/d3de9fc7-cbda-4fb1-ba88-202ac09ee28f)


## Train

After downloading the training datasets and specify their path in [dataset configs](daiv/configs/datasets/), we are ready for training!

0. Setting Environments
```Shell
conda create -n fusion python=3.9
```
```Shell
git clone 
```
```Shell
cd BLIVA
```
```Shell
pip install -e .
```
if packaging error, then
```Shell
pip install setuptools==69.5.1
```

# Training
1. pretraining of Dm-Former
```Shell
python train.py --cfg-path train_configs/pretrain_stage1.yaml
```

2. Pretraining of visual assistant branch


you should specify model path in [ pretrained ](https://github.com/pej0918/BLIVA/blob/main/train_configs/pretrain_bliva_vicuna.yaml#L8)

```Shell
python train.py --cfg-path train_configs/pretrain_stage2.yaml
```

3. Instruction Finetuning 

```Shell
python train.py --cfg-path train_configs/finetune_stage2.yaml
```

# Evaluation
Evaluation of Stage2 
```
python evaluate.py --cfg-path train_configs/pretrain_stage2_eval.yaml
```

```
python evaluate.py --cfg-path train_configs/finetune_stage2_eval.yaml
```

# Training w/ mcan output(prophet)
okvqa
```
python train.py --cfg-path train_configs/finetune_stage2_t5_vqa.yaml
```
```
python evaluate.py --cfg-path train_configs/eval_stage2_vqa.yaml
```
1. answer_latent = self.proj_norm(proj_feat)
/root/workspace/24s-VQA-MLLM/BEiT3/stage2-mcan-prophet/VQA-MLLM-stage2/daiv/output/BLIP2/Finetune_stage2/20240823055
2. img_feat