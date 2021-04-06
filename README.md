## A simple pytorch distributed data parallel example

### Features

- Run train and test phrase using single machine and multiple gpus

- Currently, only multi-gpu mode is supported

### Usage


Train and test in ddp mode:


```
./scripts/train_mnist_ddp.sh
```

Train and test in single gpu mode:

```
python mnist.py
```
